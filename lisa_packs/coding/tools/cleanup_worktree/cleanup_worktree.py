"""Explicitly remove an owned clean coding worktree without force flags."""

from __future__ import annotations

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    load_owned_artifact,
    prune_checkpoint_tags,
    run_git,
)

TOOL_ID = "coding_cleanup_worktree"


class Parameters(BaseModel):
    """Structured worktree cleanup request."""

    artifact_id: str = Field(min_length=1, max_length=100)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Remove only the artifact's owned worktree, never a source checkout."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
            allow_missing_worktree=True,
        )
        worktree = artifact_worktree(artifact, config=config, allow_missing=True)
        # Cleanup is idempotent: when the directory vanished but git still
        # registers it, git prunes that stale registration and exits 0; when
        # the registration is gone too it reports the path as "not a working
        # tree", which is the same already-removed outcome, not a failure.
        already_removed = not worktree.exists()
        source_root = config.resolve_workspace_root()
        removed = await run_git(
            source_root,
            ("worktree", "remove", str(worktree)),
            config=config,
        )
        if removed.return_code != 0:
            if "is not a working tree" not in f"{removed.stderr}{removed.stdout}":
                raise CodingWorkflowError(
                    "git worktree remove failed: "
                    + (removed.stderr.strip() or "commit or revert changes first")
                )
            already_removed = True
        artifact.status = "cleaned"
        await ctx.code_change_artifact_store.save(artifact)
        # Cleanup ends the artifact's branch for good; its checkpoint tags
        # have nothing left to restore, so prune them from the source root,
        # which also works when the worktree was already gone.
        pruned, _prune_failed = await prune_checkpoint_tags(
            artifact, root=source_root, config=config
        )
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    op_text = (
        f"Worktree for artifact {artifact.artifact_id} was already removed; "
        "recorded it as cleaned."
        if already_removed
        else f"Cleaned owned worktree for artifact {artifact.artifact_id}."
    )
    if pruned:
        op_text += f" Pruned {len(pruned)} checkpoint tag(s)."
    return ToolResult(
        tool_id=TOOL_ID,
        output_text=op_text,
        output_json={
            "artifact_id": artifact.artifact_id,
            "branch": artifact.branch,
            "already_removed": already_removed,
            "pruned_checkpoint_tags": pruned,
        },
    )
