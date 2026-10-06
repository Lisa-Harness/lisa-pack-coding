"""Apply bounded, exact text edits to an owned coding worktree."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from hashlib import sha256
from itertools import islice
from pathlib import Path
from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import CodingPackConfig, get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    read_utf8_file_snapshot,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    load_owned_artifact,
    run_git,
)
from pydantic import BaseModel, Field, model_validator

TOOL_ID = "coding_edit_files"


class Replacement(BaseModel):
    """One exact replacement against an original file snapshot."""

    old_text: str = Field(min_length=1, max_length=100_000)
    new_text: str = Field(max_length=100_000)


class FileEdit(BaseModel):
    """One bounded file action in a structured edit batch."""

    kind: Literal["update", "create", "delete"]
    path: str = Field(min_length=1, max_length=500)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    replacements: list[Replacement] = Field(default_factory=list[Replacement], max_length=50)
    content: str | None = Field(default=None, max_length=500_000)

    @model_validator(mode="after")
    def _validate_shape(self) -> FileEdit:
        if self.kind == "update":
            if self.expected_sha256 is None or not self.replacements or self.content is not None:
                raise ValueError("Updates require expected_sha256 and replacements only.")
        elif self.kind == "create":
            if self.expected_sha256 is not None or self.content is None or self.replacements:
                raise ValueError("Creates require content only.")
        elif self.expected_sha256 is None or self.replacements or self.content is not None:
            raise ValueError("Deletes require expected_sha256 only.")
        return self


class Parameters(BaseModel):
    """Structured edit request for one owned worktree."""

    artifact_id: str = Field(min_length=1, max_length=100)
    edits: list[FileEdit] = Field(min_length=1, max_length=20)


@dataclass(frozen=True)
class PreparedEdit:
    """Validated desired file content used to generate a single Git patch."""

    path: str
    before: str | None
    after: str | None


def _replace_snapshot(source: str, replacements: list[Replacement]) -> str:
    """Apply unique, non-overlapping replacements against the original snapshot."""

    spans: list[tuple[int, int, str]] = []
    for replacement in replacements:
        start = source.find(replacement.old_text)
        if start < 0 or source.find(replacement.old_text, start + 1) >= 0:
            raise CodingWorkflowError(
                "Each old_text must occur exactly once in the original file snapshot."
            )
        spans.append((start, start + len(replacement.old_text), replacement.new_text))

    parts: list[str] = []
    cursor = 0
    for start, end, new_text in sorted(spans):
        if start < cursor:
            raise CodingWorkflowError(
                "Replacement spans must not overlap in the original file snapshot."
            )
        parts.extend((source[cursor:start], new_text))
        cursor = end
    parts.append(source[cursor:])
    return "".join(parts)


def _diff_lines(content: str | None) -> list[str]:
    """Split at LF only, preserving other separators as literal file content."""

    if content is None:
        return []
    parts = content.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _unified_patch(edits: list[PreparedEdit]) -> str:
    """Render a Git-compatible patch from fully validated text snapshots."""

    lines: list[str] = []
    for edit in edits:
        before = _diff_lines(edit.before)
        after = _diff_lines(edit.after)
        lines.extend((f"diff --git a/{edit.path} b/{edit.path}\n",))
        if edit.before is None:
            lines.extend(("new file mode 100644\n", "--- /dev/null\n", f"+++ b/{edit.path}\n"))
        elif edit.after is None:
            lines.extend(("deleted file mode 100644\n", f"--- a/{edit.path}\n", "+++ /dev/null\n"))
        else:
            lines.extend((f"--- a/{edit.path}\n", f"+++ b/{edit.path}\n"))
        diff = difflib.unified_diff(before, after, n=3)
        for line in islice(diff, 2, None):
            if line.endswith("\n"):
                lines.append(line)
            else:
                lines.extend((line + "\n", "\\ No newline at end of file\n"))
    return "".join(lines)


def _prepare_edit(edit: FileEdit, *, worktree: Path, config: CodingPackConfig) -> PreparedEdit:
    """Validate one file action and compute its desired snapshot without writing."""

    path = resolve_relative_path_at_root(worktree, edit.path)
    if edit.kind == "create":
        if path.exists():
            raise CodingWorkflowError(f"Cannot create existing file: {edit.path}")
        assert edit.content is not None
        if len(edit.content.encode("utf-8")) > config.max_file_bytes:
            raise CodingWorkflowError("Created content exceeds the configured file byte limit.")
        return PreparedEdit(path=edit.path, before=None, after=edit.content)
    if not path.is_file():
        raise CodingWorkflowError(f"Requested edit path is not a file: {edit.path}")
    before, digest = read_utf8_file_snapshot(path, max_bytes=config.max_file_bytes)
    if digest != edit.expected_sha256:
        raise CodingWorkflowError(f"File changed since it was read: {edit.path}")
    if edit.kind == "delete":
        return PreparedEdit(path=edit.path, before=before, after=None)
    after = _replace_snapshot(before, edit.replacements)
    if len(after.encode("utf-8")) > config.max_file_bytes:
        raise CodingWorkflowError("Edited content exceeds the configured file byte limit.")
    return PreparedEdit(path=edit.path, before=before, after=after)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Validate and apply all structured edits as one owned-worktree revision."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx, artifact_id=params.artifact_id, config=config, claim=True
        )
        worktree = artifact_worktree(artifact, config=config)
        if len({edit.path for edit in params.edits}) != len(params.edits):
            raise CodingWorkflowError("Each structured edit path must be unique.")

        prepared = [_prepare_edit(edit, worktree=worktree, config=config) for edit in params.edits]
        changed = [edit for edit in prepared if edit.before != edit.after]
        if not changed:
            raise CodingWorkflowError("Structured edit batch makes no changes.")
        patch = _unified_patch(changed)
        if len(patch.encode("utf-8")) > config.max_patch_bytes:
            raise CodingWorkflowError("Generated patch exceeds the configured byte limit.")
        checked = await run_git(
            worktree,
            ("apply", "--check", "--whitespace=nowarn", "-"),
            config=config,
            stdin_text=patch,
        )
        if checked.return_code != 0:
            raise CodingWorkflowError(checked.stderr.strip() or "git apply --check failed")
        applied = await run_git(
            worktree,
            ("apply", "--whitespace=nowarn", "-"),
            config=config,
            stdin_text=patch,
        )
        if applied.return_code != 0:
            raise CodingWorkflowError(applied.stderr.strip() or "git apply failed")
        artifact.revision += 1
        artifact.changed_paths = list(
            dict.fromkeys([*artifact.changed_paths, *(edit.path for edit in changed)])
        )
        artifact.checks = {}
        artifact.status = "patched"
        await ctx.code_change_artifact_store.save(artifact)
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=(
            f"Applied structured edits to {len(changed)} paths at revision {artifact.revision}."
        ),
        output_json={
            "artifact_id": artifact.artifact_id,
            "revision": artifact.revision,
            "changed_paths": artifact.changed_paths,
            "patch_sha256": sha256(patch.encode("utf-8")).hexdigest(),
        },
    )
