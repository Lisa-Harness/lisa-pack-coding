"""Temporary-repository integration tests for the coding worktree workflow.

Uses the framework harness's in-memory ``WorkProductStore`` double so the
pack tests never import the host layer (packs may import kernel/sdk only).
"""

from __future__ import annotations

import asyncio
import hashlib
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
from lisa_packs.coding.tools.edit_files.edit_files import execute as edit_files
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
    preparation_recipes: list[dict[str, object]] | None = None,
    sandbox_result: ToolResult | None = None,
    task_id: str = "",
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
        task_id=task_id,
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
            "preparation_recipes": preparation_recipes
            if preparation_recipes is not None
            else [],
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
    recipes = [{"id": "python-uv", "project_modules": ["app"]}]
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
    assert automatic.output_json["excerpt"] == [
        {"line": 1, "text": "hello from the task worktree"}
    ]
    assert source.output_json["scope"] == "source"
    assert source.output_json["artifact_id"] is None
    assert source.output_json["excerpt"] == [{"line": 1, "text": "hello"}]


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
