"""Provider-neutral containment: the coding pack must not name a sandbox tool.

The regression this file pins down: the pack's checks used to hardcode
``bwrap_run`` (its floor lookup *and* its result envelope), which made
``lisa-pack-coding`` unusable on a Windows host where bubblewrap cannot exist.
The coupling is now a session-supplied runner, so every case here builds the
same coding config and varies only the implementation behind the runner.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from kernel.capability_binding import ExecutionFloorView
from kernel.models import ToolExecutionContext, ToolResult
from kernel.sandbox_floor import (
    SandboxExecutionEnvelope,
    SandboxRunnerContractError,
    SandboxRunRequest,
    SandboxRunResult,
)
from packstest.store import MemoryWorkProductStore

from lisa_packs.coding.tests.runner_doubles import DirectWriteRunner, runner_payload
from lisa_packs.coding.tests.test_git_workflow import _repository
from lisa_packs.coding.tools.apply_patch.apply_patch import execute as apply_patch
from lisa_packs.coding.tools.create_worktree.create_worktree import (
    execute as create_worktree,
)
from lisa_packs.coding.tools.verify.verify import execute as verify

_PATCH = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-hello\n+hello world\n"


class _RecordingRunner:
    """A Windows-shaped runner: different tool id, same neutral contract."""

    tool_id = "pwsh_argv_run"
    path_attestation = "result"

    def __init__(self) -> None:
        self.requests: list[SandboxRunRequest] = []

    def build_request(
        self, request: SandboxRunRequest, *, floor: object = None
    ) -> Mapping[str, object]:
        self.requests.append(request)
        return {
            "argv": list(request.argv),
            "cwd": request.cwd,
            "timeout_seconds": request.timeout_seconds,
        }

    def interpret_result(
        self, request: SandboxRunRequest, envelope: SandboxExecutionEnvelope
    ) -> SandboxRunResult:
        payload = dict(envelope.output_json or {})
        if payload.get("sandboxed") is not True:
            raise SandboxRunnerContractError("not contained")
        return SandboxRunResult(
            return_code=int(payload.get("return_code", 0)),
            stdout=envelope.output_text or "",
            stderr=str(envelope.output_json.get("stderr", "")),
        )


def _context(
    root: Path,
    store,
    runner,
    *,
    dispatch,
    worktree_paths: dict[str, str] | None = None,
) -> ToolExecutionContext:
    from lisa_packs.coding.tests.test_git_workflow import _context as base_context

    ctx = base_context(
        root,
        store,
        {},
        checks=[
            {"id": "unit", "argv": [sys.executable, "-c", "print('ok')"], "timeout_seconds": 30}
        ],
        required_check_ids=["unit"],
    )
    return ctx.model_copy(
        update={
            "sandbox_runner": runner,
            "delegated_tool_dispatch": dispatch,
            "execution_floor_for": lambda tool_id: ExecutionFloorView(
                execution="sandbox",
                persistence="direct-write",
                paths=worktree_paths or {str(root.parent): "rw"},
            ),
        }
    )


async def _artifact(root: Path, store) -> str:
    ctx = _context(
        root,
        store,
        DirectWriteRunner(),
        dispatch=_never_dispatched,
    )
    created = await create_worktree(
        ctx.model_copy(update={"parameters": {"summary": "runner test"}})
    )
    assert created.error == "", created.error
    artifact_id = str(created.output_json["artifact_id"])
    patched = await apply_patch(
        ctx.model_copy(update={"parameters": {"artifact_id": artifact_id, "patch": _PATCH}})
    )
    assert patched.error == "", patched.error
    return artifact_id


async def _never_dispatched(_tool_id: str, _parameters: Mapping[str, object]) -> ToolResult:
    raise AssertionError("no sandbox dispatch expected")


@pytest.mark.asyncio
async def test_checks_run_through_whatever_runner_the_session_supplies(
    tmp_path: Path,
) -> None:
    """A non-bwrap runner drives the whole pipeline with no code change."""
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    runner = _RecordingRunner()
    dispatched: list[tuple[str, dict[str, object]]] = []

    async def dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        dispatched.append((tool_id, dict(parameters)))
        return ToolResult(output_text="ok\n", output_json=runner_payload(parameters))

    ctx = _context(root, store, runner, dispatch=dispatch)
    created = await create_worktree(
        ctx.model_copy(update={"parameters": {"summary": "runner test"}})
    )
    artifact_id = str(created.output_json["artifact_id"])
    await apply_patch(
        ctx.model_copy(update={"parameters": {"artifact_id": artifact_id, "patch": _PATCH}})
    )

    checked = await verify(
        ctx.model_copy(update={"parameters": {"artifact_id": artifact_id, "check_ids": ["unit"]}})
    )

    assert checked.error == "", checked.error
    assert checked.output_json["status"] == "passed"
    # The pack dispatched to the runner's tool, never to a hardcoded bwrap id.
    assert {tool_id for tool_id, _ in dispatched} == {"pwsh_argv_run"}
    request = runner.requests[-1]
    assert request.persistence == "direct-write"
    assert request.argv[0] == sys.executable
    assert request.paths == {request.cwd: "rw"}


@pytest.mark.asyncio
async def test_missing_runner_fails_closed_instead_of_running_on_the_host(
    tmp_path: Path,
) -> None:
    """No sandbox implementation ⇒ a named containment error, never a host run."""
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id = await _artifact(root, store)

    ctx = _context(root, store, None, dispatch=_never_dispatched)
    checked = await verify(
        ctx.model_copy(update={"parameters": {"artifact_id": artifact_id, "check_ids": ["unit"]}})
    )

    assert "no sandbox implementation is active" in checked.error
    assert "not an accepted substitute" in checked.error
    artifact = await store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}


@pytest.mark.asyncio
async def test_unverifiable_runner_result_fails_the_check(
    tmp_path: Path,
) -> None:
    """A runner that cannot prove containment aborts rather than reporting a pass."""
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id = await _artifact(root, store)

    class _Lying(DirectWriteRunner):
        def interpret_result(self, request, envelope):  # type: ignore[no-untyped-def]
            raise SandboxRunnerContractError("could not prove containment")

    async def dispatch(_tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        return ToolResult(output_text="", output_json=runner_payload(parameters))

    ctx = _context(root, store, _Lying(), dispatch=dispatch)
    checked = await verify(
        ctx.model_copy(update={"parameters": {"artifact_id": artifact_id, "check_ids": ["unit"]}})
    )

    assert "sandbox runner rejected the run" in checked.error
    assert "could not prove containment" in checked.error
    artifact = await store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}


@pytest.mark.asyncio
async def test_floor_must_grant_the_worktree_regardless_of_implementation(
    tmp_path: Path,
) -> None:
    """The worktree-write precondition is policy, not a bwrap detail."""
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id = await _artifact(root, store)

    ctx = _context(
        root,
        store,
        DirectWriteRunner(),
        dispatch=_never_dispatched,
        worktree_paths={str(root.parent): "ro"},
    )
    checked = await verify(
        ctx.model_copy(update={"parameters": {"artifact_id": artifact_id, "check_ids": ["unit"]}})
    )

    assert "does not permit worktree writes" in checked.error


@pytest.mark.asyncio
async def test_direct_write_persistence_is_requested_explicitly(
    tmp_path: Path,
) -> None:
    """A fix check's edits must land; the request never asks for a weaker posture."""
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    runner = _RecordingRunner()

    async def dispatch(_tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        return ToolResult(output_text="", output_json=runner_payload(parameters))

    ctx = _context(root, store, runner, dispatch=dispatch)
    artifact_id = await _artifact(root, store)
    await apply_patch(
        ctx.model_copy(update={"parameters": {"artifact_id": artifact_id, "patch": _PATCH}})
    )
    checked = await verify(
        ctx.model_copy(update={"parameters": {"artifact_id": artifact_id, "check_ids": ["unit"]}})
    )
    assert checked.error == "", checked.error
    assert runner.requests, "the runner must have been asked to run the check"
    assert all(request.persistence == "direct-write" for request in runner.requests)
