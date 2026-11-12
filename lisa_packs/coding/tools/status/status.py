"""Inspect the durable and Git status of an owned coding worktree.

Also resolves the upstream tip and reports it beside the artifact's base so
drift is visible; the next edit, verification, or commit rebases onto it
automatically. Check records carry only failure excerpts and the diff only its
stat, keeping a routine status call small (``coding_git_diff`` shows the full
diff).
"""

from __future__ import annotations

import re

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.freshness import resolve_upstream_tip
from lisa_packs.coding.verification import failure_excerpt
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    load_owned_artifact,
    run_git,
)

TOOL_ID = "coding_status"
_ARTIFACT_KEY = re.compile(r"^coding\.[a-z]+:(?P<artifact>[^:]+)")
_SNAPSHOT_ARTIFACTS = 4
_SNAPSHOT_PATHS = 12


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
        if status.return_code != 0 or diff_stat.return_code != 0:
            raise CodingWorkflowError(
                status.stderr.strip() or diff_stat.stderr.strip() or "git status failed"
            )
        source_head_sha = await resolve_upstream_tip(source_root, config=config)
        source_branch_name = config.base_branch
        if not source_branch_name:
            source_branch = await run_git(
                source_root, ("branch", "--show-current"), config=config
            )
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
    if ahead:
        source_label = (
            f"source branch {source_branch_name}"
            if source_branch_name
            else "source checkout"
        )
        text += (
            f" The {source_label} has moved ahead of the artifact base "
            f"{artifact.base_sha}; the next edit, coding_verify, or coding_commit "
            f"rebases onto {source_head_sha} automatically."
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
                    "failure_excerpt": ""
                    if record.status == "passed"
                    else failure_excerpt(record.stdout, record.stderr),
                    "duration_ms": record.duration_ms,
                    "completed_at": record.completed_at.isoformat()
                    if record.completed_at
                    else None,
                }
                for check_id, record in artifact.checks.items()
            },
        },
    )


async def history_snapshot(ctx: ToolExecutionContext, keys: list[str]) -> str:
    """Summarize the live state of the artifacts named in a turn's history keys.

    Folded reads and statuses stay dereferenceable by ref; this line keeps
    the current branch, revision, and dirty paths resident without them.
    """

    artifact_ids = list(
        dict.fromkeys(
            match.group("artifact")
            for key in keys
            if (match := _ARTIFACT_KEY.match(key)) and match.group("artifact") != "-"
        )
    )[:_SNAPSHOT_ARTIFACTS]
    if not artifact_ids:
        return ""
    config = get_coding_config(ctx)
    lines: list[str] = []
    for artifact_id in artifact_ids:
        try:
            artifact = await load_owned_artifact(
                ctx, artifact_id=artifact_id, config=config
            )
            status = await run_git(
                artifact_worktree(artifact, config=config),
                ("status", "--short"),
                config=config,
            )
        except (CodingWorkflowError, OSError, ValueError):
            continue
        if status.return_code != 0 or status.output_truncated:
            shown = "unknown (git status failed)"
        else:
            dirty = [ln.strip() for ln in status.stdout.splitlines() if ln.strip()]
            shown = ", ".join(dirty[:_SNAPSHOT_PATHS]) or "clean"
            if len(dirty) > _SNAPSHOT_PATHS:
                shown += f", +{len(dirty) - _SNAPSHOT_PATHS} more"
        lines.append(
            f"artifact {artifact.artifact_id}: {artifact.status} on {artifact.branch} "
            f"at {artifact.revision}; worktree: {shown}"
        )
    return "\n".join(lines)
