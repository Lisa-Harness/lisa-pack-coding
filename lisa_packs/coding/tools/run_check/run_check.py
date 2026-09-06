"""Run one configured finite check in an owned coding worktree.

When the bwrap sandbox pack is registered, the check argv is composed through
``bwrap_run`` (read-only workspace, the owned worktree bound writable, ``cwd`` =
the worktree) so arbitrary configured commands run contained. When the sandbox
is unavailable or degrades at runtime, the check falls back to host execution
and the result is stamped ``sandboxed: false`` — visible, never silent.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sdk.workproducts import WorkProductCheck, WorkProductCheckStatus
from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    ProcessResult,
    artifact_worktree,
    load_owned_artifact,
    run_process,
)
from pydantic import BaseModel, Field

TOOL_ID = "coding_run_check"


class Parameters(BaseModel):
    """Structured named-check invocation."""

    artifact_id: str = Field(min_length=1, max_length=100)
    check_id: str = Field(min_length=1, max_length=80)


def _sandbox_registered() -> bool:
    """True when ``bwrap_run`` is registered (pack installed + importable)."""
    try:
        from kernel.tools.registry import get_tool_catalog
    except Exception:
        return False
    return "bwrap_run" in get_tool_catalog()


async def _run_contained(
    ctx: ToolExecutionContext,
    argv: tuple[str, ...],
    *,
    worktree: Path,
    timeout_seconds: int,
) -> ProcessResult | None:
    """Run the check argv through bwrap_run; return None when unavailable.

    The owned worktree is passed as a writable bind and as ``cwd`` so the check
    runs inside it with the rest of the filesystem read-only. A ``None`` return
    means the sandbox could not run the command (pack absent, binary missing,
    or a degraded namespaced run) and the caller should fall back to host exec.
    """
    if not _sandbox_registered():
        return None
    from kernel.tools.registry import execute_tool

    child = ToolExecutionContext(
        tool_id="bwrap_run",
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        user_slug=ctx.user_slug,
        surface_type=ctx.surface_type,
        surface_id=ctx.surface_id,
        memory_scope=ctx.memory_scope,
        mode_id=ctx.mode_id,
        query=ctx.query,
        parameters={
            "argv": list(argv),
            "cwd": str(worktree),
            # The worktree sits outside bwrap's capture root, so bind it
            # writable; the check only needs scratch + the worktree writable.
            "writable_paths": [str(worktree)],
            "timeout_seconds": min(timeout_seconds, 600),
        },
        data_dir=ctx.data_dir,
        pack_config=ctx.pack_config,
        approval_service=ctx.approval_service,
        approval_request_id=ctx.approval_request_id,
        session_plan_digest=ctx.session_plan_digest,
        event_bus=ctx.event_bus,
        state_provider=ctx.state_provider,
    )
    tool_result = await execute_tool("bwrap_run", child)
    payload = dict(tool_result.output_json or {})
    if tool_result.error:
        # Degraded sandbox (e.g. bwrap binary absent): fall back to host exec.
        return None
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
        sandboxed = _sandbox_registered()
        result: ProcessResult | None = None
        if sandboxed:
            contained = await _run_contained(
                ctx,
                check.argv,
                worktree=worktree,
                timeout_seconds=check.timeout_seconds,
            )
            if contained is not None:
                result = contained
            else:
                # Registered but could not run (degraded): fall back to host.
                sandboxed = False
        if result is None:
            result = await run_process(
                check.argv,
                cwd=worktree,
                timeout_seconds=check.timeout_seconds,
                max_output_bytes=config.max_command_output_bytes,
            )
        elapsed = time.perf_counter() - started
        status: WorkProductCheckStatus = (
            "timed_out" if result.timed_out else "passed" if result.return_code == 0 else "failed"
        )
        record = WorkProductCheck(
            check_id=check.id,
            status=status,
            revision=artifact.revision,
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
            "sandboxed": sandboxed,
        },
    )
