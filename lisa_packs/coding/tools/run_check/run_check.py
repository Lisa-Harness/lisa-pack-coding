"""Run one configured finite check in an owned coding worktree.

The check argv runs through ``bwrap_run`` in the owned worktree. If the sandbox
is unavailable or degrades, the check fails without running on the host.

A ``verify`` check must not rewrite files: any edits it makes are reverted and
the run fails. A ``fix`` check (formatter, autofixer) may rewrite files; when it
succeeds its edits join the task's changed paths as a new revision, exactly as
if they had been applied by ``coding_apply_patch``, and other checks go stale.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field
from sdk.workproducts import WorkProductCheck, WorkProductCheckStatus

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.local_files import CodingPathError, resolve_relative_path_at_root
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    ProcessResult,
    artifact_worktree,
    candidate_tree_sha,
    expand_check_argv,
    load_owned_artifact,
    preparation_is_current,
    restore_tree_paths,
    run_contained,
    tree_changes,
    worktree_tree_sha,
)

TOOL_ID = "coding_run_check"


class Parameters(BaseModel):
    """Structured named-check invocation."""

    artifact_id: str = Field(min_length=1, max_length=100)
    check_id: str = Field(min_length=1, max_length=80)


_FAILURE_EXCERPT_BYTES = 2000


def _failure_excerpt(result: ProcessResult, *, status: WorkProductCheckStatus) -> str:
    """Return the tail of a failed check's output, so the diagnosis travels.

    A bare status JSON proved useless in practice: models reread the run
    without the cause ("Failed to download ..." vs. a genuine test failure)
    and burned iterations re-deriving it. Keep the excerpt bounded and only
    attach it when the check actually failed.
    """

    if status == "passed" or not (result.stdout or result.stderr):
        return ""
    combined = result.stderr.strip() + "\n" + result.stdout.strip()
    if len(combined) > _FAILURE_EXCERPT_BYTES:
        combined = "…\n" + combined[-_FAILURE_EXCERPT_BYTES:]
    return f"\n\nFailure output:\n{combined.strip()}"


def _editable(worktree: Path, paths: list[str]) -> bool:
    """A fixer may only touch paths the edit tools could also touch."""
    try:
        for path in paths:
            resolve_relative_path_at_root(worktree, path)
    except CodingPathError:
        return False
    return True


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Run only a session-configured command vector and persist its revision result."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        check = next(
            (item for item in config.checks if item.id == params.check_id), None
        )
        if check is None:
            configured = ", ".join(f"'{item.id}'" for item in config.checks) or "none"
            raise CodingWorkflowError(
                f"Unknown configured check '{params.check_id}'. Configured checks: {configured}."
            )
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
            claim=True,
        )
        worktree = artifact_worktree(artifact, config=config)
        if not preparation_is_current(artifact, worktree=worktree, config=config):
            raise CodingWorkflowError(
                "Workspace preparation is missing or stale; run coding_prepare_workspace first."
            )

        started = time.perf_counter()
        before = await worktree_tree_sha(
            worktree, config=config, paths=artifact.changed_paths
        )
        result = await run_contained(
            ctx,
            expand_check_argv(check, worktree=worktree),
            worktree=worktree,
            timeout_seconds=check.timeout_seconds,
        )
        after = await worktree_tree_sha(
            worktree, config=config, paths=artifact.changed_paths
        )
        changes = await tree_changes(
            worktree, config=config, before=before, after=after
        )
        elapsed = time.perf_counter() - started
        succeeded = not result.timed_out and result.return_code == 0
        changed = [path for _, path in changes]
        keep = check.kind == "fix" and succeeded and _editable(worktree, changed)
        if changes and not keep:
            await restore_tree_paths(
                worktree, config=config, tree=before, changes=changes
            )
            if check.kind == "verify":
                raise CodingWorkflowError(
                    f"Check '{check.id}' modified {', '.join(changed)}; the edits were "
                    "reverted. A check that rewrites files must be configured with kind: fix."
                )
            if succeeded:
                raise CodingWorkflowError(
                    f"Fix '{check.id}' modified excluded paths ({', '.join(changed)}); "
                    "the edits were reverted."
                )
        fixed_paths = changed if keep else []
        if fixed_paths:
            artifact.revision += 1
            artifact.changed_paths = list(
                dict.fromkeys([*artifact.changed_paths, *fixed_paths])
            )
            artifact.checks = {}
            artifact.status = "patched"
        candidate_tree = await candidate_tree_sha(
            worktree, config=config, paths=artifact.changed_paths or None
        )
        status: WorkProductCheckStatus = (
            "timed_out"
            if result.timed_out
            else "passed"
            if result.return_code == 0
            else "failed"
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
        output_text=f"Check '{check.id}' {status} in {elapsed:.1f}s."
        + (
            f" Fixed {', '.join(fixed_paths)} at revision {artifact.revision};"
            " rerun the other checks."
            if fixed_paths
            else ""
        )
        + _failure_excerpt(result, status=status),
        output_json={
            "artifact_id": artifact.artifact_id,
            "check_id": check.id,
            "kind": check.kind,
            "status": status,
            "revision": artifact.revision,
            "fixed_paths": fixed_paths,
            "return_code": result.return_code,
            "elapsed_seconds": round(elapsed, 3),
            "timed_out": result.timed_out,
            "output_truncated": result.output_truncated,
            "sandboxed": True,
        },
    )
