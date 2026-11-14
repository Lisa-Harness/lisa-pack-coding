"""Tests for the coding pack's sandbox-floor contribution factory.

The contribution must derive its paths from the same config resolution the
worktree tools use (``resolve_worktree_parent`` + ``resolve_workspace_root``),
so the floor and the actual worktree location can never drift apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packstest.git import git_repo

from lisa_packs.coding.sandbox import floor_contribution


@pytest.mark.asyncio
async def test_contribution_declares_direct_write_worktree_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "t").mkdir(parents=True, exist_ok=True)
    repo = await git_repo(tmp_path / "t")
    pack_config = {"workspace_root": str(repo.root)}
    context = {"data_dir": str(tmp_path / "data"), "workspace_root": str(repo.root)}
    contribution = floor_contribution(pack_config, context)
    assert contribution is not None
    assert contribution["execution"] == "sandbox"
    assert contribution["persistence"] == "direct-write"
    expected_parent = str((tmp_path / "data" / "git-worktrees" / "source").resolve())
    assert contribution["paths"] == {
        expected_parent: "rw",
        str(repo.root.resolve()): "rw",
    }


@pytest.mark.asyncio
async def test_uv_cache_root_contributed_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A uv-based session contributes ~/.cache/uv when it exists on the
    # host; the location is the toolchain's default (HOME passes through
    # the sandbox env allowlist), so the bind matches what uv resolves
    # inside the sandbox.
    (tmp_path / "t").mkdir(parents=True, exist_ok=True)
    repo = await git_repo(tmp_path / "t", with_remote=False)
    (tmp_path / ".cache" / "uv").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    contribution = floor_contribution(
        {
            "workspace_root": str(repo.root),
            "checks": [{"id": "lint", "argv": ["uv", "run", "ruff", "check"]}],
        },
        {"data_dir": str(tmp_path), "workspace_root": str(repo.root)},
    )
    assert contribution is not None
    assert contribution["paths"][str(tmp_path / ".cache" / "uv")] == "rw"


@pytest.mark.asyncio
async def test_go_cache_roots_contributed_for_go_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Go-era floor carried ~/.cache/go-build + ~/go/pkg/mod by hand;
    # the contribution derives them from the configured checks, so the
    # same session manifest works across language platforms.
    (tmp_path / "t").mkdir(parents=True, exist_ok=True)
    repo = await git_repo(tmp_path / "t", with_remote=False)
    (tmp_path / ".cache" / "go-build").mkdir(parents=True)
    (tmp_path / "go" / "pkg" / "mod").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    contribution = floor_contribution(
        {
            "workspace_root": str(repo.root),
            "checks": [{"id": "test", "argv": ["go", "test", "./..."]}],
        },
        {"data_dir": str(tmp_path), "workspace_root": str(repo.root)},
    )
    assert contribution is not None
    assert contribution["paths"][str(tmp_path / ".cache" / "go-build")] == "rw"
    assert contribution["paths"][str(tmp_path / "go" / "pkg" / "mod")] == "rw"


@pytest.mark.asyncio
async def test_missing_cache_root_is_not_contributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Floor rw binds have no existence check; contributing a missing
    # directory would abort every sandboxed run. A host without the cache
    # yet contributes nothing — first sync outside the sandbox creates it.
    # workspace_root stays a real git repo (strict resolve), while HOME
    # points at a nonexistent root so every cache candidate is dropped.
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent-home"))
    # A nonexistent workspace_root raises (strict resolve); the assertion
    # target is the cache path, so use a real repo and check that no
    # cache-root key derived from the missing HOME appears.
    (tmp_path / "t").mkdir(parents=True, exist_ok=True)
    repo = await git_repo(tmp_path / "t", with_remote=False)
    contribution = floor_contribution(
        {
            "workspace_root": str(repo.root),
            "checks": [{"id": "lint", "argv": ["uv", "run", "ruff", "check"]}],
        },
        {"data_dir": str(tmp_path), "workspace_root": str(repo.root)},
    )
    assert contribution is not None
    missing_uv = str(tmp_path / "nonexistent-home" / ".cache" / "uv")
    assert missing_uv not in contribution["paths"]


@pytest.mark.asyncio
async def test_pnpm_cache_roots_contributed_for_pnpm_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pnpm resolves its content store and metadata cache from HOME alone
    # (both are absent from the bwrap child-env allowlist's override list),
    # so a pnpm session needs those roots bound or an install either fails
    # on an unwritable store or relocates it into the worktree.
    (tmp_path / "t").mkdir(parents=True, exist_ok=True)
    repo = await git_repo(tmp_path / "t", with_remote=False)
    (tmp_path / ".local" / "share" / "pnpm" / "store").mkdir(parents=True)
    (tmp_path / ".cache" / "pnpm").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    contribution = floor_contribution(
        {
            "workspace_root": str(repo.root),
            "checks": [{"id": "type", "argv": ["pnpm", "exec", "tsc", "--noEmit"]}],
            "preparation_recipes": [
                {"id": "node-pnpm", "prepare_argv": ["pnpm", "install", "--frozen-lockfile"]}
            ],
        },
        {"data_dir": str(tmp_path), "workspace_root": str(repo.root)},
    )
    assert contribution is not None
    assert contribution["paths"][str(tmp_path / ".local" / "share" / "pnpm" / "store")] == "rw"
    assert contribution["paths"][str(tmp_path / ".cache" / "pnpm")] == "rw"


@pytest.mark.asyncio
async def test_pnpm_token_does_not_match_npm_and_env_prefix_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Token matching is exact: an ``npm`` session must not pick up pnpm
    # stores, while ``env FOO=bar pnpm install`` must (the word is still a
    # whole argv token, which the old space-joined substring test missed).
    (tmp_path / "t").mkdir(parents=True, exist_ok=True)
    repo = await git_repo(tmp_path / "t", with_remote=False)
    (tmp_path / ".npm").mkdir(parents=True)
    (tmp_path / ".local" / "share" / "pnpm" / "store").mkdir(parents=True)
    (tmp_path / ".cache" / "pnpm").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    npm_only = floor_contribution(
        {
            "workspace_root": str(repo.root),
            "checks": [{"id": "test", "argv": ["npm", "test"]}],
        },
        {"data_dir": str(tmp_path), "workspace_root": str(repo.root)},
    )
    assert npm_only is not None
    assert str(tmp_path / ".npm") in npm_only["paths"]
    assert str(tmp_path / ".cache" / "pnpm") not in npm_only["paths"]

    env_prefixed = floor_contribution(
        {
            "workspace_root": str(repo.root),
            "checks": [
                {
                    "id": "test",
                    "argv": ["env", "CI=1", "pnpm", "run", "test"],
                }
            ],
        },
        {"data_dir": str(tmp_path), "workspace_root": str(repo.root)},
    )
    assert env_prefixed is not None
    assert str(tmp_path / ".cache" / "pnpm") in env_prefixed["paths"]


@pytest.mark.asyncio
async def test_contribution_omits_workspace_when_inside_worktree_parent(
    tmp_path: Path,
) -> None:
    (tmp_path / "t").mkdir(parents=True, exist_ok=True)
    repo = await git_repo(tmp_path / "t")
    pack_config = {
        "workspace_root": str(repo.root),
        # Explicit worktree parent containing the workspace: one grant
        # covers both; the workspace entry would be redundant nesting.
        "worktree_dir": str(repo.root.parent),
    }
    context = {"data_dir": str(tmp_path / "data"), "workspace_root": str(repo.root)}
    contribution = floor_contribution(pack_config, context)
    assert contribution is not None
    assert contribution["paths"] == {str(repo.root.parent): "rw"}


@pytest.mark.asyncio
async def test_contribution_rejects_invalid_pack_config(tmp_path: Path) -> None:
    # CodingPackConfig requires workspace_root; the factory must surface
    # the config error, not paper over it.
    with pytest.raises(Exception):  # noqa: B017 - config error type is pack-internal
        floor_contribution({}, {"data_dir": str(tmp_path)})


@pytest.mark.asyncio
async def test_contribution_names_only_axes_every_implementation_can_enforce(
    tmp_path: Path,
) -> None:
    """The contribution must be portable across sandbox implementations.

    It is merged onto whichever implementation the session selects, and each
    implementation validates what it can enforce. Naming a namespace-era axis
    here (``network``/``device``/``ipc``) would reject the pack load on a
    Windows Job Object sandbox; the contribution deliberately names only the
    posture every implementation can honour.
    """
    (tmp_path / "t").mkdir(parents=True, exist_ok=True)
    repo = await git_repo(tmp_path / "t")
    contribution = floor_contribution(
        {"workspace_root": str(repo.root)},
        {"data_dir": str(tmp_path / "data"), "workspace_root": str(repo.root)},
    )
    assert contribution is not None
    assert set(contribution) == {"execution", "persistence", "paths"}
    assert contribution["execution"] == "sandbox"
    assert contribution["persistence"] == "direct-write"
    assert set(contribution["paths"].values()) == {"rw"}
