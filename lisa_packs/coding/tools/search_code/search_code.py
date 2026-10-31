"""Search local source using a fixed ripgrep JSON invocation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    readable_root,
    validate_artifact_id,
)
from pydantic import BaseModel, Field, field_validator

TOOL_ID = "coding_search_code"


class Parameters(BaseModel):
    """Structured code search request."""

    artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Owned worktree's artifact id. Omit (JSON null) to search the source tree.",
    )
    scope: Literal["auto", "source", "worktree"] = "auto"
    query: str = Field(min_length=1, max_length=500)
    path: str = Field(default="", description="Optional relative file or directory scope.")
    max_results: int = Field(default=25, ge=1, le=200)

    @field_validator("artifact_id", mode="before")
    @classmethod
    def _reject_mistyped_artifact_id(cls, value: object) -> object:
        """Surface the JSON-null contract when the sentinel arrives mistyped."""

        return validate_artifact_id(value)


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Drain a subprocess stream while retaining at most ``limit`` bytes."""

    retained = bytearray()
    overflowed = False
    while chunk := await stream.read(8192):
        remaining = limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            overflowed = True
    return bytes(retained), overflowed


async def _run_ripgrep(
    *,
    root: Path,
    path: str,
    query: str,
    max_results: int,
    max_output_bytes: int,
    timeout_seconds: int,
) -> tuple[list[dict[str, object]], bool, bool]:
    """Run ripgrep with JSON output and bounded resource consumption."""

    args = [
        "rg",
        "--json",
        "--no-heading",
        "--no-config",
        "--no-ignore-vcs",
        "--max-count",
        str(max_results),
        "--max-filesize",
        "2M",
        query,
    ]
    if path:
        args.append(path)
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_read_limited(process.stdout, limit=max_output_bytes))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, limit=8192))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        process.kill()
        await process.wait()
    stdout, stdout_overflowed = await stdout_task
    await stderr_task
    results: list[dict[str, object]] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "match":
            data = record.get("data", {})
            path_text = data.get("path", {}).get("text", "")
            if path_text:
                path_text = path_text.replace("\\", "/").removeprefix("./")
                try:
                    resolve_relative_path_at_root(root, path_text)
                except CodingPathError:
                    continue
            results.append(
                {
                    "path": path_text,
                    "line": data.get("line_number", 0),
                    "column": data.get("submatches", [{}])[0].get("start", 0) + 1,
                    "text": data.get("lines", {}).get("text", "").rstrip("\n"),
                }
            )
    return results, timed_out, stdout_overflowed


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Search the local coding workspace through a bounded ripgrep JSON invocation."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        read_root = await readable_root(
            ctx, artifact_id=params.artifact_id, scope=params.scope, config=config
        )
        root = read_root.path
        search_scope = resolve_relative_path_at_root(root, params.path, allow_root=True)
        if not search_scope.exists():
            return ToolResult(tool_id=TOOL_ID, error="Requested path does not exist.")
        search_path = search_scope.relative_to(root).as_posix() or "."
        results, timed_out, overflowed = await _run_ripgrep(
            root=root,
            path=search_path,
            query=params.query,
            max_results=min(params.max_results, config.max_search_results),
            max_output_bytes=config.max_search_output_bytes,
            timeout_seconds=config.search_timeout_seconds,
        )
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Found {len(results)} matches.",
        output_json={
            "matches": results,
            "timed_out": timed_out,
            "output_truncated": overflowed,
            "scope": read_root.scope,
            "artifact_id": read_root.artifact_id,
        },
    )
