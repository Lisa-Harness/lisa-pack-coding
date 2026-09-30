"""Run one configured finite check in an owned coding worktree.

The check argv runs through ``bwrap_run`` in the owned worktree. If the sandbox
is unavailable or degrades, the check fails without running on the host.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    candidate_tree_sha,
    expand_check_argv,
    load_owned_artifact,
    preparation_is_current,
    run_contained,
)
from pydantic import BaseModel, Field
from sdk.workproducts import WorkProductCheck, WorkProductCheckStatus

TOOL_ID = "coding_run_check"


class Parameters(BaseModel):
    """Structured named-check invocation."""

    artifact_id: str = Field(min_length=1, max_length=100)
    check_id: str = Field(min_length=1, max_length=80)


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
        if not preparation_is_current(artifact, worktree=worktree, config=config):
            raise CodingWorkflowError(
                "Workspace preparation is missing or stale; run coding_prepare_workspace first."
            )

        started = time.perf_counter()
        candidate_tree = await candidate_tree_sha(worktree, config=config)
        result = await run_contained(
            ctx,
            expand_check_argv(check, worktree=worktree),
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
            argv=list(expand_check_argv(check, worktree=worktree)),
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
