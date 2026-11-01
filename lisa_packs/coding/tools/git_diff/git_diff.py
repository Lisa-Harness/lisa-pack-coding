"""Inspect bounded file summaries and patches between Git snapshots."""

from __future__ import annotations

from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import CodingPackConfig, get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    relative_path,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.tools.git_read_helpers import resolve_commit
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    ReadableRoot,
    readable_root,
    run_git,
    validate_artifact_id,
)
from pydantic import BaseModel, Field, field_validator, model_validator

TOOL_ID = "coding_git_diff"


class Parameters(BaseModel):
    """Structured Git comparison request."""

    artifact_id: str | None = Field(default=None, min_length=1, max_length=100)
    scope: Literal["auto", "source", "worktree"] = "auto"
    comparison: Literal["branch", "trees", "commit"] = "branch"
    base: str | None = Field(default=None, min_length=1, max_length=256)
    head: str | None = Field(default=None, min_length=1, max_length=256)
    revision: str | None = Field(default=None, min_length=1, max_length=256)
    paths: list[str] = Field(default_factory=list, max_length=50)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=50, ge=1, le=200)
    include_patches: bool = False

    @field_validator("artifact_id", mode="before")
    @classmethod
    def _reject_mistyped_artifact_id(cls, value: object) -> object:
        return validate_artifact_id(value)

    @model_validator(mode="after")
    def _validate_revision_inputs(self) -> Parameters:
        if self.comparison == "commit":
            if self.revision is None or self.base is not None or self.head is not None:
                raise ValueError("commit comparison requires revision only.")
        elif self.revision is not None or self.base is None or self.head is None:
            raise ValueError(
                "branch and trees comparisons require both base and head only."
            )
        return self


def _parse_name_status(output: str) -> list[dict[str, str]]:
    """Parse Git's NUL-delimited name-status records, including renames."""

    fields = output.split("\0")
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(fields):
                break
            old_path, path = fields[index], fields[index + 1]
            changes.append({"status": status, "old_path": old_path, "path": path})
            index += 2
        else:
            if index >= len(fields):
                break
            changes.append({"status": status, "path": fields[index]})
            index += 1
    return changes


async def _comparison(
    root: ReadableRoot,
    params: Parameters,
    config: CodingPackConfig,
) -> tuple[str, str, str, str | None]:
    if params.comparison == "commit":
        assert params.revision is not None
        commit = await resolve_commit(root.path, params.revision, config=config)
        parents = await run_git(
            root.path, ("rev-list", "--parents", "-n", "1", commit), config=config
        )
        if parents.return_code != 0:
            raise CodingWorkflowError(
                parents.stderr.strip() or f"Cannot inspect parents of {commit}."
            )
        fields = parents.stdout.split()
        if not fields or fields[0] != commit:
            raise CodingWorkflowError(f"Cannot inspect parents of {commit}.")
        if len(fields) > 1:
            return fields[1], fields[1], commit, None
        empty_tree = await run_git(
            root.path,
            ("hash-object", "-t", "tree", "--stdin"),
            config=config,
            stdin_text="",
        )
        if empty_tree.return_code != 0:
            raise CodingWorkflowError(
                empty_tree.stderr.strip() or "Cannot resolve the empty Git tree."
            )
        empty_tree_sha = empty_tree.stdout.strip()
        return empty_tree_sha, empty_tree_sha, commit, None

    assert params.base is not None and params.head is not None
    base = await resolve_commit(root.path, params.base, config=config)
    head = await resolve_commit(root.path, params.head, config=config)
    if params.comparison == "trees":
        return base, base, head, None
    merge_base = await run_git(root.path, ("merge-base", base, head), config=config)
    if merge_base.return_code != 0:
        raise CodingWorkflowError(
            merge_base.stderr.strip()
            or f"No merge base exists between {params.base!r} and {params.head!r}."
        )
    merge_base_sha = merge_base.stdout.strip()
    return base, merge_base_sha, head, merge_base_sha


async def _validated_paths(root: ReadableRoot, requested: list[str]) -> list[str]:
    """Normalize requested paths and reject paths outside the readable root."""

    paths: list[str] = []
    for requested_path in requested:
        path = resolve_relative_path_at_root(root.path, requested_path)
        paths.append(relative_path(root.path, path))
    return paths


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Return a paginated changed-file list and optional complete per-file patches."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        read_root = await readable_root(
            ctx, artifact_id=params.artifact_id, scope=params.scope, config=config
        )
        base_sha, diff_base_sha, head_sha, merge_base = await _comparison(
            read_root, params, config
        )
        paths = await _validated_paths(read_root, params.paths)
        revisions = [diff_base_sha, head_sha]
        pathspecs = [f":(literal){path}" for path in paths]
        diff_args = [
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--relative",
            *revisions,
        ]
        if pathspecs:
            diff_args.extend(("--", *pathspecs))
        names = await run_git(
            read_root.path,
            ("diff", "--name-status", "-z", *diff_args),
            config=config,
        )
        summary = await run_git(
            read_root.path,
            ("diff", "--shortstat", *diff_args),
            config=config,
        )
        if names.return_code != 0 or summary.return_code != 0:
            raise CodingWorkflowError(
                names.stderr.strip() or summary.stderr.strip() or "Git diff failed."
            )
        changes = _parse_name_status(names.stdout)
        selected = changes[params.offset : params.offset + params.limit]
        patches: list[dict[str, object]] = []
        patch_budget = config.max_patch_bytes
        if params.include_patches:
            for change in selected:
                path = change["path"]
                if patch_budget <= 0:
                    patches.append(
                        {
                            "path": path,
                            "diff": "",
                            "complete": False,
                            "reason": "byte_limit",
                        }
                    )
                    continue
                patch = await run_git(
                    read_root.path,
                    (
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--find-renames",
                        "--relative",
                        "--unified=3",
                        *revisions,
                        "--",
                        f":(literal){path}",
                    ),
                    config=config,
                    max_output_bytes=patch_budget + 1,
                )
                if patch.return_code != 0:
                    raise CodingWorkflowError(
                        patch.stderr.strip() or f"Cannot read diff for {path}."
                    )
                patch_size = len(patch.stdout.encode("utf-8"))
                if patch.output_truncated or patch_size > patch_budget:
                    patches.append(
                        {
                            "path": path,
                            "diff": "",
                            "complete": False,
                            "reason": "byte_limit",
                        }
                    )
                    patch_budget = 0
                else:
                    patches.append(
                        {"path": path, "diff": patch.stdout, "complete": True}
                    )
                    patch_budget -= patch_size
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Found {len(changes)} changed files.",
        output_json={
            "comparison": params.comparison,
            "base_sha": base_sha,
            "diff_base_sha": diff_base_sha,
            "head_sha": head_sha,
            "merge_base_sha": merge_base,
            "scope": read_root.scope,
            "artifact_id": read_root.artifact_id,
            "changes": selected,
            "total_changes": len(changes),
            "offset": params.offset,
            "next_offset": params.offset + len(selected)
            if params.offset + len(selected) < len(changes)
            else None,
            "file_list_truncated": names.output_truncated,
            "summary": summary.stdout.strip() or "(no changes)",
            "patches": patches,
            "patch_byte_limit": config.max_patch_bytes
            if params.include_patches
            else None,
        },
    )
