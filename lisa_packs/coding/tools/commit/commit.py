"""Commit an owned worktree after verifying it against the upstream tip.

Committing always runs the verification pipeline first (upstream sync,
preparation, fix checks, required checks with reuse), so a commit is never
certified against a stale base or a stale toolchain.
"""

from __future__ import annotations

from pathlib import Path

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from lisa_packs.coding.config import CodingPackConfig, get_coding_config
from lisa_packs.coding.verification import VerifyReport, verify_artifact
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_branch_is_allowed,
    artifact_has_current_required_checks,
    artifact_worktree,
    candidate_tree_sha,
    load_owned_artifact,
    run_git,
)

TOOL_ID = "coding_commit"


class Parameters(BaseModel):
    """Structured commit request."""

    artifact_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1)


async def _verify_committed_tree(
    worktree: Path, *, config: CodingPackConfig, candidate_tree: str
) -> None:
    tree = await run_git(worktree, ("rev-parse", "HEAD^{tree}"), config=config)
    if tree.return_code != 0 or tree.stdout.strip() != candidate_tree:
        raise CodingWorkflowError("Committed tree differs from the checked candidate tree.")


def _committed_text(revision: int, commit_sha: str, report: VerifyReport) -> str:
    return f"Committed revision {revision} as {commit_sha}. " + report.text(
        revision=revision
    )


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Stage explicit changed paths and commit only a current checked revision."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
            claim=True,
        )
        if not artifact_branch_is_allowed(artifact, config):
            raise CodingWorkflowError("Artifact branch is not an owned unprotected feature branch.")
        worktree = artifact_worktree(artifact, config=config)
        if not artifact.changed_paths:
            raise CodingWorkflowError("Artifact has no task-owned changed paths to commit.")
        report = await verify_artifact(ctx, artifact, config=config)
        paths = list(dict.fromkeys(artifact.changed_paths))
        candidate_tree = await candidate_tree_sha(worktree, config=config, paths=paths)
        if not artifact_has_current_required_checks(artifact, candidate_tree=candidate_tree):
            raise CodingWorkflowError(
                "Not committed: the required checks did not all pass.\n"
                + report.text(revision=artifact.revision)
            )
        current_branch = await run_git(worktree, ("branch", "--show-current"), config=config)
        if current_branch.return_code != 0 or current_branch.stdout.strip() != artifact.branch:
            raise CodingWorkflowError("Owned worktree is not on the recorded feature branch.")
        reset = await run_git(worktree, ("reset", "--mixed", "HEAD"), config=config)
        if reset.return_code != 0:
            raise CodingWorkflowError(reset.stderr.strip() or "git index reset failed")
        staged = await run_git(
            worktree, ("--literal-pathspecs", "add", "--", *paths), config=config
        )
        if staged.return_code != 0:
            raise CodingWorkflowError(staged.stderr.strip() or "git add failed")
        committed = await run_git(worktree, ("commit", "-m", params.message), config=config)
        if committed.return_code != 0:
            raise CodingWorkflowError(committed.stderr.strip() or "git commit failed")
        head = await run_git(worktree, ("rev-parse", "HEAD"), config=config)
        if head.return_code != 0:
            raise CodingWorkflowError(head.stderr.strip() or "Cannot resolve committed HEAD.")
        await _verify_committed_tree(worktree, config=config, candidate_tree=candidate_tree)
        artifact.commit_sha = head.stdout.strip()
        artifact.head_sha = artifact.commit_sha
        artifact.status = "committed"
        # A commit carries the imported content out of the pending-import state.
        artifact.imported_commits = {}
        artifact.import_commit = ""
        await ctx.code_change_artifact_store.save(artifact)
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=_committed_text(artifact.revision, artifact.commit_sha, report),
        output_json={
            "verification": report.to_json(artifact=artifact),
            "artifact_id": artifact.artifact_id,
            "commit_sha": artifact.commit_sha,
            "branch": artifact.branch,
            "revision": artifact.revision,
        },
    )
