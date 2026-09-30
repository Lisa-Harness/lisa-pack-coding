"""Worktree-trajectory eval: the full coding pack write chain, in-process.

Drives ``create_worktree → status → read_file → apply_patch → run_check →
commit → push → status → cleanup_worktree`` against a throwaway Git repo and
bare remote. Asserts the artifact state machine transitions
(``created → patched → committed → pushed → cleaned``) and per-tool result
fields. Deterministic: the only I/O is Git inside tmp dirs; no model calls.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest
from kernel.capability_binding import ExecutionFloorView
from kernel.models import ToolResult
from packstest.catalog import build_tool_catalog
from packstest.context import ToolContextFactory
from packstest.git import git_repo
from packstest.report import Trajectory
from packstest.runner import ToolRun, run_tool

PACK_PATH = Path(__file__).resolve().parents[4]
SHA1_HEX_LENGTH = 40

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


def _verified_sandbox(factory: ToolContextFactory) -> None:
    factory.execution_floor_for = lambda _tool_id: ExecutionFloorView(
        execution="sandbox",
        persistence="direct-write",
        paths={str(factory.root.parent): "rw"},
    )

    async def dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        assert tool_id == "bwrap_run"
        assert parameters["argv"] == ["true"]
        worktree = str(parameters["cwd"])
        assert parameters["overrides"] == {
            "persistence": "direct-write",
            "paths": {worktree: "rw"},
        }
        return ToolResult(
            output_text="ok",
            output_json={
                "sandboxed": True,
                "return_code": 0,
                "persistence": "direct-write",
                "cwd": worktree,
                "writable_paths": [worktree],
            },
        )

    factory.delegated_tool_dispatch = dispatch


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
async def test_rejected_patch_can_be_corrected_without_mutating_first(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "format test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))
    rejected = await run_tool(
        catalog, "coding_apply_patch", factory,
        {
            "artifact_id": artifact_id,
            "patch": "*** Begin Patch\n*** Update File: app.txt\n@@\n-hello\n+fixed\n*** End Patch",
        },
    )
    assert "unified diff file path" in rejected.result.error
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.revision == 0
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello\n"
    corrected = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": PATCH}
    )
    assert corrected.result.error == ""
    assert corrected.result.output_json["revision"] == 1
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello from lisa\n"
    assert "Do not use *** Begin Patch" in catalog["coding_apply_patch"]["description"]


@pytest.mark.eval
@pytest.mark.asyncio
async def test_full_worktree_trajectory(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """The create→patch→check→commit→push chain reaches ``pushed`` cleanly."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    factory.identity["task_id"] = "coding-task"
    _verified_sandbox(factory)
    trajectory = Trajectory(name="worktree-full-chain")
    artifact_id = ""

    run = await _step(catalog, factory, trajectory, "coding_create_worktree", {"summary": "eval"})
    assert run.result.error == "", run.result.error
    artifact_id = str(run.result.output_json["artifact_id"])
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.task_id == "coding-task"
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
    assert len(commit_sha) == SHA1_HEX_LENGTH

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
async def test_required_check_rejects_changed_candidate_tree(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    config = {**_config(repo.root), "required_check_ids": ["smoke"]}
    factory = ToolContextFactory(root=repo.root, pack_config=config)
    _verified_sandbox(factory)
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))
    patched = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert patched.result.error == ""
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    assert checked.result.output_json["status"] == "passed"

    (worktree / "untracked.txt").write_text("unchecked content\n", encoding="utf-8")
    committed = await run_tool(
        catalog,
        "coding_commit",
        factory,
        {"artifact_id": artifact_id, "message": "unchecked file"},
    )
    assert "checked tree" in committed.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.commit_sha == ""


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


@pytest.mark.eval
@pytest.mark.asyncio
async def test_required_check_refuses_missing_sandbox(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert patched.result.error == ""

    checked = await run_tool(
        catalog,
        "coding_run_check",
        factory,
        {"artifact_id": artifact_id, "check_id": "smoke"},
    )

    assert "sandbox" in checked.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
async def test_required_check_rejects_restricted_worktree_floor(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = str(created.result.output_json["worktree_path"])
    patched = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert patched.result.error == ""

    factory.execution_floor_for = lambda _tool_id: ExecutionFloorView(
        execution="sandbox",
        persistence="direct-write",
        paths={str(repo.root.parent): "rw", worktree: "ro"},
    )

    async def forbidden_dispatch(_tool_id: str, _parameters: Mapping[str, object]) -> ToolResult:
        raise AssertionError("the restricted worktree must not be dispatched")

    factory.delegated_tool_dispatch = forbidden_dispatch
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    assert "floor" in checked.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
async def test_check_rejects_worktree_mutation_during_sandbox_run(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": PATCH}
    )
    assert patched.result.error == ""
    _verified_sandbox(factory)
    original_dispatch = factory.delegated_tool_dispatch
    assert original_dispatch is not None

    async def mutating_dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        (Path(str(parameters["cwd"])) / "generated.txt").write_text("new\n", encoding="utf-8")
        return await original_dispatch(tool_id, parameters)

    factory.delegated_tool_dispatch = mutating_dispatch
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    assert "changed the candidate git tree" in checked.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
async def test_timed_out_check_cannot_certify_commit(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    config = {**_config(repo.root), "required_check_ids": ["smoke"]}
    factory = ToolContextFactory(root=repo.root, pack_config=config)
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": PATCH}
    )
    assert patched.result.error == ""
    _verified_sandbox(factory)
    original_dispatch = factory.delegated_tool_dispatch
    assert original_dispatch is not None

    async def timed_out_dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        result = await original_dispatch(tool_id, parameters)
        return result.model_copy(update={"output_json": {**result.output_json, "timed_out": True}})

    factory.delegated_tool_dispatch = timed_out_dispatch
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    assert checked.result.output_json["status"] == "timed_out"
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks["smoke"].status == "timed_out"
    committed = await run_tool(
        catalog, "coding_commit", factory, {"artifact_id": artifact_id, "message": "not checked"}
    )
    assert "required check" in committed.result.error.lower()


@pytest.mark.eval
@pytest.mark.asyncio
async def test_no_required_checks_allows_commit_without_running_optional_check(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": PATCH}
    )
    assert patched.result.error == ""
    committed = await run_tool(
        catalog,
        "coding_commit",
        factory,
        {"artifact_id": artifact_id, "message": "no required checks"},
    )
    assert committed.result.error == ""
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.status == "committed" and artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"sandboxed": False, "return_code": 0},
        {"sandboxed": True, "degraded": True, "return_code": 0},
        {"sandboxed": True, "return_code": 0, "cwd": ""},
    ],
)
async def test_required_check_rejects_unverified_sandbox_result(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(root=repo.root, pack_config=_config(repo.root))
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert patched.result.error == ""

    async def fake_sandbox(_tool_id: str, parameters: dict[str, object]) -> ToolResult:
        worktree = str(parameters["cwd"])
        return ToolResult(
            output_text="ok",
            output_json={
                "persistence": "direct-write",
                "cwd": worktree,
                "writable_paths": [worktree],
                **payload,
            },
        )

    factory.execution_floor_for = lambda _tool_id: ExecutionFloorView(
        execution="sandbox",
        persistence="direct-write",
        paths={str(repo.root.parent): "rw"},
    )
    factory.delegated_tool_dispatch = fake_sandbox

    checked = await run_tool(
        catalog,
        "coding_run_check",
        factory,
        {"artifact_id": artifact_id, "check_id": "smoke"},
    )

    assert "sandbox" in checked.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}