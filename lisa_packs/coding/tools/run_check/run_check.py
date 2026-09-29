"""Run one configured finite check in an owned coding worktree.

The check argv runs through ``bwrap_run`` in the owned worktree. If the sandbox
is unavailable or degrades, the check fails without running on the host.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    ProcessResult,
    artifact_worktree,
    candidate_tree_sha,
    load_owned_artifact,
)
from pydantic import BaseModel, Field
from sdk.workproducts import WorkProductCheck, WorkProductCheckStatus

TOOL_ID = "coding_run_check"


class Parameters(BaseModel):
    """Structured named-check invocation."""

    artifact_id: str = Field(min_length=1, max_length=100)
    check_id: str = Field(min_length=1, max_length=80)


async def _run_contained(
    ctx: ToolExecutionContext,
    argv: tuple[str, ...],
    *,
    worktree: Path,
    timeout_seconds: int,
) -> ProcessResult:
    """Run under the bwrap owner's authorized sandbox floor."""
    dispatch = ctx.delegated_tool_dispatch
    floor_for = ctx.execution_floor_for
    floor = floor_for("bwrap_run") if floor_for is not None else None
    if (
        dispatch is None
        or floor is None
        or floor.execution != "sandbox"
        or floor.persistence != "direct-write"
    ):
        raise CodingWorkflowError("bwrap sandbox has no direct-write execution floor.")
    matching_paths = (
        (root, access)
        for path, access in floor.paths.items()
        if (root := Path(path).expanduser().resolve()) == worktree
        or worktree.is_relative_to(root)
    )
    nearest = max(matching_paths, key=lambda entry: len(entry[0].parts), default=None)
    if nearest is None or nearest[1] != "rw":
        raise CodingWorkflowError("bwrap sandbox floor does not permit worktree writes.")

    tool_result = await dispatch(
        "bwrap_run",
        {
            "argv": list(argv),
            "cwd": str(worktree),
            "overrides": {
                "persistence": "direct-write",
                "paths": {str(worktree): "rw"},
            },
            "timeout_seconds": min(timeout_seconds, 600),
        },
    )
    payload = dict(tool_result.output_json or {})
    if tool_result.error:
        raise CodingWorkflowError(f"bwrap sandbox failed: {tool_result.error}")
    if (
        payload.get("sandboxed") is not True
        or payload.get("degraded")
        or payload.get("persistence") != "direct-write"
        or payload.get("cwd") != str(worktree)
        or not isinstance(payload.get("writable_paths"), list)
        or str(worktree) not in payload["writable_paths"]
        or type(payload.get("return_code")) is not int
    ):
        raise CodingWorkflowError("bwrap sandbox did not verify the writable worktree check.")
    return ProcessResult(
        return_code=int(payload.get("return_code", 0)),
        stdout=tool_result.output_text or "",
        stderr=str(payload.get("stderr", "")),
        timed_out=bool(payload.get("timed_out", False)),
        output_truncated=bool(payload.get("truncated", False)),
    )


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Run only a session-configured command vector and persist its revision result."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        check = next((item for item in config.checks if item.id == params.check_id), None)
        if check is None:
            raise CodingWorkflowError(f"Unknown configured check '{params.check_id}'.")
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
        )
        worktree = artifact_worktree(artifact, config=config)

        started = time.perf_counter()
        candidate_tree = await candidate_tree_sha(worktree, config=config)
        result = await _run_contained(
            ctx,
            check.argv,
            worktree=worktree,
            timeout_seconds=check.timeout_seconds,
        )
        if await candidate_tree_sha(worktree, config=config) != candidate_tree:
            raise CodingWorkflowError(
                "Check changed the candidate Git tree; rerun on current content."
            )
        elapsed = time.perf_counter() - started
        status: WorkProductCheckStatus = (
            "timed_out" if result.timed_out else "passed" if result.return_code == 0 else "failed"
        )
        record = WorkProductCheck(
            check_id=check.id,
            status=status,
            revision=artifact.revision,
            checked_tree_sha=candidate_tree,
            argv=list(check.argv),
            duration_ms=round(elapsed * 1000),
            stdout=result.stdout,
            stderr=result.stderr,
            completed_at=datetime.now(UTC),
        )
        artifact.checks[check.id] = record
        await ctx.code_change_artifact_store.save(artifact)
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Check '{check.id}' {status} in {elapsed:.1f}s.",
        output_json={
            "artifact_id": artifact.artifact_id,
            "check_id": check.id,
            "status": status,
            "return_code": result.return_code,
            "elapsed_seconds": round(elapsed, 3),
            "timed_out": result.timed_out,
            "output_truncated": result.output_truncated,
            "sandboxed": True,
        },
    )
