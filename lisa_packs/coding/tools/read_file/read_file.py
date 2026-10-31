"""Read a bounded line window from one local text source file."""

from __future__ import annotations

from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    read_text_file,
    read_utf8_file_snapshot,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    readable_root,
    validate_artifact_id,
)
from pydantic import BaseModel, Field, field_validator

TOOL_ID = "coding_read_file"


class Parameters(BaseModel):
    """Structured bounded line-range request."""

    artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Owned worktree's artifact id. Omit (JSON null) to read the source workspace.",
    )
    scope: Literal["auto", "source", "worktree"] = "auto"
    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)

    @field_validator("artifact_id", mode="before")
    @classmethod
    def _reject_mistyped_artifact_id(cls, value: object) -> object:
        """Surface the JSON-null contract when the sentinel arrives mistyped."""

        return validate_artifact_id(value)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Read a bounded text excerpt without materializing an unbounded source file."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        read_root = await readable_root(
            ctx, artifact_id=params.artifact_id, scope=params.scope, config=config
        )
        path = resolve_relative_path_at_root(read_root.path, params.path)
        if not path.is_file():
            return ToolResult(tool_id=TOOL_ID, error="Requested path is not a file.")
        content_sha256: str | None = None
        try:
            text, content_sha256 = read_utf8_file_snapshot(path, max_bytes=config.max_file_bytes)
            byte_truncated = False
        except CodingPathError:
            text, byte_truncated = read_text_file(path, max_bytes=config.max_file_bytes)
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    start_line = params.start_line
    end_line = max(start_line, min(params.end_line, start_line + config.max_read_lines - 1))
    all_lines = text.splitlines()
    last_line = min(end_line, len(all_lines))
    width = len(str(last_line))
    # One text block with ``cat -n`` style prefixes keeps exact line numbers at
    # ~1.2x the source size; a JSON object per line cost ~3x once rendered.
    excerpt = "\n".join(
        f"{number:>{width}}\t{all_lines[number - 1]}" for number in range(start_line, last_line + 1)
    )
    line_count = max(0, last_line - start_line + 1)
    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Read {line_count} lines from {params.path}.",
        output_json={
            "path": params.path,
            "scope": read_root.scope,
            "artifact_id": read_root.artifact_id,
            "content_sha256": content_sha256,
            "editable": content_sha256 is not None,
            "start_line": start_line,
            "end_line": last_line if line_count else start_line,
            "total_lines_observed": len(all_lines),
            "truncated_by_bytes": byte_truncated,
            "excerpt": excerpt,
        },
    )
