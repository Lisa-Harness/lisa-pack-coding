"""Record one verified conclusion about the checked worktree revision.

A fact is a short ``{claim, at, revision}`` triple recorded onto the current
turn's thread entry. Later turns rehydrate facts from the digest's
``thread:verified_facts`` item instead of re-deriving the same evidence from
180-char preview teasers.

The tool *writes* nothing on its own. Its output is a structured payload the
runtime's turn-completion seam stores on the thread entry for the turn the
tool ran in; the fact therefore dies if the turn dies, exactly like the
operation's other receipts. Durability comes from the thread segment being
sealed on the turn boundary, not from this call.

Verification gate: the claim's ``revision`` must equal the current checked
commit SHA of an owned artifact whose worktree is clean and whose checks
fingerprint matches HEAD. Otherwise the fact records confidently about a
tree that has since drifted — that is the bug this gate exists to prevent.
"""

from __future__ import annotations

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_has_current_required_checks,
    artifact_worktree,
    candidate_tree_sha,
    load_owned_artifact,
    preparation_is_current,
    run_git,
)

TOOL_ID = "coding_record_fact"


class Parameters(BaseModel):
    """Structured fact record against one owned worktree."""

    artifact_id: str = Field(min_length=1, max_length=100)
    claim: str = Field(min_length=1, max_length=200)
    at: str = Field(
        min_length=1,
        max_length=120,
        description="Anchor of the evidence, e.g. 'daemon.go:124' or 'token_gin.go:12-25'.",
    )
    revision: str = Field(
        min_length=7,
        max_length=80,
        description="The checked commit SHA this fact was verified against.",
    )


def _normalize_revision(value: str) -> str:
    return value.strip().lower()


def revisions_match(claimed: str, actual: str) -> bool:
    """Full-SHA equality after normalization (prefix claims are refused)."""
    return bool(claimed) and _normalize_revision(claimed) == _normalize_revision(actual)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Validate the claim against the checked revision and return the payload."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
        )
        if not artifact.commit_sha:
            raise CodingWorkflowError(
                "artifact has no checked commit; nothing to verify against"
            )
        worktree = artifact_worktree(artifact, config=config)
        status = await run_git(worktree, ("status", "--porcelain"), config=config)
        if status.return_code != 0:
            raise CodingWorkflowError(status.stderr.strip() or "git status failed")
        if status.stdout.strip():
            raise CodingWorkflowError(
                "worktree is dirty; facts can only be recorded against a clean checked commit"
            )
        if not preparation_is_current(artifact, worktree=worktree, config=config):
            raise CodingWorkflowError(
                "workspace preparation is stale; re-run preparation before recording"
            )
        head = await run_git(worktree, ("rev-parse", "HEAD"), config=config)
        if head.return_code != 0:
            raise CodingWorkflowError(head.stderr.strip() or "git rev-parse failed")
        if head.stdout.strip() != artifact.commit_sha:
            raise CodingWorkflowError(
                "owned worktree HEAD no longer matches the recorded commit"
            )
        if not revisions_match(params.revision, head.stdout):
            raise CodingWorkflowError(
                f"claim's revision {params.revision!r} does not match HEAD {head.stdout.strip()!r}; "
                "facts must be recorded against the current checked commit"
            )
        if not artifact_has_current_required_checks(
            artifact,
            candidate_tree=await candidate_tree_sha(
                worktree, config=config, paths=artifact.changed_paths
            ),
        ):
            raise CodingWorkflowError(
                "required check fingerprints are stale for HEAD; re-run checks before recording"
            )
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Recorded fact on {artifact.artifact_id}: {params.claim}",
        output_json={
            "fact": {
                "claim": params.claim,
                "at": params.at,
                "revision": head.stdout.strip(),
            },
            "artifact_id": artifact.artifact_id,
            "recorded_against_revision": head.stdout.strip(),
        },
    )
