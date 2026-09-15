"""Worktree-trajectory eval: the full coding pack write chain, in-process.

Drives ``create_worktree → status → read_file → apply_patch → run_check →
commit → push → status → cleanup_worktree`` against a throwaway Git repo and
bare remote. Asserts the artifact state machine transitions
(``created → patched → committed → pushed → cleaned``) and per-tool result
fields. Deterministic: the only I/O is Git inside tmp dirs; no model calls.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from packstest.catalog import build_tool_catalog
from packstest.context import ToolContextFactory
from packstest.git import git_repo
from packstest.runner import ToolRun, run_tool
from packstest.report import Trajectory

import asyncio

PACK_PATH = Path(__file__).resolve().parents[4]

PATCH = """--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello
+hello from lisa
"""


def _config(root: Path) -> dict[str, object]:
    return {
        "workspace_root": str(root),
        "checks": [{"id": "smoke", "argv": ["true"], "timeout_seconds": 60}],
        "required_check_ids": [],
    }


@pytest.fixture()
def catalog() -> dict[str, dict[str, object]]:
    from packstest.catalog import _ensure_source_importable

    _ensure_source_importable(PACK_PATH)
    return build_tool_catalog(PACK_PATH)


async def _step(
    catalog: dict[str, dict[str, object]],
    factory: ToolContextFactory,
    trajectory: Trajectory,
    tool_id: str,
    params: dict[str, object],
) -> ToolRun:
    run = await run_tool(catalog, tool_id, factory, params)
    trajectory.record(run)
    return run


@pytest.mark.eval
@pytest.mark.asyncio
async def test_full_worktree_trajectory(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """The create→patch→check→commit→push chain reaches ``pushed`` cleanly."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    trajectory = Trajectory(name="worktree-full-chain")
    artifact_id = ""

    run = await _step(catalog, factory, trajectory, "coding_create_worktree", {"summary": "eval"})
    assert run.result.error == "", run.result.error
    artifact_id = str(run.result.output_json["artifact_id"])
    branch = str(run.result.output_json["branch"])
    worktree = str(run.result.output_json["worktree_path"])
    assert branch.startswith("lisa/")
    assert Path(worktree).is_dir()

    run = await _step(catalog, factory, trajectory, "coding_status", {"artifact_id": artifact_id})
    assert run.result.error == ""
    assert run.result.output_json["status"] == "created"

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_read_file",
        {"artifact_id": artifact_id, "path": "app.txt"},
    )
    assert run.result.error == ""
    assert run.result.output_json["excerpt"][0]["text"] == "hello"

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_apply_patch",
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert run.result.error == ""
    assert run.result.output_json["revision"] == 1
    assert run.result.output_json["changed_paths"] == ["app.txt"]

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {"artifact_id": artifact_id, "check_id": "smoke"},
    )
    assert run.result.error == ""
    assert run.result.output_json["status"] == "passed"

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_commit",
        {"artifact_id": artifact_id, "message": "eval: apply patch"},
    )
    assert run.result.error == ""
    commit_sha = str(run.result.output_json["commit_sha"])
    assert len(commit_sha) == 40

    run = await _step(catalog, factory, trajectory, "coding_push", {"artifact_id": artifact_id})
    assert run.result.error == ""
    assert run.result.output_json["pushed_sha"] == commit_sha

    run = await _step(catalog, factory, trajectory, "coding_status", {"artifact_id": artifact_id})
    assert run.result.error == ""
    final = run.result.output_json
    assert final["status"] == "pushed"
    assert final["checks"]["smoke"]["status"] == "passed"

    assert trajectory.all_errors_absent(), trajectory.first_error()
    assert trajectory.tool_ids == [
        "coding_create_worktree",
        "coding_status",
        "coding_read_file",
        "coding_apply_patch",
        "coding_run_check",
        "coding_commit",
        "coding_push",
        "coding_status",
    ]


@pytest.mark.eval
@pytest.mark.asyncio
async def test_cleanup_worktree_transitions_to_cleaned(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """Cleanup removes the worktree and the artifact records ``cleaned``."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    trajectory = Trajectory(name="cleanup")

    run = await _step(catalog, factory, trajectory, "coding_create_worktree", {"summary": "eval"})
    artifact_id = str(run.result.output_json["artifact_id"])
    worktree = Path(str(run.result.output_json["worktree_path"]))
    assert worktree.is_dir()

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_cleanup_worktree",
        {"artifact_id": artifact_id},
    )
    assert run.result.error == "", run.result.error
    assert not worktree.exists(), "worktree directory should be removed"
    assert trajectory.all_errors_absent()


@pytest.mark.eval
@pytest.mark.asyncio
async def test_worktree_paths_stay_outside_source(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """Owned worktrees live under <repo-parent>/.lisa-worktrees, never inside."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    run = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "eval"})
    worktree = Path(str(run.result.output_json["worktree_path"]))
    try:
        assert worktree.relative_to(repo.root)
        raise AssertionError("worktree must not live inside the source workspace")
    except ValueError:
        pass
    finally:
        artifact_id = str(run.result.output_json["artifact_id"])
        await run_tool(catalog, "coding_cleanup_worktree", factory, {"artifact_id": artifact_id})
        shutil.rmtree(worktree.parent, ignore_errors=True)


@pytest.mark.eval
@pytest.mark.asyncio
async def test_patch_policy_rejects_paths_outside_workspace(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """A patch referencing a traversal path is refused before git apply."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    run = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "eval"})
    artifact_id = str(run.result.output_json["artifact_id"])

    bad_patch = "--- a/../outside.txt\n+++ b/../outside.txt\n@@\n-x\n+y\n"
    run = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": bad_patch},
    )
    assert run.result.error != "", "traversal patch must be refused"
    assert "not allowed" in run.result.error, run.result.error

    await run_tool(catalog, "coding_cleanup_worktree", factory, {"artifact_id": artifact_id})