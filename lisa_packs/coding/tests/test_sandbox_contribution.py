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
