"""List bounded workspace directory entries."""

from __future__ import annotations

from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    excluded_relative_path,
    relative_path,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.workflow import CodingWorkflowError, readable_root
from pydantic import BaseModel, Field

TOOL_ID = "coding_list_directory"


class Parameters(BaseModel):
    """Structured directory listing request."""

    artifact_id: str | None = Field(default=None, min_length=1, max_length=100)
    scope: Literal["auto", "source", "worktree"] = "auto"
    path: str = Field(default="", description="Relative workspace directory, or empty for root.")
    max_entries: int = Field(default=100, ge=1, le=1000)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Return stable, bounded metadata for visible workspace entries."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        read_root = await readable_root(
            ctx, artifact_id=params.artifact_id, scope=params.scope, config=config
        )
        root = read_root.path
        directory = resolve_relative_path_at_root(root, params.path, allow_root=True)
        if not directory.is_dir():
            return ToolResult(tool_id=TOOL_ID, error="Requested path is not a directory.")
        limit = min(params.max_entries, config.max_directory_entries)
        entries = []
        for item in sorted(
            directory.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower())
        ):
            try:
                resolved = item.resolve()
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            if excluded_relative_path(relative):
                continue
            entries.append(
                {
                    "path": relative_path(root, resolved),
                    "kind": "directory" if resolved.is_dir() else "file",
                }
            )
            if len(entries) >= limit:
                break
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Listed {len(entries)} entries.",
        output_json={
            "entries": entries,
            "scope": read_root.scope,
            "artifact_id": read_root.artifact_id,
        },
    )
