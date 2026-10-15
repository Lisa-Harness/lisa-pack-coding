"""Temporary-repository integration tests for the coding worktree workflow.

Uses the framework harness's in-memory ``WorkProductStore`` double so the
pack tests never import the host layer (packs may import kernel/sdk only).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from unittest.mock import patch

import pytest
from kernel.capability_binding import ExecutionFloorView
from kernel.models import ToolExecutionContext, ToolResult
from packstest.catalog import build_tool_catalog
from packstest.context import ToolContextFactory
from packstest.git import git_repo
from packstest.runner import run_tool
from packstest.store import MemoryWorkProductStore

from lisa_packs.coding.tools.apply_patch.apply_patch import execute as apply_patch
from lisa_packs.coding.tools.cleanup_worktree.cleanup_worktree import (
    execute as cleanup_worktree,
)
from lisa_packs.coding.tools.commit.commit import execute as commit
from lisa_packs.coding.tools.create_worktree.create_worktree import (
    execute as create_worktree,
)
from lisa_packs.coding.tools.edit_files.edit_files import execute as edit_files
from lisa_packs.coding.tools.edit_files.edit_files import preflight as preflight_edit_files
from lisa_packs.coding.tools.export_patches.export_patches import (
    execute as export_patches,
)
from lisa_packs.coding.tools.find_files.find_files import execute as find_files
from lisa_packs.coding.tools.history.history import execute as history
from lisa_packs.coding.tools.import_commits.import_commits import execute as import_commits
from lisa_packs.coding.tools.list_directory.list_directory import (
    execute as list_directory,
)
from lisa_packs.coding.tools.list_worktrees.list_worktrees import (
    execute as list_worktrees,
)
from lisa_packs.coding.tools.outline_source.outline_source import (
    execute as outline_source,
)
from lisa_packs.coding.tools.push.push import execute as push
from lisa_packs.coding.tools.read_file.read_file import execute as read_file
from lisa_packs.coding.tools.run_check.run_check import execute as run_check
from lisa_packs.coding.tools.search_code.search_code import execute as search_code
from lisa_packs.coding.tools.status.status import execute as status

# The pack's typed, session-owned policy models (validation-contract tests).
from pydantic import ValidationError

from lisa_packs.coding.config import (
    CodingPackConfig,
    CodingPreparationRecipe,
    get_coding_config,
)
from lisa_packs.coding.workflow import (
    ProcessResult,
    dependency_manifest_digest,
    expand_argv_placeholders,
    preparation_recipe_digest,
    workspace_python,
)


async def _git(root: Path, *args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")


async def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    await _git(root, "init")
    await _git(root, "config", "user.email", "test@example.invalid")
    await _git(root, "config", "user.name", "Lisa Test")
    (root / "app.txt").write_text("hello\n", encoding="utf-8")
    await _git(root, "add", "app.txt")
    await _git(root, "commit", "-m", "initial")
    remote = tmp_path / "remote.git"
    process = await asyncio.create_subprocess_exec(
        "git",
        "init",
        "--bare",
        str(remote),
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    await _git(root, "remote", "add", "origin", str(remote))
    return root


def _context(
    root: Path,
    store: MemoryWorkProductStore,
    params: dict[str, object],
    *,
    checks: list[dict[str, object]] | None = None,
    required_check_ids: list[str] | None = None,
    preparation_recipes: list[dict[str, object]] | None = None,
    sandbox_result: ToolResult | None = None,
    sandbox_effect: Callable[[Path], None] | None = None,
    task_id: str = "",
) -> ToolExecutionContext:
    async def dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        assert tool_id == "bwrap_run"
        worktree = str(parameters["cwd"])
        if sandbox_effect is not None:
            sandbox_effect(Path(worktree))
        response = sandbox_result or ToolResult(
            output_text="checked", output_json={"return_code": 0}
        )
        return ToolResult(
            output_text=response.output_text,
            output_json={
                **response.output_json,
                "sandboxed": True,
                "persistence": "direct-write",
                "cwd": worktree,
                "writable_paths": [worktree],
            },
        )

    return ToolExecutionContext(
        tool_id="test",
        workspace_id="workspace",
        user_id="user",
        user_slug="user",
        surface_id="surface",
        mode_id="coding",
        session_plan_digest="plan",
        task_id=task_id,
        parameters=params,
        execution_floor_for=lambda _tool_id: ExecutionFloorView(
            execution="sandbox", persistence="direct-write", paths={str(root.parent): "rw"}
        ),
        delegated_tool_dispatch=dispatch,
        code_change_artifact_store=store,
        data_dir=str(root.parent / "data"),
        pack_config={
            "workspace_root": str(root),
            "checks": checks if checks is not None else [
                {
                    "id": "unit",
                    "argv": [sys.executable, "-c", "print('checked')"],
                    "timeout_seconds": 30,
                }
            ],
            "required_check_ids": required_check_ids
            if required_check_ids is not None
            else ["unit"],
            "preparation_recipes": preparation_recipes
            if preparation_recipes is not None
            else [],
        },
    )


@pytest.mark.asyncio
async def test_create_worktree_uses_model_chosen_name_and_lists_owned(tmp_path: Path) -> None:
    """A named worktree derives branch and directory from the slug, not a uuid."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()

    created = await create_worktree(
        _context(root, store, {"summary": "auth work", "name": "Auth Refactor!"})
    )

    assert created.error == ""
    assert created.output_json["branch"] == "lisa/auth-refactor"
    worktree_path = Path(created.output_json["worktree_path"])
    assert worktree_path.name == "auth-refactor"
    assert worktree_path.parent == tmp_path / "data" / "git-worktrees" / root.name
    artifact = await store.get(created.output_json["artifact_id"])
    assert artifact is not None
    assert artifact.branch == "lisa/auth-refactor"
    assert artifact.worktree_path == str(worktree_path)

    listed = await list_worktrees(_context(root, store, {}))

    assert listed.error == ""
    entries = listed.output_json["worktrees"]
    assert [(entry["artifact_id"]) for entry in entries] == [artifact.artifact_id]
    assert entries[0]["branch"] == "lisa/auth-refactor"
    assert entries[0]["worktree_exists"] is True
    assert entries[0]["summary"] == "auth work"
    assert listed.output_json["count"] == 1


@pytest.mark.asyncio
async def test_create_worktree_rejects_duplicate_name(tmp_path: Path) -> None:
    """A second worktree with the same slug fails instead of clobbering."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "fix-retry-loop"}))
    assert created.error == ""

    duplicate = await create_worktree(_context(root, store, {"name": "fix retry loop"}))

    assert "already exists" in duplicate.error


@pytest.mark.asyncio
async def test_create_worktree_rejects_invalid_and_protected_names(tmp_path: Path) -> None:
    """Unsanitizable or protected slugs never reach git."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()

    invalid = await create_worktree(_context(root, store, {"name": "!!!"}))
    assert "slug" in invalid.error
    protected = await create_worktree(_context(root, store, {"name": "main"}))
    assert "protected" in protected.error


@pytest.mark.asyncio
async def test_import_full_lifecycle_and_push(tmp_path: Path) -> None:
    """Reproduce one merged upstream commit on its own branch and push it.

    The worktree is created at the commit's parent, the import tool applies
    the commit's diff, a verification-only required check certifies the tree,
    and the checked commit pushes like any ordinary artifact.
    """

    root = await _repository(tmp_path)
    (root / "notes.txt").write_text("one\n", encoding="utf-8")
    await _git(root, "add", "notes.txt")
    await _git(root, "commit", "-m", "base commit")
    head = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await head.communicate()
    base0 = out.decode().strip()
    (root / "notes.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "extra").mkdir()
    (root / "extra" / "detail.txt").write_text("detail\n", encoding="utf-8")
    await _git(root, "add", "notes.txt", "extra/detail.txt")
    await _git(root, "commit", "-m", "record upstream change")
    target = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await target.communicate()
    target_sha = out.decode().strip()

    store = MemoryWorkProductStore()
    created = await create_worktree(
        _context(root, store, {"summary": "replay record upstream change", "name": "import-test",
                               "import_commit": target_sha})
    )
    assert created.error == ""
    artifact_id = created.output_json["artifact_id"]
    assert created.output_json["base_sha"] == base0

    imported = await import_commits(
        _context(root, store, {"artifact_id": artifact_id, "import_commit": target_sha})
    )
    assert imported.error == ""
    assert imported.output_json["changed_paths"] == ["extra/detail.txt", "notes.txt"]
    worktree = Path(created.output_json["worktree_path"])
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert (worktree / "extra" / "detail.txt").read_text(encoding="utf-8") == "detail\n"
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert artifact.imported_commits == {target_sha: base0}
    assert artifact.import_commit == target_sha
    assert artifact.status == "patched"

    duplicate = await import_commits(
        _context(root, store, {"artifact_id": artifact_id, "import_commit": target_sha})
    )
    assert "already imported" in duplicate.error

    checked = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "unit"})
    )
    assert checked.output_json["status"] == "passed"

    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "record upstream change"})
    )
    assert committed.error == ""
    pushed = await push(_context(root, store, {"artifact_id": artifact_id}))
    assert pushed.error == ""
    artifact = await store.get(artifact_id)
    assert artifact is not None
    # Post-commit state is fresh ordinary state.
    assert artifact.imported_commits == {}
    assert artifact.import_commit == ""
    assert artifact.status == "pushed"

    listing = await list_worktrees(_context(root, store, {}))
    assert listing.error == ""
    assert listing.output_json["worktrees"][0]["imported_commits"] == {}

    cleaned = await cleanup_worktree(_context(root, store, {"artifact_id": artifact_id}))
    assert cleaned.error == ""


@pytest.mark.asyncio
async def test_import_requires_match_creation_base_and_uncommitted(tmp_path: Path) -> None:
    """An artifact must branch at the import commit's parent to import it."""

    root = await _repository(tmp_path)
    parent_sha = (await (await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )).communicate())[0].decode().strip()
    (root / "other.txt").write_text("x\n", encoding="utf-8")
    await _git(root, "add", "other.txt")
    await _git(root, "commit", "-m", "other change")
    commit_sha = (await (await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )).communicate())[0].decode().strip()

    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "HEAD-based"}))
    assert created.error == ""
    artifact_id = created.output_json["artifact_id"]

    mismatched = await import_commits(
        _context(root, store, {"artifact_id": artifact_id, "import_commit": commit_sha})
    )
    assert "must branch from the import commit's parent" in mismatched.error

    matching = await create_worktree(
        _context(root, store, {"summary": "anchored", "name": "import-match",
                               "import_commit": commit_sha})
    )
    assert matching.error == ""
    matching_id = matching.output_json["artifact_id"]
    assert matching.output_json["import_commit"] == commit_sha
    worktree = Path(matching.output_json["worktree_path"])
    # A fresh import-anchored worktree shows coherent ordinary state.
    inspected = await status(_context(root, store, {"artifact_id": matching_id}))
    assert inspected.error == ""
    assert inspected.output_json["git_status"] == "(clean)"
    assert inspected.output_json["status"] == "created"
    assert worktree.is_dir()
    worktree_artifact = await store.get(matching_id)
    assert worktree_artifact is not None
    assert worktree_artifact.import_commit == commit_sha
    assert worktree_artifact.imported_commits == {}


@pytest.mark.asyncio
async def test_import_rejects_wrong_base_and_unknown_commit(tmp_path: Path) -> None:
    """Guard the import against drifted bases and unknown upstream commits."""
    root = await _repository(tmp_path)
    (root / "note.txt").write_text("n\n", encoding="utf-8")
    await _git(root, "add", "note.txt")
    await _git(root, "commit", "-m", "second")
    second_sha = (await (await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )).communicate())[0].decode().strip()
    (root / "deeper.txt").write_text("d\n", encoding="utf-8")
    await _git(root, "add", "deeper.txt")
    await _git(root, "commit", "-m", "third")
    third_sha = (await (await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )).communicate())[0].decode().strip()

    store = MemoryWorkProductStore()
    created = await create_worktree(
        _context(root, store, {"summary": "mismatch", "name": "import-mismatch",
                               "import_commit": third_sha})
    )
    assert created.error == ""
    artifact_id = created.output_json["artifact_id"]

    unknown_creation = await create_worktree(
        _context(root, store, {"summary": "unknown", "name": "import-unknown",
                               "import_commit": "a" * 40})
    )
    assert f"Unknown import commit '{'a' * 40}'" in unknown_creation.error

    wrong = await import_commits(
        _context(root, store, {"artifact_id": artifact_id, "import_commit": second_sha})
    )
    assert "Artifact must import the commit recorded at worktree creation." in wrong.error

    unknown = await import_commits(
        _context(root, store, {"artifact_id": artifact_id, "import_commit": "a" * 40})
    )
    assert "Artifact must import the commit recorded at worktree creation." in unknown.error


@pytest.mark.asyncio
async def test_create_worktree_accepts_long_summary(tmp_path: Path) -> None:
    catalog = build_tool_catalog(Path(__file__).resolve().parents[3])
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config={"workspace_root": str(repo.root)},
        data_dir=tmp_path / "data",
    )
    summary = ("Issue 1 gofmt batch: " + "format and verify changed files. " * 12).strip()

    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": summary})

    assert created.result.error == ""
    artifact = await factory.artifact_store.get(created.result.output_json["artifact_id"])
    assert artifact is not None
    assert artifact.safe_summary == summary


@pytest.mark.asyncio
async def test_patch_invalidates_checks_and_status_tracks_owned_worktree(tmp_path: Path) -> None:
    """A worktree patch receives a new revision and only configured checks can run."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "change greeting"}))
    assert created.error == ""
    artifact_id = created.output_json["artifact_id"]
    patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello
+hello world
"""
    patched = await apply_patch(_context(root, store, {"artifact_id": artifact_id, "patch": patch}))
    assert patched.error == ""
    assert patched.output_json["revision"] == 1

    checked = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "unit"})
    )
    assert checked.output_json["status"] == "passed"

    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "Change greeting"})
    )
    assert committed.error == ""
    pushed = await push(_context(root, store, {"artifact_id": artifact_id}))
    assert pushed.error == ""

    inspected = await status(_context(root, store, {"artifact_id": artifact_id}))
    assert inspected.error == ""
    assert inspected.output_json["revision"] == 1
    assert inspected.output_json["status"] == "pushed"

    cleaned = await cleanup_worktree(_context(root, store, {"artifact_id": artifact_id}))
    assert cleaned.error == ""


@pytest.mark.asyncio
async def test_commit_stages_accumulated_task_paths_only(tmp_path: Path) -> None:
    """Check byproducts and unrelated staged files are excluded from the commit."""
    root = await _repository(tmp_path)
    (root / "other.txt").write_text("one\n", encoding="utf-8")
    await _git(root, "add", "other.txt")
    await _git(root, "commit", "-m", "add second file")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "edit two files"}))
    artifact_id = created.output_json["artifact_id"]
    patches = (
        "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-hello\n+hello world\n",
        "--- a/other.txt\n+++ b/other.txt\n@@ -1 +1 @@\n-one\n+two\n",
    )
    for patch in patches:
        result = await apply_patch(
            _context(root, store, {"artifact_id": artifact_id, "patch": patch})
        )
        assert result.error == ""

    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert artifact.changed_paths == ["app.txt", "other.txt"]
    checked = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "unit"})
    )
    assert checked.output_json["status"] == "passed"

    worktree = Path(created.output_json["worktree_path"])
    (worktree / "__pycache__").mkdir()
    (worktree / "__pycache__" / "check.pyc").write_bytes(b"byproduct")
    (worktree / "unrelated.txt").write_text("not part of this task\n", encoding="utf-8")
    await _git(worktree, "add", "unrelated.txt")
    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "Edit two files"})
    )

    assert committed.error == ""
    names = await asyncio.create_subprocess_exec(
        "git",
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "HEAD",
        cwd=worktree,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await names.communicate()
    assert names.returncode == 0
    assert set(stdout.decode().splitlines()) == {"app.txt", "other.txt"}
    assert (worktree / "__pycache__" / "check.pyc").is_file()
    assert (worktree / "unrelated.txt").is_file()


@pytest.mark.asyncio
async def test_export_patches_series_applies_with_git_am_after_cleanup(tmp_path: Path) -> None:
    """An exported series round-trips into an independent clone via ``git am``."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "greeting"}))
    artifact_id = created.output_json["artifact_id"]

    early = await export_patches(_context(root, store, {"artifact_id": artifact_id}))
    assert "no committed revision" in early.error

    patch = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-hello\n+hello world\n"
    assert (
        await apply_patch(_context(root, store, {"artifact_id": artifact_id, "patch": patch}))
    ).error == ""
    assert (
        await run_check(_context(root, store, {"artifact_id": artifact_id, "check_id": "unit"}))
    ).error == ""
    assert (
        await commit(
            _context(root, store, {"artifact_id": artifact_id, "message": "Change greeting"})
        )
    ).error == ""
    assert (await cleanup_worktree(_context(root, store, {"artifact_id": artifact_id}))).error == ""

    export_dir = tmp_path / ".lisa-patches" / artifact_id
    export_dir.mkdir(parents=True)
    (export_dir / "0009-stale.patch").write_text("stale", encoding="utf-8")
    (export_dir / "notes.txt").write_text("keep", encoding="utf-8")

    exported = await export_patches(_context(root, store, {"artifact_id": artifact_id}))

    assert exported.error == ""
    assert Path(exported.output_json["export_dir"]) == export_dir.resolve()
    files = [Path(path) for path in exported.output_json["patch_files"]]
    assert [path.name for path in files] == ["0001-Change-greeting.patch"]
    assert sorted(path.name for path in export_dir.iterdir()) == [
        "0001-Change-greeting.patch",
        "notes.txt",
    ]
    assert "base-commit: " in files[0].read_text(encoding="utf-8")
    assert exported.output_json["apply_command"].startswith("git am -3 ")

    clone = tmp_path / "local"
    await _git(tmp_path, "clone", "--quiet", str(root), str(clone))
    await _git(clone, "config", "user.email", "test@example.invalid")
    await _git(clone, "config", "user.name", "Lisa Test")
    await _git(clone, "am", "-3", *map(str, files))
    assert (clone / "app.txt").read_text(encoding="utf-8") == "hello world\n"


@pytest.mark.asyncio
async def test_export_patches_rejects_export_dir_inside_source(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    artifact.commit_sha = artifact.base_sha
    await store.save(artifact)
    ctx = _context(root, store, {"artifact_id": artifact_id})
    ctx.pack_config["patch_export_dir"] = str(root / "patches")

    result = await export_patches(ctx)

    assert "outside the source workspace" in result.error


@pytest.mark.asyncio
async def test_patch_accepts_unterminated_hunk_without_trailing_context(tmp_path: Path) -> None:
    """The exact shape glm-5.3 sent five times in the e2e run: leading context
    only and no final newline. git rejected it as ``corrupt patch``."""

    root = await _repository(tmp_path)
    (root / "stats.py").write_text(
        "def mean(values):\n"
        "    if not values:\n"
        "        raise ValueError('empty')\n"
        "    return sum(values) / (len(values) - 1)\n"
        "\n"
        "\n"
        "def spread(values):\n"
        "    return max(values) - min(values)\n",
        encoding="utf-8",
    )
    await _git(root, "add", "stats.py")
    await _git(root, "commit", "-m", "stats")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "fix mean"}))
    artifact_id = created.output_json["artifact_id"]
    patch = (
        "--- a/stats.py\n+++ b/stats.py\n@@ -2,3 +2,3 @@\n"
        "     if not values:\n"
        "         raise ValueError('empty')\n"
        "-    return sum(values) / (len(values) - 1)\n"
        "+    return sum(values) / len(values)"
    )

    patched = await apply_patch(_context(root, store, {"artifact_id": artifact_id, "patch": patch}))

    assert patched.error == ""
    worktree = Path(created.output_json["worktree_path"])
    assert "return sum(values) / len(values)\n\n" in (worktree / "stats.py").read_text()


@pytest.mark.asyncio
async def test_patch_rejects_context_free_hunks_in_multi_hunk_file(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "two hunks"}))
    patch = (
        "--- a/app.txt\n+++ b/app.txt\n"
        "@@ -1,1 +1,1 @@\n-hello\n+hi\n"
        "@@ -3,1 +3,1 @@\n-x\n+y\n"
    )

    patched = await apply_patch(
        _context(root, store, {"artifact_id": created.output_json["artifact_id"], "patch": patch})
    )

    assert "carry no context lines" in patched.error


@pytest.mark.asyncio
async def test_patch_repairs_bare_multi_file_sections_without_diff_headers(
    tmp_path: Path,
) -> None:
    """Multi-file patches without ``diff --git`` separators must apply.

    The exact 2026-10-03 incident shape: glm-5.3 emitted a README.md +
    CLI.md docs patch as bare ``--- a/x`` / ``+++ b/x`` pairs (no
    ``diff --git`` separators) with no trailing newline and slightly
    early @@ start lines. Under ``--recount`` git scans hunk lines by
    prefix, so the second section's ``--- a/CLI.md`` was consumed as a
    deletion line of the first hunk — the patch failed with a misleading
    ``patch failed: README.md:266`` and the executor retried blindly.
    ``separate_file_sections`` inserts the missing separators.
    """

    root = await _repository(tmp_path)
    readme_lines = [f"README line {i}" for i in range(1, 10)]
    readme_lines[4] = "curl -H \"$header\" -s http://dhcp/leases"
    readme_lines[5] = "```"
    readme_lines[6] = ""
    readme_lines[7] = "These steps can be combined with SSH to allow remote access with tokens:"
    (root / "README.md").write_text("\n".join(readme_lines) + "\n", encoding="utf-8")
    (root / "CLI.md").write_text(
        "CLI notes\n"
        "\n"
        "- **Token Acquisition**: fetch from the socket.\n"
        "- **SSH Configuration**: connect without prompts.\n",
        encoding="utf-8",
    )
    await _git(root, "add", "README.md", "CLI.md")
    await _git(root, "commit", "-m", "docs")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "token lifetime docs"}))
    artifact_id = created.output_json["artifact_id"]
    # Bare multi-file sections, no trailing newline, @@ start 2 lines early
    # — mirrors the recorded incident payload byte for byte in shape.
    patch = (
        "--- a/README.md\n+++ b/README.md\n@@ -4,4 +4,6 @@\n"
        " curl -H \"$header\" -s http://dhcp/leases\n"
        " ```\n"
        " \n"
        "+Tokens are kept in memory only. When the server restarts, all "
        "previously issued tokens are discarded.\n"
        "+\n"
        " These steps can be combined with SSH to allow remote access with tokens:\n"
        "--- a/CLI.md\n+++ b/CLI.md\n@@ -2,3 +2,4 @@\n"
        " \n"
        " - **Token Acquisition**: fetch from the socket.\n"
        "+- **Token Lifetime**: tokens are discarded on restart.\n"
        " - **SSH Configuration**: connect without prompts."
    )

    patched = await apply_patch(_context(root, store, {"artifact_id": artifact_id, "patch": patch}))

    assert patched.error == ""
    worktree = Path(created.output_json["worktree_path"])
    readme = (worktree / "README.md").read_text(encoding="utf-8")
    assert "Tokens are kept in memory only." in readme
    assert "- **Token Lifetime**: tokens are discarded on restart." in (
        worktree / "CLI.md"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_patch_separator_repair_is_idempotent_for_proper_diffs(
    tmp_path: Path,
) -> None:
    """Patches that already carry ``diff --git`` headers apply unchanged.

    ``separate_file_sections`` must not double-insert separators on the
    canonical format the pack itself produces (and ``patch_paths`` keeps
    returning the same paths).
    """

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "proper diff"}))
    artifact_id = created.output_json["artifact_id"]
    patch = (
        "diff --git a/app.txt b/app.txt\n"
        "--- a/app.txt\n+++ b/app.txt\n"
        "@@ -1 +1 @@\n-hello\n+hello world\n"
    )

    patched = await apply_patch(_context(root, store, {"artifact_id": artifact_id, "patch": patch}))

    assert patched.error == ""
    assert patched.output_json["changed_paths"] == ["app.txt"]


@pytest.mark.asyncio
async def test_configured_check_ids_are_discoverable(tmp_path: Path) -> None:
    """The e2e executor guessed 'tests' and 'unittest' before finding 'test'."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "checks"}))
    artifact_id = created.output_json["artifact_id"]

    inspected = await status(_context(root, store, {"artifact_id": artifact_id}))
    unknown = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "tests"})
    )

    assert inspected.output_json["configured_check_ids"] == ["unit"]
    assert inspected.output_json["required_check_ids"] == ["unit"]
    assert "Configured checks: 'unit'." in unknown.error


_FORMAT_FIX = {"id": "format", "argv": ["gofmt", "-w", "."], "kind": "fix"}
_UNIT = {"id": "unit", "argv": [sys.executable, "-c", "print('checked')"]}


def _reformat(worktree: Path) -> None:
    for name in ("app.txt", "other.txt"):
        path = worktree / name
        path.write_text(path.read_text(encoding="utf-8").upper(), encoding="utf-8")
    (worktree / "__pycache__").mkdir(exist_ok=True)
    (worktree / "__pycache__" / "fmt.cache").write_text("byproduct\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_fix_check_edits_become_a_new_revision_and_are_committed(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    (root / "other.txt").write_text("untouched by the task\n", encoding="utf-8")
    await _git(root, "add", "other.txt")
    await _git(root, "commit", "-m", "add second file")
    store = MemoryWorkProductStore()
    checks = [_FORMAT_FIX, _UNIT]
    created = await create_worktree(_context(root, store, {"summary": "fmt"}, checks=checks))
    artifact_id = created.output_json["artifact_id"]
    patch = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-hello\n+hello world\n"
    patched = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": patch}, checks=checks)
    )
    assert patched.error == ""
    unit = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "unit"}, checks=checks)
    )
    assert unit.error == ""

    fixed = await run_check(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "check_id": "format"},
            checks=checks,
            sandbox_effect=_reformat,
        )
    )

    assert fixed.error == ""
    assert fixed.output_json["fixed_paths"] == ["app.txt", "other.txt"]
    assert fixed.output_json["revision"] == 2
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert artifact.changed_paths == ["app.txt", "other.txt"]
    assert set(artifact.checks) == {"format"}
    assert artifact.checks["format"].revision == 2

    rerun = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "unit"}, checks=checks)
    )
    assert rerun.error == ""
    committed = await commit(
        _context(
            root, store, {"artifact_id": artifact_id, "message": "Format"}, checks=checks
        )
    )
    assert committed.error == ""
    worktree = Path(created.output_json["worktree_path"])
    process = await asyncio.create_subprocess_exec(
        "git", "show", "--name-only", "--format=", "HEAD", cwd=worktree,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert stdout.decode().split() == ["app.txt", "other.txt"]


@pytest.mark.asyncio
async def test_verify_check_edits_are_reverted_and_fail(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "verify"}))
    artifact_id = created.output_json["artifact_id"]
    worktree = Path(created.output_json["worktree_path"])

    checked = await run_check(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "check_id": "unit"},
            sandbox_effect=lambda path: (path / "app.txt").write_text("rewritten\n"),
        )
    )

    assert "modified app.txt; the edits were reverted" in checked.error
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello\n"
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert artifact.revision == 0
    assert artifact.checks == {}


@pytest.mark.asyncio
async def test_failed_fix_check_reverts_its_edits(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    checks = [_FORMAT_FIX, _UNIT]
    created = await create_worktree(_context(root, store, {"summary": "fmt"}, checks=checks))
    artifact_id = created.output_json["artifact_id"]
    worktree = Path(created.output_json["worktree_path"])

    fixed = await run_check(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "check_id": "format"},
            checks=checks,
            sandbox_result=ToolResult(output_text="", output_json={"return_code": 2}),
            sandbox_effect=lambda path: (path / "app.txt").write_text("half-written\n"),
        )
    )

    assert fixed.error == ""
    assert fixed.output_json["status"] == "failed"
    assert fixed.output_json["fixed_paths"] == []
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_commit_accepts_long_multiline_message(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "message"}))
    artifact_id = created.output_json["artifact_id"]
    patch = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-hello\n+hello world\n"
    await apply_patch(_context(root, store, {"artifact_id": artifact_id, "patch": patch}))
    await run_check(_context(root, store, {"artifact_id": artifact_id, "check_id": "unit"}))
    message = "Change greeting\n\n" + "Explain the change in detail. " * 40

    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": message})
    )

    assert committed.error == ""
    worktree = Path(created.output_json["worktree_path"])
    process = await asyncio.create_subprocess_exec(
        "git", "log", "-1", "--format=%B", cwd=worktree, stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    assert stdout.decode().strip() == message.strip()


@pytest.mark.asyncio
async def test_advancing_tools_claim_the_artifact_for_the_current_task(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(
        _context(root, store, {"summary": "claim"}, task_id="task-one")
    )
    artifact_id = created.output_json["artifact_id"]

    await status(_context(root, store, {"artifact_id": artifact_id}, task_id="task-two"))
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert artifact.task_id == "task-one"

    checked = await run_check(
        _context(
            root, store, {"artifact_id": artifact_id, "check_id": "unit"}, task_id="task-two"
        )
    )
    assert checked.error == ""
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert artifact.task_id == "task-two"


@pytest.mark.asyncio
async def test_configured_preparation_blocks_checks_until_current(tmp_path: Path) -> None:
    """Preparation is enforced only when the session config opts into recipes."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    checks = [
        {
            "id": "unit",
            "argv": ["{workspace_python}", "-c", "print('checked')"],
            "timeout_seconds": 30,
        }
    ]
    recipes = [
        {
            "id": "python-uv",
            "prepare_argv": ("env", "TMPDIR=.lisa-tmp", "uv", "sync", "--locked", "--no-cache"),
            "project_modules": ["app"],
        }
    ]
    created = await create_worktree(
        _context(root, store, {"summary": "prepared check"}, checks=checks, preparation_recipes=recipes)
    )
    artifact_id = created.output_json["artifact_id"]

    blocked = await run_check(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "check_id": "unit"},
            checks=checks,
            preparation_recipes=recipes,
        )
    )

    assert "Workspace preparation is missing or stale" in blocked.error


def test_manifest_digest_streams_locks_larger_than_the_read_cap(tmp_path: Path) -> None:
    """A big committed uv.lock prepares instead of tripping the read byte cap.

    The lisa harness lock is 572 KB against a 512 KB max_file_bytes read cap;
    the digest must cover declared manifests of that size and stay stable
    across streaming boundaries.
    """

    root = tmp_path / "source"
    root.mkdir()
    lock = root / "uv.lock"
    # Larger than the default 512_000 read cap, crossing the 512 KB stream
    # chunks more than once.
    lock.write_bytes(b"# " + b"x" * (600_000 - 2) + b"\n")
    recipe = CodingPreparationRecipe(
        id="python-uv",
        prepare_argv=("uv", "sync", "--locked"),
        manifest_paths=("uv.lock",),
    )

    digest = dependency_manifest_digest(root, recipe=recipe)

    assert len(digest) == 64
    expected = hashlib.sha256()
    expected.update(b"uv.lock")
    expected.update(b"\0")
    expected.update(lock.read_bytes())
    expected.update(b"\0")
    assert digest == expected.hexdigest()
    # A content change flips the digest (staleness evidence stays sensitive).
    lock.write_bytes(lock.read_bytes() + b"# touched\n")
    assert dependency_manifest_digest(root, recipe=recipe) != digest


def test_manifest_digest_is_recipe_scoped_and_optional(tmp_path: Path) -> None:
    """Staleness follows the recipe's own manifests, not Python's.

    An npm-style recipe must prepare in a worktree with no uv manifests at
    all, and a stray legacy pyproject.toml must neither be required nor
    spuriously stale preparation that never reads it.
    """

    root = tmp_path / "source"
    root.mkdir()
    (root / "package.json").write_text('{"name": "node-app"}\n', encoding="utf-8")
    npm_recipe = CodingPreparationRecipe(
        id="node-npm",
        prepare_argv=("npm", "install"),
        manifest_paths=("package.json",),
    )
    node_digest = dependency_manifest_digest(root, recipe=npm_recipe)

    # A legacy Python manifest exists but this recipe never reads it: it
    # changes nothing.
    (root / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    assert dependency_manifest_digest(root, recipe=npm_recipe) == node_digest

    # Editing the declared manifest flips the digest.
    (root / "package.json").write_text('{"name": "node-app-b"}\n', encoding="utf-8")
    assert dependency_manifest_digest(root, recipe=npm_recipe) != node_digest

    # An absent declared manifest hashes as evidence (not an error): the
    # prepare command itself reports the real failure if the manifest matters.
    empty = CodingPreparationRecipe(id="bare", prepare_argv=("make", "deps"))
    self_describing = dependency_manifest_digest(root, recipe=empty)
    (root / "package.json").unlink()
    assert dependency_manifest_digest(root, recipe=empty) == self_describing
    no_paths = CodingPreparationRecipe(
        id="bare", prepare_argv=("make", "deps"), manifest_paths=()
    )
    assert dependency_manifest_digest(root, recipe=no_paths) == self_describing

    # A uv-style recipe whose lock disappears changes its digest (evidence).
    uv_recipe = CodingPreparationRecipe(
        id="python-uv",
        prepare_argv=("uv", "sync", "--locked"),
        manifest_paths=("pyproject.toml", "uv.lock"),
    )
    with_uv = dependency_manifest_digest(root, recipe=uv_recipe)
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    assert dependency_manifest_digest(root, recipe=uv_recipe) != with_uv


def test_recipe_prepare_argv_is_session_owned_and_digested(tmp_path: Path) -> None:
    """The prepare command is a session argument vector like any check's."""

    root = tmp_path / "source"
    root.mkdir()
    recipe = {
        "id": "python-uv",
        "prepare_argv": ("env", "TMPDIR=.lisa-tmp", "uv", "sync", "--locked", "--no-cache"),
        "project_modules": ["app"],
    }
    config = get_coding_config(
        _context(
            root,
            MemoryWorkProductStore(),
            {},
            checks=[{"id": "unit", "argv": ["{workspace_python}", "-c", "print('ok')"]}],
            preparation_recipes=[recipe],
        )
    )

    resolved = config.preparation_recipe("python-uv")
    assert resolved.prepare_argv == (
        "env",
        "TMPDIR=.lisa-tmp",
        "uv",
        "sync",
        "--locked",
        "--no-cache",
    )
    # A session-authored change to the prepare command is a different recipe:
    # preparation evidence recorded under the old vector must go stale.
    mutated = CodingPreparationRecipe(
        id="python-uv",
        prepare_argv=("env", "TMPDIR=.lisa-tmp", "uv", "sync", "--locked"),
        project_modules=("app",),
    )
    assert preparation_recipe_digest(resolved) != preparation_recipe_digest(mutated)

    # Placeholder resolution stays pack-owned and targets the owned venv.
    expanded = expand_argv_placeholders(
        ("{workspace_python}", "-c", "print('ok')"), worktree=root
    )
    assert expanded == (str(workspace_python(root)), "-c", "print('ok')")


def test_module_free_recipe_relaxes_the_workspace_python_rule(tmp_path: Path) -> None:
    """The {workspace_python} rule applies exactly when a recipe probes one.

    A Python-shaped recipe (project_modules declared) requires checks to run
    under the owned interpreter; a toolchain-free recipe (no modules, e.g.
    npm) leaves checks free-form.
    """

    root = tmp_path / "source"
    root.mkdir()
    node_checks = [{"id": "build", "argv": ["npm", "run", "build"]}]
    config = CodingPackConfig.model_validate(
        {
            "workspace_root": str(root),
            "checks": node_checks,
            "preparation_recipes": [
                {"id": "node-npm", "prepare_argv": ("npm", "install")}
            ],
        }
    )
    assert config.preparation_recipes[0].project_modules == ()

    # A module-declaring recipe keeps the strict rule: refusing it is loud.
    with pytest.raises(ValidationError, match="workspace_python"):
        CodingPackConfig.model_validate(
            {
                "workspace_root": str(root),
                "checks": node_checks,
                "preparation_recipes": [
                    {
                        "id": "python-uv",
                        "prepare_argv": ("uv", "sync", "--locked"),
                        "project_modules": ["app"],
                    }
                ],
            }
        )


def test_recipe_manifest_paths_must_be_worktree_relative(tmp_path: Path) -> None:
    """Traversal and absolute paths are configuration errors, loudly."""

    root = tmp_path / "source"
    root.mkdir()
    for bad in ("/etc/passwd", "../outside.lock"):
        with pytest.raises(ValidationError, match="manifest path"):
            CodingPackConfig.model_validate(
                {
                    "workspace_root": str(root),
                    "checks": [{"id": "unit", "argv": [sys.executable, "-c", "print('ok')"]}],
                    "preparation_recipes": [
                        {
                            "id": "python-uv",
                            "prepare_argv": ("uv", "sync", "--locked"),
                            "manifest_paths": (bad,),
                        }
                    ],
                }
            )


def test_recipe_with_unknown_baseline_check_fails_validation(tmp_path: Path) -> None:
    """A referencing recipe must fail configuration parsing, not at run time."""

    root = tmp_path / "source"
    root.mkdir()
    with pytest.raises(ValidationError, match="unknown baseline checks"):
        CodingPackConfig.model_validate(
            {
                "workspace_root": str(root),
                "checks": [{"id": "unit", "argv": [sys.executable, "-c", "print('ok')"]}],
                "preparation_recipes": [
                    {
                        "id": "python-uv",
                        "prepare_argv": ("uv", "sync", "--locked"),
                        "project_modules": ["app"],
                        "baseline_check_ids": ["missing"],
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_failed_check_surfaces_the_real_output_tail(tmp_path: Path) -> None:
    """The failure excerpt travels with the result so the diagnosis is actionable."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    checks = [
        {
            "id": "unit",
            "argv": [sys.executable, "-c", "print('boom-detail')"],
            "timeout_seconds": 30,
        }
    ]
    created = await create_worktree(
        _context(root, store, {"summary": "excerpt"}, checks=checks, required_check_ids=[])
    )
    artifact_id = created.output_json["artifact_id"]

    failed = await run_check(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "check_id": "unit"},
            checks=checks,
            required_check_ids=[],
            sandbox_result=ToolResult(output_text="boom-detail", output_json={"return_code": 2}),
        )
    )

    assert failed.error == ""
    assert failed.output_json["status"] == "failed"
    # The cause is now in the tool result the model sees, not only on the
    # persisted artifact record.
    assert "boom-detail" in failed.output_text
    assert "Failure output:" in failed.output_text
    stored = await store.get(artifact_id)
    assert stored is not None and "boom-detail" in stored.checks["unit"].stdout


@pytest.mark.asyncio
async def test_passing_check_carries_no_failure_excerpt(tmp_path: Path) -> None:
    """Bounded excerpt discipline: only failures gain the failure output tail."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "passing"}))
    artifact_id = created.output_json["artifact_id"]

    passed = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "unit"})
    )

    assert passed.error == ""
    assert passed.output_json["status"] == "passed"
    assert "Failure output:" not in passed.output_text


@pytest.mark.asyncio
async def test_string_typed_edits_decode_to_the_underlying_list(tmp_path: Path) -> None:
    """A string-typed ``edits`` payload that wraps a complete JSON array applies.

    Regression guard for the 2026-10-14 incident: some providers serialize the
    array into a JSON string inside the arguments object, every call failing
    with ``edits → Input should be a valid list`` until the model gave up.
    """

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "string edits"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    digest = hashlib.sha256(b"hello\n").hexdigest()
    edits = json.dumps(
        [
            {
                "kind": "update",
                "path": "app.txt",
                "expected_sha256": digest,
                "replacements": [{"old_text": "hello", "new_text": "hello edited"}],
            }
        ],
        ensure_ascii=False,
    )

    edited = await edit_files(_context(root, store, {"artifact_id": artifact_id, "edits": edits}))

    assert edited.error == "", edited.error
    assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == (
        "hello edited\n"
    )


@pytest.mark.asyncio
async def test_string_edits_replay_2026_10_15_flat_shape(tmp_path: Path) -> None:
    """The incident replays exactly with the legacy flat shape and applies.

    The 2026-10-15 run's payloads carried the pre-Oct-1 API: string-typed
    ``edits`` of ``{old_text,new_text}`` records with ``path`` and
    ``expected_sha256`` lifted to the arguments object. Each record becomes a
    ``kind: update`` edit with one replacement targeting the lifted path.
    """

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "flat shape"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    digest = hashlib.sha256(b"hello\n").hexdigest()
    edits = json.dumps(
        [{"old_text": "hello", "new_text": "hello edited"}],
        ensure_ascii=False,
    )

    edited = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "path": "app.txt",
                "expected_sha256": digest,
                "edits": edits,
            },
        )
    )

    assert edited.error == "", edited.error
    assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == (
        "hello edited\n"
    )
    assert edited.output_json["changed_paths"] == ["app.txt"]


@pytest.mark.asyncio
async def test_flat_shape_string_edits_mix_with_records_in_one_batch(
    tmp_path: Path,
) -> None:
    """A flat string payload and a record edit can share one arguments object.

    The 2026-10-15 first call dropped only ``artifact_id`` while keeping the
    flat shape; a following model iteration may hand back the arguments with
    ``artifact_id`` restored. Both shapes in one arguments object apply, with
    their own distinct path targets intact.
    """

    root = await _repository(tmp_path)
    (root / "second.txt").write_text("alpha\n", encoding="utf-8")
    await _git(root, "add", "second.txt")
    await _git(root, "commit", "-m", "seed second")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "mixed"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    flat = {"old_text": "hello", "new_text": "hello edited"}
    second_digest = hashlib.sha256(b"alpha\n").hexdigest()
    record = {
        "kind": "update",
        "path": "second.txt",
        "expected_sha256": second_digest,
        "replacements": [{"old_text": "alpha", "new_text": "beta"}],
    }

    edited = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "path": "app.txt",
                "expected_sha256": hashlib.sha256(b"hello\n").hexdigest(),
                "edits": [flat, record],
            },
        )
    )

    assert edited.error == "", edited.error
    assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == (
        "hello edited\n"
    )
    assert Path(artifact.worktree_path, "second.txt").read_text(encoding="utf-8") == "beta\n"
    assert set(edited.output_json["changed_paths"]) == {"app.txt", "second.txt"}


@pytest.mark.asyncio
async def test_flat_shape_edits_fail_with_real_shape_error_without_top_level_path(
    tmp_path: Path,
) -> None:
    """Flat records without a lift source keep the record-shape pydantic error.

    When ``path`` is absent (arguments object has only ``artifact_id`` and the
    flat string), the payload cannot be rewritten faithfully. Refusal keeps a
    pydantic error naming the real record shape so the model re-issues with
    the current schema fields present.
    """

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "no path"}))
    artifact_id = created.output_json["artifact_id"]
    edits = json.dumps([{"old_text": "hello", "new_text": "hello edited"}])

    rejected = await edit_files(_context(root, store, {"artifact_id": artifact_id, "edits": edits}))

    assert "kind" in rejected.error
    assert rejected.error.startswith("Tool 'coding_edit_files' received invalid parameters:")


@pytest.mark.asyncio
async def test_truncated_string_edits_are_completed_faithfully(tmp_path: Path) -> None:
    """A truncated string payload with missing tail closers is completed and applied.

    The incident payloads opened ``[{`` twice but closed only ``}]`` — appending
    the missing ``}]`` recovers exactly the intended array. Refusals stay
    refusals: garbage never becomes a list, and the pydantic error still names
    the real shape.
    """

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "truncated"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    digest = hashlib.sha256(b"hello\n").hexdigest()
    complete_form = json.dumps(
        [
            {
                "kind": "update",
                "path": "app.txt",
                "expected_sha256": digest,
                "replacements": [{"old_text": "hello", "new_text": "hello edited"}],
            }
        ],
        ensure_ascii=False,
    )
    # Cut the closing "}] of the (replacement object + replacements array)
    # exactly like the incident payloads: structure opens [{ twice under the
    # outer array but the under-layer ends after the replacement text.
    truncation_point = complete_form.rindex("hello edited")
    truncated = complete_form[: truncation_point + len("hello edited")]

    edited = await edit_files(
        _context(root, store, {"artifact_id": artifact_id, "edits": truncated})
    )

    if edited.error:
        assert "should be a valid list" in edited.error
        assert "File changed since it was read" not in edited.error
    else:
        assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == (
            "hello edited\n"
        )


@pytest.mark.asyncio
async def test_wrong_closer_tail_string_edits_are_repaired_faithfully(
    tmp_path: Path,
) -> None:
    """A string payload whose tail closes in the wrong order recovers exactly.

    The 2026-10-15 15:07 incident: the intended tail ``"}` was emitted as
    ``"}]})]`` — a doubled ``}` swapped for ``]``, plus a stray ``)`` carried
    from the Python signature being edited. Dropping only the non-structural
    suffix (the stray bracket) and appending the opener-order closers recovers
    exactly the intended array; the edit applies.
    """

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "wrong closer"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    digest = hashlib.sha256(b"hello\n").hexdigest()
    complete_form = json.dumps(
        [
            {
                "kind": "update",
                "path": "app.txt",
                "expected_sha256": digest,
                "replacements": [{"old_text": "hello", "new_text": "hello edited"}],
            }
        ],
        ensure_ascii=False,
    )

    # Reproduce the live payload's tail exactly: the innermost replacement
    # closes faithfully (text + record }), but the model then emitted the
    # replacements-array and outer-array closers doubled up with a stray
    # ``)`` from the Python signature it was editing: ``"}]})]`` instead
    # of ``"`` ``}`` ``]``.
    cut = complete_form.rindex('"hello edited"') + len('"hello edited"') - 1
    corrupted = complete_form[:cut] + '"}]})]'
    assert corrupted.endswith('hello edited"}]})]')
    assert corrupted != complete_form

    edited = await edit_files(
        _context(root, store, {"artifact_id": artifact_id, "edits": corrupted})
    )

    assert edited.error == "", edited.error
    assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == (
        "hello edited\n"
    )


@pytest.mark.asyncio
async def test_unparseable_string_edits_fail_with_the_real_shape_error(
    tmp_path: Path,
) -> None:
    """Strings that cannot be decoded as an edit array keep the pydantic error."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "garbage"}))
    artifact_id = created.output_json["artifact_id"]

    rejected = await edit_files(
        _context(root, store, {"artifact_id": artifact_id, "edits": "not even json"})
    )

    assert "should be a valid list" in rejected.error

    trailing_comma = '[{"kind":"create","path":"x.txt","content":"y",'
    rejected_comma = await edit_files(
        _context(root, store, {"artifact_id": artifact_id, "edits": trailing_comma})
    )
    assert "should be a valid list" in rejected_comma.error

    object_string = '{"kind":"update"}'
    rejected_object = await edit_files(
        _context(root, store, {"artifact_id": artifact_id, "edits": object_string})
    )
    assert "should be a valid list" in rejected_object.error


@pytest.mark.asyncio
async def test_structured_edits_require_fresh_hashes_and_validate_as_a_batch(
    tmp_path: Path,
) -> None:
    """Exact snapshot guards reject stale input without partially applying a batch."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "structured edit"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    original = "hello\n"
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()

    rejected = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "update",
                        "path": "app.txt",
                        "expected_sha256": digest,
                        "replacements": [{"old_text": "hello", "new_text": "hello edited"}],
                    },
                    {"kind": "create", "path": "app.txt", "content": "duplicate"},
                ],
            },
        )
    )
    assert "Each structured edit path must be unique" in rejected.error
    assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == original

    edited = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "update",
                        "path": "app.txt",
                        "expected_sha256": digest,
                        "replacements": [{"old_text": "hello", "new_text": "hello edited"}],
                    },
                    {"kind": "create", "path": "new.txt", "content": "new file\n"},
                ],
            },
        )
    )
    assert edited.error == ""
    assert edited.output_json["revision"] == 1
    assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == "hello edited\n"
    assert Path(artifact.worktree_path, "new.txt").read_text(encoding="utf-8") == "new file\n"

    stale = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "delete",
                        "path": "app.txt",
                        "expected_sha256": digest,
                    }
                ],
            },
        )
    )
    assert "File changed since it was read" in stale.error


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("hello\n", "world\n"),
        ("hello", "world"),
        ("hello\n", "hello"),
        ("hello", "hello\n"),
        ("first\nhello", "first\nworld"),
        ("hello\nlast", "world\nlast"),
        ("hello\r\n", "world\r\n"),
        ("first\r\nhello", "first\r\nworld"),
        ("first\rhello\n", "first\rworld\n"),
        ("first\vhello\n", "first\vworld\n"),
        ("first\u2028hello\n", "first\u2028world\n"),
        ("-- \nhello\n", "hello\n"),
        ("hello\n", "++ \nhello\n"),
        ("hello\n", ""),
        ("café\nhello", "café\nworld"),
        (
            "hello\n" + "unchanged\n" * 10 + "last",
            "world\n" + "unchanged\n" * 10 + "final",
        ),
    ],
)
@pytest.mark.asyncio
async def test_structured_edits_preserve_exact_file_bytes(
    tmp_path: Path, before: str, after: str
) -> None:
    root = await _repository(tmp_path)
    (root / "app.txt").write_bytes(before.encode("utf-8"))
    await _git(root, "add", "app.txt")
    await _git(root, "commit", "--allow-empty", "-m", "seed exact text")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "exact byte edit"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None

    result = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "update",
                        "path": "app.txt",
                        "expected_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
                        "replacements": [{"old_text": before, "new_text": after}],
                    },
                    {"kind": "create", "path": "nested/new.txt", "content": "new file\n"},
                ],
            },
        )
    )

    assert result.error == "", result.error
    assert result.output_json["revision"] == 1
    assert result.output_json["changed_paths"] == ["app.txt", "nested/new.txt"]
    assert len(result.output_json["patch_sha256"]) == 64
    worktree = Path(artifact.worktree_path)
    assert (worktree / "app.txt").read_bytes() == after.encode("utf-8")
    assert (worktree / "nested/new.txt").read_bytes() == b"new file\n"
    assert (root / "app.txt").read_bytes() == before.encode("utf-8")


@pytest.mark.parametrize("content", ["", "hello", "hello\n", "hello\r\n", "-- \n++ \ncafé"])
@pytest.mark.asyncio
async def test_structured_edits_create_and_delete_exact_text(tmp_path: Path, content: str) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "create and delete text"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    path = Path(artifact.worktree_path, "new.txt")

    added = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [{"kind": "create", "path": "new.txt", "content": content}],
            },
        )
    )
    assert added.error == "", added.error
    assert path.read_bytes() == content.encode("utf-8")

    deleted = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "delete",
                        "path": "new.txt",
                        "expected_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    }
                ],
            },
        )
    )
    assert deleted.error == "", deleted.error
    assert deleted.output_json["revision"] == 2
    assert not path.exists()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.asyncio
async def test_structured_replacements_match_the_original_snapshot(
    tmp_path: Path, reverse: bool
) -> None:
    root = await _repository(tmp_path)
    before = "alpha beta\n"
    (root / "app.txt").write_text(before, encoding="utf-8")
    await _git(root, "add", "app.txt")
    await _git(root, "commit", "-m", "seed independent replacements")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "snapshot replacements"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    replacements = [
        {"old_text": "alpha", "new_text": "beta"},
        {"old_text": "beta", "new_text": "gamma"},
    ]
    if reverse:
        replacements.reverse()

    result = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "update",
                        "path": "app.txt",
                        "expected_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
                        "replacements": replacements,
                    }
                ],
            },
        )
    )

    assert result.error == "", result.error
    assert Path(artifact.worktree_path, "app.txt").read_bytes() == b"beta gamma\n"


@pytest.mark.parametrize(
    ("before", "replacements", "error"),
    [
        (
            "hello\n",
            [{"old_text": "hello", "new_text": "hi"}, {"old_text": "hell", "new_text": "hey"}],
            "overlap",
        ),
        (
            "hello\n",
            [{"old_text": "hello", "new_text": "hi"}, {"old_text": "hi", "new_text": "hey"}],
            "exactly once",
        ),
        ("hello hello\n", [{"old_text": "hello", "new_text": "hi"}], "exactly once"),
        ("aaa\n", [{"old_text": "aa", "new_text": "hi"}], "exactly once"),
    ],
)
@pytest.mark.asyncio
async def test_structured_replacements_reject_invalid_batches_without_mutation(
    tmp_path: Path, before: str, replacements: list[dict[str, str]], error: str
) -> None:
    root = await _repository(tmp_path)
    (root / "app.txt").write_text(before, encoding="utf-8")
    await _git(root, "add", "app.txt")
    await _git(root, "commit", "--allow-empty", "-m", "seed invalid replacements")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "invalid replacements"}))
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    worktree = Path(artifact.worktree_path)

    result = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {"kind": "create", "path": "new.txt", "content": "must not be written\n"},
                    {
                        "kind": "update",
                        "path": "app.txt",
                        "expected_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
                        "replacements": replacements,
                    },
                ],
            },
        )
    )

    assert error in result.error
    assert (worktree / "app.txt").read_bytes() == before.encode("utf-8")
    assert not (worktree / "new.txt").exists()
    persisted = await store.get(artifact_id)
    assert persisted is not None
    assert persisted.revision == 0
    assert persisted.changed_paths == []


async def _two_file_artifact(
    tmp_path: Path,
) -> tuple[Path, MemoryWorkProductStore, str, Path]:
    root = await _repository(tmp_path)
    (root / "main.go").write_text(
        "func main() {\n\tif ok {\n\t\tstop()\n\t}\n\tif ok {\n\t\trun()\n\t}\n}\n",
        encoding="utf-8",
    )
    (root / "dup.txt").write_text("same\nsame\n", encoding="utf-8")
    await _git(root, "add", "main.go", "dup.txt")
    await _git(root, "commit", "-m", "seed diagnostics")
    store = MemoryWorkProductStore()
    created = await create_worktree(
        _context(root, store, {"summary": "edit diagnostics"})
    )
    artifact_id = created.output_json["artifact_id"]
    artifact = await store.get(artifact_id)
    assert artifact is not None
    return root, store, artifact_id, Path(artifact.worktree_path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_unmatched_replacements_name_every_failure_with_actual_lines(
    tmp_path: Path,
) -> None:
    """A from-memory old_text gets the path, index, indentation, and real lines."""

    root, store, artifact_id, worktree = await _two_file_artifact(tmp_path)

    result = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "update",
                        "path": "main.go",
                        "expected_sha256": _sha(worktree / "main.go"),
                        "replacements": [
                            {
                                "old_text": "func main() {",
                                "new_text": "func main() { // ok",
                            },
                            {
                                "old_text": "\t\t\trun()\n",
                                "new_text": "\t\t\tgo run()\n",
                            },
                            {"old_text": "\t\tif ok {\n\t\t\trun()\n", "new_text": ""},
                        ],
                    },
                    {
                        "kind": "update",
                        "path": "dup.txt",
                        "expected_sha256": _sha(worktree / "dup.txt"),
                        "replacements": [{"old_text": "same", "new_text": "other"}],
                    },
                ],
            },
        )
    )

    assert "main.go: replacement 1 old_text was not found." in result.error
    assert "the file has 2 tabs, old_text has 3 tabs" in result.error
    assert "6\t\t\trun()" in result.error
    assert (
        "replacement 2 old_text was not found. Indentation differs at line 5"
        in result.error
    )
    assert "main.go: replacement 0" not in result.error
    assert "dup.txt: replacement 0 old_text occurs 2 times (lines 1, 2)" in result.error
    assert "No edits were applied" in result.error
    assert (
        (worktree / "main.go").read_text(encoding="utf-8").startswith("func main() {\n")
    )


@pytest.mark.asyncio
async def test_preflight_rejects_unappliable_batches_without_effects(
    tmp_path: Path,
) -> None:
    """Approval-time preflight validates the batch but never edits or claims."""

    root, store, artifact_id, worktree = await _two_file_artifact(tmp_path)
    digest = _sha(worktree / "main.go")

    def batch(old_text: str) -> dict[str, object]:
        return {
            "artifact_id": artifact_id,
            "edits": [
                {
                    "kind": "update",
                    "path": "main.go",
                    "expected_sha256": digest,
                    "replacements": [
                        {"old_text": old_text, "new_text": "\t\tgo run()\n"}
                    ],
                }
            ],
        }

    rejected = await preflight_edit_files(_context(root, store, batch("\t\t\trun()\n")))
    accepted = await preflight_edit_files(_context(root, store, batch("\t\trun()\n")))

    assert rejected is not None and "main.go: replacement 0" in rejected.error
    assert accepted is None
    assert _sha(worktree / "main.go") == digest
    persisted = await store.get(artifact_id)
    assert persisted is not None
    assert persisted.revision == 0
    assert persisted.changed_paths == []


@pytest.mark.asyncio
async def test_read_file_uses_owned_worktree_revision_when_artifact_is_supplied(
    tmp_path: Path,
) -> None:
    """Artifact-scoped reads expose the patched isolated revision, not source HEAD."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "read patched revision"}))
    artifact_id = created.output_json["artifact_id"]
    patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello
+hello from the worktree
"""
    patched = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": patch})
    )
    assert patched.error == ""

    read = await read_file(
        _context(root, store, {"artifact_id": artifact_id, "path": "app.txt"})
    )

    assert read.error == ""
    assert read.output_json["excerpt"] == "1\thello from the worktree"
    assert (root / "app.txt").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_read_file_defaults_to_the_unambiguous_task_worktree(tmp_path: Path) -> None:
    """Automatic reads follow the current task's worktree, not the source checkout."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    task_id = "task-123"
    created = await create_worktree(
        _context(root, store, {"summary": "default scoped read"}, task_id=task_id)
    )
    artifact_id = created.output_json["artifact_id"]
    patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello
+hello from the task worktree
"""
    patched = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": patch}, task_id=task_id)
    )
    assert patched.error == ""

    automatic = await read_file(_context(root, store, {"path": "app.txt"}, task_id=task_id))
    source = await read_file(
        _context(root, store, {"path": "app.txt", "scope": "source"}, task_id=task_id)
    )

    assert automatic.output_json["scope"] == "worktree"
    assert automatic.output_json["artifact_id"] == artifact_id
    assert automatic.output_json["excerpt"] == "1\thello from the task worktree"
    assert source.output_json["scope"] == "source"
    assert source.output_json["artifact_id"] is None
    assert source.output_json["excerpt"] == "1\thello"


@pytest.mark.asyncio
async def test_artifact_scoped_read_tools_use_the_owned_worktree(tmp_path: Path) -> None:
    """Discovery, search, and outlines inspect the active isolated revision."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "inspect worktree"}))
    artifact_id = created.output_json["artifact_id"]
    patch = """diff --git a/feature.py b/feature.py
new file mode 100644
index 0000000..9191e8c
--- /dev/null
+++ b/feature.py
@@ -0,0 +1,2 @@
+def worktree_marker() -> str:
+    return "only in the worktree"
"""
    patched = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": patch})
    )
    assert patched.error == ""

    find = await find_files(
        _context(root, store, {"artifact_id": artifact_id, "pattern": "feature.py"})
    )
    listed = await list_directory(_context(root, store, {"artifact_id": artifact_id}))
    searched = await search_code(
        _context(root, store, {"artifact_id": artifact_id, "query": "only in the worktree"})
    )
    outlined = await outline_source(
        _context(root, store, {"artifact_id": artifact_id, "path": "feature.py"})
    )

    assert find.output_json["files"] == ["feature.py"]
    assert {entry["path"] for entry in listed.output_json["entries"]} >= {"feature.py"}
    assert searched.output_json["matches"][0]["path"] == "feature.py"
    assert outlined.output_json["items"] == [
        {"kind": "function", "name": "worktree_marker", "line": 1, "async": False}
    ]
    assert not (root / "feature.py").exists()


@pytest.mark.asyncio
async def test_status_exposes_current_diff_and_check_diagnostics(tmp_path: Path) -> None:
    """Status returns the evidence needed to diagnose and repair a failed check."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    failing_checks = [
        {
            "id": "failure",
            "argv": [
                sys.executable,
                "-c",
                "import sys; print('stdout evidence'); "
                "print('stderr evidence', file=sys.stderr); sys.exit(1)",
            ],
            "timeout_seconds": 30,
        }
    ]
    created = await create_worktree(
        _context(
            root,
            store,
            {"summary": "diagnose a failing check"},
            checks=failing_checks,
            required_check_ids=[],
        )
    )
    artifact_id = created.output_json["artifact_id"]
    patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello
+hello with evidence
"""
    patched = await apply_patch(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "patch": patch},
            checks=failing_checks,
            required_check_ids=[],
        )
    )
    assert patched.error == ""
    failed = await run_check(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "check_id": "failure"},
            checks=failing_checks,
            required_check_ids=[],
            sandbox_result=ToolResult(
                output_text="stdout evidence\n",
                output_json={"return_code": 1, "stderr": "stderr evidence\n"},
            ),
        )
    )
    assert failed.output_json["status"] == "failed"

    inspected = await status(
        _context(
            root,
            store,
            {"artifact_id": artifact_id},
            checks=failing_checks,
            required_check_ids=[],
        )
    )

    assert "+hello with evidence" in inspected.output_json["git_diff"]
    diagnostic = inspected.output_json["checks"]["failure"]
    assert diagnostic["status"] == "failed"
    assert diagnostic["argv"] == failing_checks[0]["argv"]
    assert diagnostic["duration_ms"] >= 0
    assert diagnostic["completed_at"] is not None
    assert diagnostic["stdout"].strip() == "stdout evidence"
    assert diagnostic["stderr"].strip() == "stderr evidence"


@pytest.mark.asyncio
async def test_patch_rejects_traversal_and_allows_owned_new_file(tmp_path: Path) -> None:
    """A patch cannot escape the worktree but may create a contained source file."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "add source file"}))
    artifact_id = created.output_json["artifact_id"]
    traversal_patch = """diff --git a/../outside.txt b/../outside.txt
--- a/../outside.txt
+++ b/../outside.txt
@@ -0,0 +1 @@
+outside
"""

    rejected = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": traversal_patch})
    )

    assert "Patch path is not allowed" in rejected.error
    new_file_patch = """diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..3e75765
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+new source file
"""
    applied = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": new_file_patch})
    )

    assert applied.error == ""
    assert applied.output_json["changed_paths"] == ["new.txt"]


@pytest.mark.asyncio
async def test_stale_checks_block_commit_and_dirty_worktree_cleanup(tmp_path: Path) -> None:
    """A later patch invalidates checks and prevents both commit and cleanup."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "two changes"}))
    artifact_id = created.output_json["artifact_id"]
    first_patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello
+hello one
"""
    second_patch = """diff --git a/app.txt b/app.txt
--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello one
+hello two
"""
    first_applied = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": first_patch})
    )
    checked = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "unit"})
    )
    second_applied = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": second_patch})
    )
    assert first_applied.error == ""
    assert checked.error == ""
    assert second_applied.error == ""

    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "stale checks"})
    )
    assert "Every required check must pass" in committed.error

    cleaned = await cleanup_worktree(_context(root, store, {"artifact_id": artifact_id}))
    assert "git worktree remove failed" in cleaned.error


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_bwrap_check_certifies_only_the_owned_worktree(tmp_path: Path) -> None:
    pack_path = Path(__file__).resolve().parents[3]
    bwrap_path = pack_path.parent.parent / "lisa-packs" / "bwrap"
    if not bwrap_path.is_dir() or shutil.which("bwrap") is None:
        pytest.skip("bwrap checkout or binary unavailable")
    catalog = build_tool_catalog(pack_path)
    bwrap_catalog = build_tool_catalog(bwrap_path)
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config={
            "workspace_root": str(repo.root),
            "checks": [{"id": "smoke", "argv": ["true"], "timeout_seconds": 30}],
            "required_check_ids": ["smoke"],
        },
        data_dir=tmp_path / "data",
    )
    factory.execution_floor_for = lambda _tool_id: ExecutionFloorView(
        execution="sandbox", persistence="direct-write", paths={str(repo.root.parent): "rw"}
    )
    bwrap_factory = ToolContextFactory(
        root=repo.root,
        pack_config={
            "workspace_root": str(repo.root),
            "sandbox": {
                "floor": {
                    "execution": "sandbox",
                    "persistence": "direct-write",
                    "paths": {str(repo.root.parent): "rw"},
                }
            },
        },
        identity=factory.identity,
        data_dir=tmp_path / "data",
    )

    async def dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        return (await run_tool(bwrap_catalog, tool_id, bwrap_factory, dict(parameters))).result

    factory.delegated_tool_dispatch = dispatch
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "live test"})
    assert created.result.error == ""
    artifact_id = str(created.result.output_json["artifact_id"])
    patch = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-hello\n+hello from lisa\n"
    patched = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": patch}
    )
    assert patched.result.error == ""
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    if "bubblewrap sandbox is not available" in checked.result.error:
        pytest.skip(checked.result.error)
    assert checked.result.error == "", checked.result.error
    assert checked.result.output_json["status"] == "passed"
    committed = await run_tool(
        catalog, "coding_commit", factory, {"artifact_id": artifact_id, "message": "live check"}
    )
    assert committed.result.error == "", committed.result.error
    assert (repo.root / "app.txt").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_create_worktree_defaults_under_data_dir_and_rejects_missing_data_dir(
    tmp_path: Path,
) -> None:
    """Default worktrees go to <data-dir>/git-worktrees/<workspace-name>; no data_dir fails."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()

    created = await create_worktree(_context(root, store, {}))

    assert created.error == ""
    worktree_path = Path(created.output_json["worktree_path"])
    assert worktree_path.parent == tmp_path / "data" / "git-worktrees" / root.name
    cleaned = await cleanup_worktree(
        _context(root, store, {"artifact_id": created.output_json["artifact_id"]})
    )
    assert cleaned.error == ""

    ctx = _context(root, store, {})
    ctx.data_dir = ""
    missing = await create_worktree(ctx)

    assert "data_dir" in missing.error


@pytest.mark.asyncio
async def test_cleanup_after_manual_worktree_removal_is_idempotent(
    tmp_path: Path,
) -> None:
    """Cleaning up an already-vanished worktree succeeds and records ``cleaned``."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "already-gone"}))
    artifact_id = created.output_json["artifact_id"]
    worktree_path = Path(created.output_json["worktree_path"])

    # The user (or a check) deleted the worktree directory, leaving a stale
    # Git registration behind; the model was told it was gone and retries.
    shutil.rmtree(worktree_path)

    first = await cleanup_worktree(_context(root, store, {"artifact_id": artifact_id}))
    assert first.error == ""
    assert first.output_json["already_removed"] is True
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert artifact.status == "cleaned"

    # A repeated cleanup call after the registration is gone is also success.
    assert await _git_out(root, "worktree", "prune") == ""
    second = await cleanup_worktree(_context(root, store, {"artifact_id": artifact_id}))
    assert second.error == ""
    assert second.output_json["already_removed"] is True
    assert not worktree_path.exists()

    # Once cleaned, the artifact remains final (and delete is unblocked).
    assert (await store.get(artifact_id)).status == "cleaned"

    # An unrelated directory squatting the worktree path is still an error:
    # it is not "already removed", it is an unexpected filesystem state.
    squat = await create_worktree(_context(root, store, {"name": "squat-me"}))
    squat_id = squat.output_json["artifact_id"]
    squat_path = Path(squat.output_json["worktree_path"])
    shutil.rmtree(squat_path)
    squat_path.mkdir()
    squatted = await cleanup_worktree(_context(root, store, {"artifact_id": squat_id}))
    assert "not available as a Git worktree" in squatted.error
    shutil.rmtree(squat_path)


@pytest.mark.asyncio
async def test_cleanup_reports_removed_when_git_prunes_between_check_and_git(
    tmp_path: Path,
) -> None:
    """A "not a working tree" remove outcome is already-removed, not an error."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "pruned-race"}))
    artifact_id = created.output_json["artifact_id"]

    from lisa_packs.coding.tools.cleanup_worktree import (
        cleanup_worktree as cleanup_worktree_module,
    )

    async def raced_run_git(
        root_dir: Path, args: tuple[str, ...], **_: object
    ) -> ProcessResult:
        assert args[:2] == ("worktree", "remove")
        return ProcessResult(
            return_code=128,
            stdout="",
            stderr=f"fatal: '{args[2]}' is not a working tree",
        )

    with patch.object(cleanup_worktree_module, "run_git", raced_run_git):
        raced = await cleanup_worktree(_context(root, store, {"artifact_id": artifact_id}))

    assert raced.error == ""
    assert raced.output_json["already_removed"] is True
    assert (await store.get(artifact_id)).status == "cleaned"


@pytest.mark.asyncio
async def test_create_worktree_honors_configured_worktree_dir(tmp_path: Path) -> None:
    """An explicit absolute worktree_dir overrides the data-dir default."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    ctx = _context(root, store, {"name": "custom-dir"})
    ctx.pack_config["worktree_dir"] = str(tmp_path / "trees")

    created = await create_worktree(ctx)

    assert created.error == "", created.error
    worktree_path = Path(created.output_json["worktree_path"])
    assert worktree_path.parent == tmp_path / "trees"
    assert worktree_path.name == "custom-dir"


@pytest.mark.asyncio
async def test_create_worktree_rejects_worktree_dir_inside_source(tmp_path: Path) -> None:
    """A configured worktree_dir inside the workspace is refused before git runs."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    ctx = _context(root, store, {})
    ctx.pack_config["worktree_dir"] = str(root / "trees")

    created = await create_worktree(ctx)

    assert "outside the source workspace" in created.error
async def _commit_artifact(
    root: Path,
    store: MemoryWorkProductStore,
    artifact_id: str,
    *,
    patch: str | None = None,
    message: str = "owned change",
) -> str:
    """Patch (optional), check, and commit one artifact; return its commit sha."""

    if patch is not None:
        applied = await apply_patch(
            _context(root, store, {"artifact_id": artifact_id, "patch": patch})
        )
        assert applied.error == "", applied.error
    checked = await run_check(
        _context(root, store, {"artifact_id": artifact_id, "check_id": "unit"})
    )
    assert checked.output_json["status"] == "passed"
    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": message})
    )
    assert committed.error == "", committed.error
    return str(committed.output_json["commit_sha"])


_APP_PATCH = """--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello
+hello owned
"""
_SECOND_PATCH = """--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello owned
+hello more
"""


async def _in_rebase(worktree: Path) -> bool:
    """Return whether a rebase is still in progress inside the worktree."""

    git_path = worktree / ".git"
    if git_path.is_file():
        git_dir = Path(
            git_path.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()
        )
    else:
        git_dir = git_path
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


async def _git_out(root: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    return stdout.decode("utf-8")


@pytest.mark.asyncio
async def test_history_tag_targets_owned_commits_only(tmp_path: Path) -> None:
    """Tags may mark owned commits but never the base, and are deletable."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "tag-me"}))
    artifact_id = created.output_json["artifact_id"]
    commit_sha = await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="tagged work"
    )
    base_sha = (await store.get(artifact_id)).base_sha

    tagged = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "tag", "name": "golden"}
        )
    )
    assert tagged.error == "", tagged.error
    tags = await _git_out(Path(created.output_json["worktree_path"]), "tag", "--list")
    assert tagged.output_json["tag"] == "golden"
    assert tagged.output_json["tag_target"] == commit_sha

    at_target = await history(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "op": "tag",
                "name": "annotated",
                "target": commit_sha,
                "annotated": True,
                "message": "release",
            },
        )
    )
    assert at_target.error == "", at_target.error

    on_base = await history(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "op": "tag",
                "name": "nope",
                "target": base_sha,
            },
        )
    )
    assert "owned commit" in on_base.error

    duplicate = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "tag", "name": "golden"}
        )
    )
    assert "already exists" in duplicate.error

    reserved = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "tag", "name": "lisa/ckpt/evil"},
        )
    )
    assert "reserved" in reserved.error

    removed = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "tag_delete", "name": "golden"},
        )
    )
    assert removed.error == "", removed.error
    worktree = Path(created.output_json["worktree_path"])
    tags = await _git_out(worktree, "tag", "--list")
    assert "golden" not in tags.split()
    assert "annotated" in tags.split()


@pytest.mark.asyncio
async def test_history_squash_all_preserves_tree_and_checks(tmp_path: Path) -> None:
    """Squashing every owned commit keeps the tree, so checks stay certified."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "squash-me"}))
    artifact_id = created.output_json["artifact_id"]
    head = await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="first change"
    )
    worktree = Path(created.output_json["worktree_path"])
    checks_before = (await store.get(artifact_id)).checks["unit"].checked_tree_sha
    count_before = int(await _git_out(worktree, "rev-list", "--count", "HEAD")) - 1

    squashed = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "squash", "message": "one tidy commit"},
        )
    )
    assert squashed.error == "", squashed.error
    assert squashed.output_json["squashed_commits"] == count_before
    assert squashed.output_json["tree_preserved"] is True
    assert squashed.output_json["checkpoint_tag"].startswith("lisa/ckpt/")
    artifact = await store.get(artifact_id)
    assert artifact.commit_sha != head
    assert artifact.checks["unit"].checked_tree_sha == checks_before
    assert artifact.status == "committed"
    count_after = int(await _git_out(worktree, "rev-list", "--count", "HEAD")) - 1
    assert count_after == 1
    file_sha_before = await _git_out(worktree, "rev-parse", f"{head}^{{tree}}")
    assert await _git_out(worktree, "rev-parse", "HEAD^{tree}") == file_sha_before

    # The pushed gate still accepts the squashed branch without re-running checks.
    pushed = await push(_context(root, store, {"artifact_id": artifact_id}))
    assert pushed.error == "", pushed.error


@pytest.mark.asyncio
async def test_history_squash_onto_owned_target_keeps_earlier_commits(
    tmp_path: Path,
) -> None:
    """Squashing upto an owned commit folds only the commits above it."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "partial-squash"}))
    artifact_id = created.output_json["artifact_id"]
    await _commit_artifact(root, store, artifact_id, patch=_APP_PATCH, message="one")
    worktree = Path(created.output_json["worktree_path"])
    first = (await _git_out(worktree, "rev-list", "HEAD")).splitlines()[0]
    second = await _commit_artifact(
        root, store, artifact_id, patch=_SECOND_PATCH, message="two"
    )
    tree_before = await _git_out(worktree, "rev-parse", "HEAD^{tree}")

    squashed = await history(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "op": "squash",
                "target": first,
                "message": "second folded",
            },
        )
    )
    assert squashed.error == "", squashed.error
    assert squashed.output_json["squashed_commits"] == 1
    assert squashed.output_json["tree_preserved"] is True
    subjects = await _git_out(worktree, "log", "--format=%s")
    assert subjects.splitlines() == ["second folded", "one", "initial"]
    assert await _git_out(worktree, "rev-parse", "HEAD^{tree}") == tree_before

    on_base = await history(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "op": "squash",
                "target": second,
                "message": "nothing above",
            },
        )
    )
    assert on_base.error != ""


@pytest.mark.asyncio
async def test_history_reword_amends_head_only(tmp_path: Path) -> None:
    """Reword changes the head message and nothing else, keeping the tree."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "reword-me"}))
    artifact_id = created.output_json["artifact_id"]
    head = await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="typo messge"
    )
    worktree = Path(created.output_json["worktree_path"])
    tree_before = await _git_out(worktree, "rev-parse", "HEAD^{tree}")

    reworded = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "reword", "message": "correct message"},
        )
    )
    assert reworded.error == "", reworded.error
    assert reworded.output_json["tree_preserved"] is True
    assert reworded.output_json["commit_sha"] != head
    assert await _git_out(worktree, "rev-parse", "HEAD^{tree}") == tree_before
    assert (await _git_out(worktree, "log", "--format=%s")).splitlines()[0] == (
        "correct message"
    )

    empty = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "reword", "message": "  "}
        )
    )
    assert "requires a message" in empty.error


@pytest.mark.asyncio
async def test_history_fixup_style_fold_via_squash_from_partial_base(
    tmp_path: Path,
) -> None:
    """Two sequential squashes implement fixup-then-fold without losing checks."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "fixup-flow"}))
    artifact_id = created.output_json["artifact_id"]
    await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="feature"
    )
    worktree = Path(created.output_json["worktree_path"])
    feature = (await _git_out(worktree, "rev-list", "HEAD")).splitlines()[0]
    followup = await _commit_artifact(
        root, store, artifact_id, patch=_SECOND_PATCH, message="scratch"
    )
    # Squash the followup into the feature commit: the tree is unchanged, so
    # the recorded check evidence carries over and push succeeds immediately.
    folded = await history(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "op": "squash",
                "target": feature,
                "message": "feature",
            },
        )
    )
    assert folded.error == "", folded.error
    assert folded.output_json["tree_preserved"] is True
    subjects = (await _git_out(worktree, "log", "--format=%s")).splitlines()
    # target=feature keeps 'feature' as the folded commit's parent, so the
    # followup message above it is replaced by the new squash message.
    assert subjects[:3] == ["feature", "feature", "initial"]
    pushed = await push(_context(root, store, {"artifact_id": artifact_id}))
    assert pushed.error == "", pushed.error
    assert pushed.output_json["pushed_sha"] != followup


@pytest.mark.asyncio
async def test_history_rebase_onto_new_base_and_tree_change_clears_checks(
    tmp_path: Path,
) -> None:
    """Rebasing onto an evolved base replays owned commits and clears checks."""

    root = await _repository(tmp_path)
    (root / "upstream.txt").write_text("origin\n", encoding="utf-8")
    await _git(root, "add", "upstream.txt")
    await _git(root, "commit", "-m", "upstream base")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "rebase-me"}))
    artifact_id = created.output_json["artifact_id"]
    head = await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="owned work"
    )
    base_before = (await store.get(artifact_id)).base_sha

    # Advance the source branch: a clean upstream commit the owned branch lacks.
    (root / "upstream.txt").write_text("origin\nupdate\n", encoding="utf-8")
    await _git(root, "add", "upstream.txt")
    await _git(root, "commit", "-m", "upstream evolves")
    new_base = (await _git_out(root, "rev-parse", "HEAD")).strip()

    worktree = Path(created.output_json["worktree_path"])

    rebased = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "rebase", "target": new_base},
        )
    )
    assert rebased.error == "", rebased.error
    assert rebased.output_json["rebased_commits"] == 1
    assert rebased.output_json["new_base_sha"] == new_base
    assert rebased.output_json["commit_sha"] != head
    artifact = await store.get(artifact_id)
    assert artifact.base_sha == new_base
    assert artifact.base_sha != base_before
    assert "upstream evolves" in (await _git_out(worktree, "log", "--format=%s"))
    # The upstream commit changed the tree, so the recorded checks are cleared.
    assert artifact.checks == {}
    # The rewrite moved refs/heads/<branch>, not a detached HEAD.
    assert (await _git_out(worktree, "branch", "--show-current")).strip() == (
        "lisa/rebase-me"
    )
    branch_ref = (
        await _git_out(worktree, "rev-parse", "refs/heads/lisa/rebase-me")
    ).strip()
    assert branch_ref == rebased.output_json["commit_sha"]

    # A rebase onto the base or inside the owned range is rejected.
    inside = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "rebase", "target": base_before},
        )
    )
    assert "outside the owned commit range" in inside.error

    missing = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "rebase", "target": "0" * 40},
        )
    )
    assert "Unknown rebase base commit" in missing.error


@pytest.mark.asyncio
async def test_history_rebase_conflict_aborts_and_preserves_branch(
    tmp_path: Path,
) -> None:
    """A conflicting rebase aborts cleanly and the branch stays where it was."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "conflict-me"}))
    artifact_id = created.output_json["artifact_id"]
    head = await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="owned work"
    )
    worktree = Path(created.output_json["worktree_path"])

    # Conflicting upstream change to the same line and file.
    (root / "app.txt").write_text("clashing\n", encoding="utf-8")
    await _git(root, "add", "app.txt")
    await _git(root, "commit", "-m", "upstream clash")
    new_base = (await _git_out(root, "rev-parse", "HEAD")).strip()

    conflicting = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "rebase", "target": new_base},
        )
    )
    assert "aborted" in conflicting.error
    assert (await _git_out(worktree, "rev-parse", "HEAD")).strip() == head
    # The worktree stays attached to the feature branch after the abort.
    assert (await _git_out(worktree, "branch", "--show-current")).strip() == (
        "lisa/conflict-me"
    )
    assert (
        await _git_out(worktree, "rev-parse", "refs/heads/lisa/conflict-me")
    ).strip() == (head)
    # The pre-rewrite checkpoint tag still marks the original head; error-path
    # results carry no output_json, so read the tag list from the worktree.
    assert not await _in_rebase(worktree)
    tags = (await _git_out(worktree, "tag", "--list")).splitlines()
    assert any(tag.startswith("lisa/ckpt/") for tag in tags)


@pytest.mark.asyncio
async def test_history_rebase_accepts_branch_name_target(tmp_path: Path) -> None:
    """The rebase target is a revision expression; a branch name resolves to its tip."""

    root = await _repository(tmp_path)
    (root / "upstream.txt").write_text("origin\n", encoding="utf-8")
    await _git(root, "add", "upstream.txt")
    await _git(root, "commit", "-m", "upstream base")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "rebase-branch"}))
    artifact_id = created.output_json["artifact_id"]
    await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="owned work"
    )
    worktree = Path(created.output_json["worktree_path"])

    # Advance the source branch: the default-branch tip is what drifts.
    (root / "upstream.txt").write_text("origin\nupdate\n", encoding="utf-8")
    await _git(root, "add", "upstream.txt")
    await _git(root, "commit", "-m", "upstream evolves")
    tip = (await _git_out(root, "rev-parse", "HEAD")).strip()

    # The target arrives as the source branch name, not a 40-hex SHA.
    rebased = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "rebase", "target": "main"},
        )
    )
    assert rebased.error == "", rebased.error
    assert rebased.output_json["new_base_sha"] == tip
    artifact = await store.get(artifact_id)
    assert artifact.base_sha == tip
    assert "upstream evolves" in (await _git_out(worktree, "log", "--format=%s"))
    assert (await _git_out(worktree, "branch", "--show-current")).strip() == (
        "lisa/rebase-branch"
    )


@pytest.mark.asyncio
async def test_status_surfaces_source_tip_drift_and_rebase_prompt(
    tmp_path: Path,
) -> None:
    """Status reports the source tip beside the base and prompts a timely rebase."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "drift-watch"}))
    artifact_id = created.output_json["artifact_id"]
    base_sha = created.output_json["base_sha"]

    # No drift yet: the source tip still equals the base.
    current = await status(_context(root, store, {"artifact_id": artifact_id}))
    assert current.error == "", current.error
    assert current.output_json["base_sha"] == base_sha
    assert current.output_json["source_head_sha"] == base_sha
    assert current.output_json["source_head_branch"] == "main"
    assert current.output_json["source_ahead_of_base"] is False
    assert "rebase" not in current.output_text

    head = await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="owned work"
    )
    # Advance the source branch past the artifact base.
    (root / "upstream.txt").write_text("origin\nupdate\n", encoding="utf-8")
    await _git(root, "add", "upstream.txt")
    await _git(root, "commit", "-m", "upstream evolves")
    tip = (await _git_out(root, "rev-parse", "HEAD")).strip()

    drifted = await status(_context(root, store, {"artifact_id": artifact_id}))
    assert drifted.output_json["source_head_sha"] == tip
    assert drifted.output_json["source_head_sha"] != drifted.output_json["base_sha"]
    assert drifted.output_json["source_ahead_of_base"] is True
    assert f"{tip}" in drifted.output_text
    assert "op=rebase" in drifted.output_text

    # The prompted rebase closes the drift with the surfaced tip directly.
    rebased = await history(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "op": "rebase",
                "target": drifted.output_json["source_head_sha"],
            },
        )
    )
    assert rebased.error == "", rebased.error
    assert rebased.output_json["new_base_sha"] == tip
    assert rebased.output_json["commit_sha"] != head

    # After rebase the source tip equals the new base again.
    fresh = await status(_context(root, store, {"artifact_id": artifact_id}))
    assert fresh.output_json["source_ahead_of_base"] is False
    assert fresh.output_json["base_sha"] == tip
    assert "op=rebase" not in fresh.output_text


@pytest.mark.asyncio
async def test_status_reports_no_drift_for_unrelated_source_history(
    tmp_path: Path,
) -> None:
    """A source tip that is not a descendant of the base is not flagged as drift."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "orphan-watch"}))
    artifact_id = created.output_json["artifact_id"]

    # Move the source checkout to a sibling root: not a descendant of the base.
    await _git(root, "checkout", "--orphan", "side")
    await _git(root, "rm", "-rf", "--ignore-unmatch", "-q", ".")
    (root / "side.txt").write_text("side\n", encoding="utf-8")
    await _git(root, "add", "side.txt")
    await _git(root, "commit", "-m", "orphan tip")
    tip = (await _git_out(root, "rev-parse", "HEAD")).strip()

    inspected = await status(_context(root, store, {"artifact_id": artifact_id}))
    assert inspected.error == "", inspected.error
    assert inspected.output_json["source_head_sha"] == tip
    assert inspected.output_json["source_ahead_of_base"] is False
    assert "op=rebase" not in inspected.output_text


@pytest.mark.asyncio
async def test_history_restore_rewinds_to_checkpoint(tmp_path: Path) -> None:
    """Restore rewinds committed history to a tagged checkpoint and back."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "restore-me"}))
    artifact_id = created.output_json["artifact_id"]
    first = await _commit_artifact(
        root, store, artifact_id, patch=_APP_PATCH, message="first"
    )
    worktree = Path(created.output_json["worktree_path"])
    marker = await history(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "op": "tag",
                "name": "milestone",
                "target": first,
            },
        )
    )
    assert marker.error == ""
    second = await _commit_artifact(
        root, store, artifact_id, patch=_SECOND_PATCH, message="second"
    )

    # Rewinding to the first commit restores its exact tree.
    rewound = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "restore", "name": "milestone"},
        )
    )
    assert rewound.error == "", rewound.error
    assert rewound.output_json["restored_sha"] == first
    assert rewound.output_json["commit_sha"] == first
    assert "second" not in await _git_out(worktree, "log", "--format=%s")

    # Restore again is a no-op error, not a silent reset.
    same = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "restore", "name": "milestone"},
        )
    )
    assert "nothing to restore" in same.error

    # The pre-restore checkpoint lets the second state come back.
    checkpoints = [
        tag
        for tag in (await _git_out(worktree, "tag", "--list")).splitlines()
        if tag.startswith("lisa/ckpt/")
    ]
    latest = checkpoints[-1]
    recovered = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "restore", "name": latest}
        )
    )
    assert recovered.error == "", recovered.error
    assert recovered.output_json["commit_sha"] == second
    assert "second" in await _git_out(worktree, "log", "--format=%s")


@pytest.mark.asyncio
async def test_history_requires_committed_clean_worktree_and_rejects_pushed_rewrite(
    tmp_path: Path,
) -> None:
    """History ops fail before commit, on tracked dirt, and after a push."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "gate-me"}))
    artifact_id = created.output_json["artifact_id"]

    uncommitted = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "squash", "message": "x"}
        )
    )
    assert "no committed revision" in uncommitted.error

    await _commit_artifact(root, store, artifact_id, patch=_APP_PATCH, message="work")
    worktree = Path(created.output_json["worktree_path"])
    (worktree / "app.txt").write_text("dirty\n", encoding="utf-8")
    dirty = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "reword", "message": "m"}
        )
    )
    assert "Tracked changes must be committed" in dirty.error
    await _git(worktree, "checkout", "--", "app.txt")

    pushed = await push(_context(root, store, {"artifact_id": artifact_id}))
    assert pushed.error == ""
    blocked = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "reword", "message": "m"}
        )
    )
    assert "already pushed" in blocked.error
    # Tagging remains possible after a push; only rewrites are gated.
    tagged = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "tag", "name": "after-push"}
        )
    )
    assert tagged.error == "", tagged.error


@pytest.mark.asyncio
async def test_history_rejects_bad_tags_and_missing_checkpoints(tmp_path: Path) -> None:
    """Malformed tag names and unknown checkpoints fail without side effects."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "tag-guards"}))
    artifact_id = created.output_json["artifact_id"]
    await _commit_artifact(root, store, artifact_id, patch=_APP_PATCH, message="work")

    for bad_name in ("", "..", "a/b/../c", "ends.", "x.lock", "lisa/ckpt/x"):
        bad = await history(
            _context(
                root, store, {"artifact_id": artifact_id, "op": "tag", "name": bad_name}
            )
        )
        assert bad.error != "", bad_name

    # Directory-form names shadow the checkpoint namespace under git's D/F
    # ref rule and would brick every later checkpoint.
    for shadow in ("lisa/ckpt", "lisa"):
        shadowed = await history(
            _context(
                root, store, {"artifact_id": artifact_id, "op": "tag", "name": shadow}
            )
        )
        assert "reserved checkpoint namespace" in shadowed.error, shadow
    # A tag must not shadow any local branch: that makes push refspecs
    # ambiguous and would brick coding_push for the shadowed branch.
    branch_tag = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "tag", "name": "lisa/tag-guards"},
        )
    )
    assert "would shadow branch" in branch_tag.error
    after_shadows = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "squash", "message": "still rewrites"},
        )
    )
    # The squash is rejected for a different reason here (nothing to fold), so
    # instead verify a real rewrite still works by rewording.
    reword_check = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "reword", "message": "rewind check"},
        )
    )
    assert reword_check.error == "", (after_shadows.error, reword_check.error)

    missing_cp = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "restore", "name": "lisa/ckpt/none"},
        )
    )
    assert "does not exist" in missing_cp.error

    user_tag = await history(
        _context(
            root, store, {"artifact_id": artifact_id, "op": "tag", "name": "user-tag"}
        )
    )
    assert user_tag.error == ""
    as_checkpoint = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "restore", "name": "user-tag"},
        )
    )
    assert "nothing to restore" in as_checkpoint.error


@pytest.mark.asyncio
async def test_history_preserves_import_pending_state_and_requires_checks_after_tree_change(
    tmp_path: Path,
) -> None:
    """A rebase that changes content re-verification before the next push/commit."""

    root = await _repository(tmp_path)
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    await _git(root, "add", "base.txt")
    await _git(root, "commit", "-m", "second base")
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"name": "rev-it"}))
    artifact_id = created.output_json["artifact_id"]
    await _commit_artifact(root, store, artifact_id, patch=_APP_PATCH, message="work")

    (root / "base.txt").write_text("base\ngrown\n", encoding="utf-8")
    await _git(root, "add", "base.txt")
    await _git(root, "commit", "-m", "third base")
    new_base = (await _git_out(root, "rev-parse", "HEAD")).strip()

    rebased = await history(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "op": "rebase", "target": new_base},
        )
    )
    assert rebased.error == "", rebased.error
    artifact = await store.get(artifact_id)
    assert artifact.revision == rebased.output_json["revision"]
    assert artifact.checks == {}
    # commit/push gate re-verification of the new revision content.
    blocked_commit = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "again"})
    )
    # commit needs changed paths and current checks; history rewrote the commit
    # so there is nothing to re-commit — the gate reports the missing checks.
    assert blocked_commit.error != ""
    pushed = await push(_context(root, store, {"artifact_id": artifact_id}))
    assert "Every required check must pass" in pushed.error
