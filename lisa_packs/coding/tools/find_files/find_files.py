"""Find local source files using bounded Python glob matching."""

from __future__ import annotations

import fnmatch
from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    iter_source_files,
    relative_path,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    readable_root,
    validate_artifact_id,
)
from pydantic import BaseModel, Field, field_validator

TOOL_ID = "coding_find_files"


class Parameters(BaseModel):
    """Structured file matching request."""

    artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Owned worktree's artifact id. Omit (JSON null) to search the source tree.",
    )
    scope: Literal["auto", "source", "worktree"] = "auto"
    pattern: str = Field(default="*", min_length=1, max_length=200)
    path: str = Field(default="", description="Optional relative directory to search.")
    max_results: int = Field(default=50, ge=1, le=200)

    @field_validator("artifact_id", mode="before")
    @classmethod
    def _reject_mistyped_artifact_id(cls, value: object) -> object:
        """Surface the JSON-null contract when the sentinel arrives mistyped."""

        return validate_artifact_id(value)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Find matching non-generated files without a shell or arbitrary command string."""

    params = Parameters.model_validate(ctx.parameters)
    if ".." in params.pattern.replace("\\", "/").split("/"):
        return ToolResult(tool_id=TOOL_ID, error="Patterns cannot contain path traversal.")
    try:
        config = get_coding_config(ctx)
        read_root = await readable_root(
            ctx, artifact_id=params.artifact_id, scope=params.scope, config=config
        )
        root = read_root.path
        search_root = resolve_relative_path_at_root(root, params.path, allow_root=True)
        if not search_root.is_dir():
            return ToolResult(tool_id=TOOL_ID, error="Requested path is not a directory.")
        limit = min(params.max_results, config.max_search_results)
        matches = []
        for candidate in iter_source_files(search_root):
            workspace_relative = relative_path(root, candidate)
            search_relative = candidate.relative_to(search_root).as_posix()
            if fnmatch.fnmatch(search_relative, params.pattern) or fnmatch.fnmatch(
                workspace_relative, params.pattern
            ):
                matches.append(workspace_relative)
            if len(matches) >= limit:
                break
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Found {len(matches)} matching files.",
        output_json={"files": matches, "scope": read_root.scope, "artifact_id": read_root.artifact_id},
    )
