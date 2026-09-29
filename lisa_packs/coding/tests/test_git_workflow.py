"""Temporary-repository integration tests for the coding worktree workflow.

Uses the framework harness's in-memory ``WorkProductStore`` double so the
pack tests never import the host layer (packs may import kernel/sdk only).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

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
from lisa_packs.coding.tools.find_files.find_files import execute as find_files
from lisa_packs.coding.tools.list_directory.list_directory import (
    execute as list_directory,
)
from lisa_packs.coding.tools.outline_source.outline_source import (
    execute as outline_source,
)
from lisa_packs.coding.tools.push.push import execute as push
from lisa_packs.coding.tools.read_file.read_file import execute as read_file
from lisa_packs.coding.tools.run_check.run_check import execute as run_check
from lisa_packs.coding.tools.search_code.search_code import execute as search_code
from lisa_packs.coding.tools.status.status import execute as status


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
    sandbox_result: ToolResult | None = None,
) -> ToolExecutionContext:
    async def dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        assert tool_id == "bwrap_run"
        worktree = str(parameters["cwd"])
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
        parameters=params,
        execution_floor_for=lambda _tool_id: ExecutionFloorView(
            execution="sandbox", persistence="direct-write", paths={str(root.parent): "rw"}
        ),
        delegated_tool_dispatch=dispatch,
        code_change_artifact_store=store,
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
        },
    )


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
    assert read.output_json["excerpt"] == [{"line": 1, "text": "hello from the worktree"}]
    assert (root / "app.txt").read_text(encoding="utf-8") == "hello\n"


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
