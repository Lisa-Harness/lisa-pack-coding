"""Apply bounded, exact text edits to an owned coding worktree."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
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
    replacements: list[Replacement] = Field(default_factory=list, max_length=50)
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


def _replace_exactly_once(source: str, replacement: Replacement) -> str:
    """Replace a source fragment only when it has one unambiguous occurrence."""

    matches = source.count(replacement.old_text)
    if matches != 1:
        raise CodingWorkflowError(
            "Each old_text must occur exactly once in the original file snapshot."
        )
    return source.replace(replacement.old_text, replacement.new_text, 1)


def _unified_patch(edits: list[PreparedEdit]) -> str:
    """Render a Git-compatible patch from fully validated text snapshots."""

    lines: list[str] = []
    for edit in edits:
        before = [] if edit.before is None else edit.before.splitlines(keepends=True)
        after = [] if edit.after is None else edit.after.splitlines(keepends=True)
        lines.extend((f"diff --git a/{edit.path} b/{edit.path}\n",))
        if edit.before is None:
            lines.extend(("new file mode 100644\n", "--- /dev/null\n", f"+++ b/{edit.path}\n"))
        elif edit.after is None:
            lines.extend(("deleted file mode 100644\n", f"--- a/{edit.path}\n", "+++ /dev/null\n"))
        else:
            lines.extend((f"--- a/{edit.path}\n", f"+++ b/{edit.path}\n"))
        lines.extend(
            difflib.unified_diff(
                before,
                after,
                fromfile="",
                tofile="",
                n=3,
            )
        )
    return "".join(line for line in lines if not line.startswith(("--- \n", "+++ \n")))


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Validate and apply all structured edits as one owned-worktree revision."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(ctx, artifact_id=params.artifact_id, config=config)
        worktree = artifact_worktree(artifact, config=config)
        if len({edit.path for edit in params.edits}) != len(params.edits):
            raise CodingWorkflowError("Each structured edit path must be unique.")

        prepared: list[PreparedEdit] = []
        for edit in params.edits:
            path = resolve_relative_path_at_root(worktree, edit.path)
            if edit.kind == "create":
                if path.exists():
                    raise CodingWorkflowError(f"Cannot create existing file: {edit.path}")
                assert edit.content is not None
                if len(edit.content.encode("utf-8")) > config.max_file_bytes:
                    raise CodingWorkflowError("Created content exceeds the configured file byte limit.")
                prepared.append(PreparedEdit(path=edit.path, before=None, after=edit.content))
                continue
            if not path.is_file():
                raise CodingWorkflowError(f"Requested edit path is not a file: {edit.path}")
            before, digest = read_utf8_file_snapshot(path, max_bytes=config.max_file_bytes)
            if digest != edit.expected_sha256:
                raise CodingWorkflowError(f"File changed since it was read: {edit.path}")
            if edit.kind == "delete":
                prepared.append(PreparedEdit(path=edit.path, before=before, after=None))
                continue
            after = before
            for replacement in edit.replacements:
                after = _replace_exactly_once(after, replacement)
            if len(after.encode("utf-8")) > config.max_file_bytes:
                raise CodingWorkflowError("Edited content exceeds the configured file byte limit.")
            prepared.append(PreparedEdit(path=edit.path, before=before, after=after))

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
        output_text=f"Applied structured edits to {len(changed)} paths at revision {artifact.revision}.",
        output_json={
            "artifact_id": artifact.artifact_id,
            "revision": artifact.revision,
            "changed_paths": artifact.changed_paths,
            "patch_sha256": sha256(patch.encode("utf-8")).hexdigest(),
        },
    )