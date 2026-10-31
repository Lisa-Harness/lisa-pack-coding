"""Push an owned checked commit without any force option."""

from __future__ import annotations

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_branch_is_allowed,
    artifact_has_current_required_checks,
    artifact_worktree,
    candidate_tree_sha,
    load_owned_artifact,
    prune_checkpoint_tags,
    run_git,
)

TOOL_ID = "coding_push"


class Parameters(BaseModel):
    """Structured owned-branch push request."""

    artifact_id: str = Field(min_length=1, max_length=100)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Push the recorded feature branch with no force capability."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
            claim=True,
        )
        if not artifact.commit_sha:
            raise CodingWorkflowError("Artifact has no committed revision to push.")
        if not artifact_branch_is_allowed(artifact, config):
            raise CodingWorkflowError("Artifact branch is not an owned unprotected feature branch.")
        worktree = artifact_worktree(artifact, config=config)
        paths = list(dict.fromkeys(artifact.changed_paths))
        if not paths:
            raise CodingWorkflowError(
                "Artifact has no task-owned changed paths to verify."
            )
        if not artifact_has_current_required_checks(
            artifact,
            candidate_tree=await candidate_tree_sha(worktree, config=config, paths=paths),
        ):
            raise CodingWorkflowError(
                "Every required check must pass at the current artifact revision and checked tree."
            )
        current_head = await run_git(worktree, ("rev-parse", "HEAD"), config=config)
        if current_head.return_code != 0 or current_head.stdout.strip() != artifact.commit_sha:
            raise CodingWorkflowError("Owned worktree HEAD no longer matches the recorded commit.")
        pushed = await run_git(
            worktree,
            ("push", "--set-upstream", config.push_remote, artifact.branch),
            config=config,
        )
        if pushed.return_code != 0:
            raise CodingWorkflowError(pushed.stderr.strip() or "git push failed")
        artifact.pushed_sha = artifact.commit_sha
        artifact.status = "pushed"
        await ctx.code_change_artifact_store.save(artifact)
        # A pushed branch is frozen: rewrites and rewinds past pushed_sha are
        # refused, so every checkpoint tag for this artifact is now dead.
        pruned, _prune_failed = await prune_checkpoint_tags(
            artifact, root=config.resolve_workspace_root(), config=config
        )
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    op_text = f"Pushed {artifact.branch} at {artifact.pushed_sha}."
    if pruned:
        op_text += f" Pruned {len(pruned)} checkpoint tag(s)."
    return ToolResult(
        tool_id=TOOL_ID,
        output_text=op_text,
        output_json={
            "artifact_id": artifact.artifact_id,
            "branch": artifact.branch,
            "pushed_sha": artifact.pushed_sha,
            "remote": config.push_remote,
            "pruned_checkpoint_tags": pruned,
        },
    )
