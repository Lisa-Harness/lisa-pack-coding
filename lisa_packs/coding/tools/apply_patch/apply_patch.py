"""Apply a bounded unified diff only within an owned coding worktree."""

from __future__ import annotations

from pydantic import BaseModel, Field

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    load_owned_artifact,
    patch_paths,
    run_git,
)

TOOL_ID = "coding_apply_patch"


class Parameters(BaseModel):
    """Structured patch application request."""

    artifact_id: str = Field(min_length=1, max_length=100)
    patch: str = Field(min_length=1, max_length=2_000_000)


# Context-light hunks (no trailing context) are only accepted by git with
# --unidiff-zero; --recount tolerates miscounted @@ line counts.
_APPLY_FLAGS = ("--unidiff-zero", "--recount", "--whitespace=nowarn")


def normalize_patch(patch: str) -> str:
    """Terminate the final line: JSON tool arguments rarely end in ``\\n`` and
    git rejects an unterminated last line as ``corrupt patch``."""

    return patch if patch.endswith("\n") else patch + "\n"


def separate_file_sections(patch: str) -> str:
    """Insert ``diff --git`` headers between bare file sections.

    Models routinely emit multi-file patches as plain ``--- a/x`` / ``+++ b/x``
    pairs with no ``diff --git`` separator. Under ``--recount`` git locates the
    end of each hunk by scanning line prefixes, so the next section's ``---
    a/other`` is consumed as a *deletion line* of the previous hunk and the
    whole patch fails with a misleading ``patch failed: <file>:<line>`` that
    names a line whose content looks correct (the 2026-10-03 docs-patch
    incident: a valid README.md + CLI.md patch failed at README.md:266).
    Without ``--recount`` the @@ counts are trusted and the same patch
    applies; the separator terminates the hunk scan cleanly under both
    parsings. Idempotent: sections already preceded by a ``diff --git``
    line are left untouched.
    """

    out: list[str] = []
    pending: str | None = None
    section_has_header = True  # content before the first --- needs no header
    for line in patch.splitlines():
        if line.startswith(("diff --git ", "Index: ")):
            section_has_header = True
            pending = None
            out.append(line)
            continue
        if line.startswith("--- "):
            # Buffer the --- header until the paired +++ line names the
            # target: a new-file section (--- /dev/null) must derive its
            # separator from the +++ path, not /dev/null.
            pending = line
            continue
        if pending is not None:
            if line.startswith("+++ "):
                if not section_has_header:
                    target = line[4:].split("\t", maxsplit=1)[0].strip().removeprefix("b/")
                    if target == "/dev/null":
                        # Deletion of a file: the --- path names the target.
                        target = (
                            pending[4:].split("\t", maxsplit=1)[0].strip().removeprefix("a/")
                        )
                    out.append(f"diff --git a/{target} b/{target}")
                section_has_header = False
                out.append(pending)
                out.append(line)
            else:
                # A --- line not paired with +++ is patch content; keep it.
                out.append(pending)
                out.append(line)
            pending = None
            continue
        out.append(line)
    if pending is not None:
        out.append(pending)
    if not out:
        return patch
    return "\n".join(out) + "\n"


def reject_unanchored_hunks(patch: str) -> None:
    """Raise when a multi-hunk file diff has hunks with no context lines.

    ``--unidiff-zero`` makes a context-free hunk's ``@@`` line numbers
    authoritative. That is safe for a lone hunk, but across several hunks
    git adds its own running offset, so a number the model already adjusted
    is counted twice and the hunk lands on the wrong line.
    """

    hunks: dict[str, list[tuple[str, bool]]] = {}
    path = ""
    header = ""
    skip = False
    has_context = False

    def _record() -> None:
        if header and not skip:
            hunks.setdefault(path, []).append((header, has_context))

    for line in patch.splitlines():
        if line.startswith("--- "):
            _record()
            header = ""
            skip = line[4:].split("\t", maxsplit=1)[0].strip() == "/dev/null"
        elif line.startswith("+++ "):
            path = line[4:].split("\t", maxsplit=1)[0].strip().removeprefix("b/")
            skip = skip or path == "/dev/null"
        elif line.startswith("@@"):
            _record()
            header = line
            has_context = False
        elif header and (line.startswith(" ") or line == ""):
            has_context = True
    _record()

    for target, entries in hunks.items():
        if len(entries) < 2:
            continue
        bare = [header for header, context in entries if not context]
        if bare:
            raise CodingWorkflowError(
                f"{target} has {len(entries)} hunks and {len(bare)} of them carry no "
                f"context lines (first: '{bare[0]}'). In a multi-hunk file diff the "
                "line numbers cannot be trusted, so every hunk must include at least "
                "three unchanged lines before and after the change to anchor it by "
                "content."
            )


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Check and apply a unified diff, invalidating stale checks afterward."""

    params = Parameters.model_validate(ctx.parameters)
    patch = normalize_patch(params.patch)
    patch = separate_file_sections(patch)
    try:
        config = get_coding_config(ctx)
        if len(patch.encode("utf-8")) > config.max_patch_bytes:
            raise CodingWorkflowError("Patch exceeds the configured byte limit.")
        affected_paths = patch_paths(patch, config=config)
        reject_unanchored_hunks(patch)
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
            claim=True,
        )
        worktree = artifact_worktree(artifact, config=config)
        checked = await run_git(
            worktree,
            ("apply", "--check", *_APPLY_FLAGS, "-"),
            config=config,
            stdin_text=patch,
        )
        if checked.return_code != 0:
            raise CodingWorkflowError(checked.stderr.strip() or "git apply --check failed")
        applied = await run_git(
            worktree,
            ("apply", *_APPLY_FLAGS, "-"),
            config=config,
            stdin_text=patch,
        )
        if applied.return_code != 0:
            raise CodingWorkflowError(applied.stderr.strip() or "git apply failed")
        artifact.revision += 1
        artifact.changed_paths = list(dict.fromkeys([*artifact.changed_paths, *affected_paths]))
        artifact.checks = {}
        artifact.status = "patched"
        await ctx.code_change_artifact_store.save(artifact)
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=(
            f"Applied patch to {len(affected_paths)} paths at revision {artifact.revision}."
        ),
        output_json={
            "artifact_id": artifact.artifact_id,
            "revision": artifact.revision,
            "changed_paths": affected_paths,
        },
    )
