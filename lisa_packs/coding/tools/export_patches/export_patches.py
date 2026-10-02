"""Export an owned committed artifact as a ``git format-patch`` series."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import CodingPackConfig, get_coding_config
from lisa_packs.coding.workflow import CodingWorkflowError, load_owned_artifact, run_git
from pydantic import BaseModel, Field

TOOL_ID = "coding_export_patches"

_SERIES_FILE = re.compile(r"^\d{4}-.*\.patch$")


class Parameters(BaseModel):
    """Structured patch-series export request."""

    artifact_id: str = Field(min_length=1, max_length=100)


def _export_dir(config: CodingPackConfig, *, source_root: Path, artifact_id: str) -> Path:
    if config.patch_export_dir:
        parent = Path(config.patch_export_dir).expanduser()
        if not parent.is_absolute():
            raise CodingWorkflowError("patch_export_dir must be an absolute path.")
    else:
        parent = source_root.parent / ".lisa-patches"
    target = (parent / artifact_id).resolve()
    if target.is_relative_to(source_root):
        raise CodingWorkflowError("Patch export directory must be outside the source workspace.")
    return target


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Write ``base_sha..commit_sha`` as mailbox patches that ``git am`` can apply."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        source_root = config.resolve_workspace_root()
        # The worktree shares the source object store, so export still works after cleanup.
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
            require_worktree=False,
        )
        if Path(artifact.source_root).resolve() != source_root:
            raise CodingWorkflowError(
                "Artifact source root does not match the configured workspace."
            )
        if not artifact.commit_sha or not artifact.base_sha:
            raise CodingWorkflowError("Artifact has no committed revision to export.")
        ancestor = await run_git(
            source_root,
            ("merge-base", "--is-ancestor", artifact.base_sha, artifact.commit_sha),
            config=config,
        )
        if ancestor.return_code != 0:
            raise CodingWorkflowError("Artifact base is not an ancestor of its recorded commit.")
        commit_range = f"{artifact.base_sha}..{artifact.commit_sha}"
        target = _export_dir(config, source_root=source_root, artifact_id=artifact.artifact_id)
        target.mkdir(parents=True, exist_ok=True)
        for stale in target.iterdir():
            if stale.is_file() and not stale.is_symlink() and _SERIES_FILE.match(stale.name):
                stale.unlink()
        exported = await run_git(
            source_root,
            (
                "format-patch",
                f"--base={artifact.base_sha}",
                "--output-directory",
                str(target),
                commit_range,
            ),
            config=config,
        )
        if exported.return_code != 0:
            raise CodingWorkflowError(exported.stderr.strip() or "git format-patch failed")
        patch_files = [line.strip() for line in exported.stdout.splitlines() if line.strip()]
        if not patch_files:
            raise CodingWorkflowError("Artifact commit range produced no patches.")
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    apply_command = f"git am -3 {shlex.quote(str(target))}/*.patch"
    return ToolResult(
        tool_id=TOOL_ID,
        output_text=(
            f"Exported {len(patch_files)} patch(es) for {artifact.branch} to {target}.\n"
            f"Apply in your checkout with: {apply_command}"
        ),
        output_json={
            "artifact_id": artifact.artifact_id,
            "branch": artifact.branch,
            "base_sha": artifact.base_sha,
            "commit_sha": artifact.commit_sha,
            "export_dir": str(target),
            "patch_files": patch_files,
            "apply_command": apply_command,
        },
    )
