"""Extract a bounded structural outline from local source files."""

from __future__ import annotations

import ast
import re

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    read_text_file,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.workflow import CodingWorkflowError, readable_root
from pydantic import BaseModel, Field

TOOL_ID = "coding_outline_source"
_TS_DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:class|function|interface|type)\s+([A-Za-z_$][\w$]*)"
)
_TS_ARROW = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")


class Parameters(BaseModel):
    """Structured source outline request."""

    artifact_id: str | None = Field(default=None, min_length=1, max_length=100)
    path: str = Field(min_length=1)


def _python_outline(source: str) -> list[dict[str, object]]:
    """Return top-level Python classes and functions through the AST."""

    tree = ast.parse(source)
    items: list[dict[str, object]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            items.append({"kind": "class", "name": node.name, "line": node.lineno})
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    items.append(
                        {
                            "kind": "method",
                            "name": member.name,
                            "line": member.lineno,
                            "async": isinstance(member, ast.AsyncFunctionDef),
                        }
                    )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            items.append(
                {
                    "kind": "function",
                    "name": node.name,
                    "line": node.lineno,
                    "async": isinstance(node, ast.AsyncFunctionDef),
                }
            )
    return items


def _ts_outline(source: str) -> list[dict[str, object]]:
    """Return a regex-based TypeScript/JavaScript structural outline."""

    items: list[dict[str, object]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        match = _TS_DECLARATION.match(line) or _TS_ARROW.match(line)
        if match:
            items.append({"kind": "declaration", "name": match.group(1), "line": number})
    return items


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Extract a bounded structural outline from a local source file."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        root = await readable_root(ctx, artifact_id=params.artifact_id, config=config)
        path = resolve_relative_path_at_root(root, params.path)
        if not path.is_file():
            return ToolResult(tool_id=TOOL_ID, error="Requested path is not a file.")
        text, _ = read_text_file(path, max_bytes=config.max_file_bytes)
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    suffix = path.suffix.lower()
    if suffix == ".py":
        outline = _python_outline(text)
    elif suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        outline = _ts_outline(text)
    else:
        return ToolResult(tool_id=TOOL_ID, error="Unsupported file type for outline extraction.")

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Extracted {len(outline)} structural items from {params.path}.",
        output_json={"path": params.path, "items": outline},
    )
