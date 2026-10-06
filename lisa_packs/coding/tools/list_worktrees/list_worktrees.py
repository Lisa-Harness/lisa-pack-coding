"""List owned coding worktrees so the model can name, reuse, and organize them."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from kernel.models import ToolExecutionContext, ToolResult
from sdk.workproducts import WorkProductStore

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import CodingWorkflowError

TOOL_ID = "coding_list_worktrees"


def _entry(artifact, *, worktree_exists: bool) -> dict:
    return {
        "artifact_id": artifact.artifact_id,
        "branch": artifact.branch,
        "worktree_path": artifact.worktree_path,
        "worktree_exists": worktree_exists,
        "status": artifact.status,
        "revision": artifact.revision,
        "commit_sha": artifact.commit_sha,
        "pushed_sha": artifact.pushed_sha,
        "pr_url": artifact.pr_url,
        "changed_paths": list(artifact.changed_paths),
        "summary": artifact.safe_summary,
    }


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """List this identity's coding artifacts for the configured workspace."""

    try:
        config = get_coding_config(ctx)
        source_root = config.resolve_workspace_root()
        store = ctx.code_change_artifact_store
        if store is None:
            raise CodingWorkflowError("No code change artifact store is available.")
        artifacts = [
            artifact
            for artifact in await cast(WorkProductStore, store).list_owned(ctx)
            if Path(artifact.source_root).resolve() == source_root
        ]
        entries = [
            _entry(
                artifact,
                worktree_exists=(
                    bool(artifact.worktree_path)
                    and (Path(artifact.worktree_path) / ".git").exists()
                ),
            )
            for artifact in sorted(
                artifacts, key=lambda item: item.updated_at, reverse=True
            )
        ]
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    lines = [
        f"- {entry['branch']} ({entry['artifact_id'][:12]}): {entry['status']}"
        + (f" — {entry['summary']}" if entry["summary"] else "")
        for entry in entries
    ]
    return ToolResult(
        tool_id=TOOL_ID,
        output_text=(
            "Owned worktrees:\n" + "\n".join(lines)
            if entries
            else "No owned worktrees exist for this workspace."
        ),
        output_json={"worktrees": entries, "count": len(entries)},
    )
