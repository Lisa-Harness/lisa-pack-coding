"""Extract a bounded structural outline from local source files."""

from __future__ import annotations

import ast
import re
from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    read_text_file,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    readable_root,
    validate_artifact_id,
)
from pydantic import BaseModel, Field, field_validator

TOOL_ID = "coding_outline_source"
_TS_DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:class|function|interface|type)\s+([A-Za-z_$][\w$]*)"
)
_TS_ARROW = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")

# Go: gofmt keeps every top-level declaration at column 0, so column-anchored
# regexes are reliable. Methods carry a receiver before the name: func (s *T) M(.
# Generic type parameters (`[T any]`) sit between the name and the parameter
# list for both plain funcs and methods.
_GO_METHOD = re.compile(r"^func\s+\([^)]*\)\s*([A-Za-z_][\w]*)(?:\[[^\]]*\])?\s*\(")
_GO_FUNC = re.compile(r"^func\s+([A-Za-z_][\w]*)(?:\[[^\]]*\])?\s*\(")
_GO_TYPE = re.compile(r"^type\s+([A-Za-z_][\w]*)\s+(struct|interface)\b")

# Rust: declarations are keyword-led; identifiers may be pub-scoped, async,
# unsafe, const, or extern.
_RS_ITEM = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"(struct|enum|trait|union|impl|mod|type)\s+([A-Za-z_][\w]*)"
)
_RS_FN = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"(?:const\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_][\w]*)"
)


class Parameters(BaseModel):
    """Structured source outline request."""

    artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Owned worktree's artifact id. Omit (JSON null) to outline the source tree.",
    )
    scope: Literal["auto", "source", "worktree"] = "auto"
    path: str = Field(min_length=1)

    @field_validator("artifact_id", mode="before")
    @classmethod
    def _reject_mistyped_artifact_id(cls, value: object) -> object:
        """Surface the JSON-null contract when the sentinel arrives mistyped."""

        return validate_artifact_id(value)


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


def _go_outline(source: str) -> list[dict[str, object]]:
    """Return a regex-based Go structural outline (funcs, methods, named types)."""

    items: list[dict[str, object]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        match = _GO_METHOD.match(line)
        if match:
            items.append({"kind": "method", "name": match.group(1), "line": number})
            continue
        match = _GO_FUNC.match(line)
        if match:
            items.append({"kind": "function", "name": match.group(1), "line": number})
            continue
        match = _GO_TYPE.match(line)
        if match:
            items.append({"kind": match.group(2), "name": match.group(1), "line": number})
    return items


def _rust_outline(source: str) -> list[dict[str, object]]:
    """Return a regex-based Rust structural outline (fns and item declarations)."""

    items: list[dict[str, object]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        match = _RS_FN.match(line)
        if match:
            items.append({"kind": "function", "name": match.group(1), "line": number})
            continue
        match = _RS_ITEM.match(line)
        if match:
            items.append({"kind": match.group(1), "name": match.group(2), "line": number})
    return items


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Extract a bounded structural outline from a local source file."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        read_root = await readable_root(
            ctx, artifact_id=params.artifact_id, scope=params.scope, config=config
        )
        path = resolve_relative_path_at_root(read_root.path, params.path)
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
    elif suffix == ".go":
        outline = _go_outline(text)
    elif suffix == ".rs":
        outline = _rust_outline(text)
    else:
        return ToolResult(tool_id=TOOL_ID, error="Unsupported file type for outline extraction.")

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Extracted {len(outline)} structural items from {params.path}.",
        output_json={
            "path": params.path,
            "items": outline,
            "scope": read_root.scope,
            "artifact_id": read_root.artifact_id,
        },
    )
