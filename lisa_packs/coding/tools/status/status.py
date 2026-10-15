"""Inspect the durable and Git status of an owned coding worktree.

Also resolves the source checkout's current tip and reports it beside the
artifact's base so base drift is visible; a drifted artifact with a commit
gets an explicit prompt to rebase promptly.
"""

from __future__ import annotations

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    load_owned_artifact,
    run_git,
)

TOOL_ID = "coding_status"


class Parameters(BaseModel):
    """Structured artifact status request."""

    artifact_id: str = Field(min_length=1, max_length=100)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Report Git and check state without changing the worktree."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        source_root = config.resolve_workspace_root()
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
        )
        worktree = artifact_worktree(artifact, config=config)
        status = await run_git(worktree, ("status", "--short"), config=config)
        diff_stat = await run_git(worktree, ("diff", "--stat"), config=config)
        diff = await run_git(worktree, ("diff", "--no-ext-diff"), config=config)
        if (
            status.return_code != 0
            or diff_stat.return_code != 0
            or diff.return_code != 0
        ):
            raise CodingWorkflowError(
                status.stderr.strip()
                or diff_stat.stderr.strip()
                or diff.stderr.strip()
                or "git status failed"
            )
        source_head = await run_git(source_root, ("rev-parse", "HEAD"), config=config)
        source_branch = await run_git(
            source_root, ("branch", "--show-current"), config=config
        )
        if source_head.return_code != 0 or source_branch.return_code != 0:
            raise CodingWorkflowError(
                source_head.stderr.strip()
                or source_branch.stderr.strip()
                or "Cannot resolve the source checkout tip."
            )
        source_head_sha = source_head.stdout.strip()
        source_branch_name = source_branch.stdout.strip()
        ahead = False
        if artifact.base_sha and artifact.base_sha != source_head_sha:
            ancestor = await run_git(
                source_root,
                ("merge-base", "--is-ancestor", artifact.base_sha, source_head_sha),
                config=config,
            )
            if ancestor.return_code not in (0, 1):
                raise CodingWorkflowError(
                    ancestor.stderr.strip() or "git merge-base --is-ancestor failed"
                )
            ahead = ancestor.return_code == 0
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    text = (
        f"Artifact {artifact.artifact_id} is {artifact.status} at revision "
        f"{artifact.revision}."
    )
    if ahead and artifact.commit_sha:
        source_label = (
            f"source branch {source_branch_name}"
            if source_branch_name
            else "source checkout"
        )
        text += (
            f" The {source_label} has moved ahead of the artifact base "
            f"{artifact.base_sha}; rebase promptly onto {source_head_sha} with "
            "coding_history (op=rebase)."
        )

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=text,
        output_json={
            "artifact_id": artifact.artifact_id,
            "status": artifact.status,
            "revision": artifact.revision,
            "branch": artifact.branch,
            "commit_sha": artifact.commit_sha,
            "base_sha": artifact.base_sha,
            "pushed_sha": artifact.pushed_sha,
            "source_head_sha": source_head_sha,
            "source_head_branch": source_branch_name,
            "source_ahead_of_base": ahead,
            "git_status": status.stdout.strip() or "(clean)",
            "git_diff_stat": diff_stat.stdout.strip() or "(no diff)",
            "git_diff": diff.stdout,
            "git_diff_truncated": diff.output_truncated,
            "configured_check_ids": [check.id for check in config.checks],
            "configured_preparation_recipe_ids": [
                r.id for r in config.preparation_recipes
            ],
            "fix_check_ids": [
                check.id for check in config.checks if check.kind == "fix"
            ],
            "required_check_ids": list(artifact.required_check_ids),
            "checks": {
                check_id: {
                    "status": record.status,
                    "revision": record.revision,
                    "argv": record.argv,
                    "stdout": record.stdout,
                    "stderr": record.stderr,
                    "duration_ms": record.duration_ms,
                    "completed_at": record.completed_at.isoformat()
                    if record.completed_at
                    else None,
                }
                for check_id, record in artifact.checks.items()
            },
        },
    )
