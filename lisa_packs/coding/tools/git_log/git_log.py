"""Read bounded commit history from the source workspace or an owned worktree."""

from __future__ import annotations

from typing import Literal

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    relative_path,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.tools.git_read_helpers import resolve_commit
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    readable_root,
    run_git,
    validate_artifact_id,
)
from pydantic import BaseModel, Field, field_validator, model_validator

TOOL_ID = "coding_git_log"


class Parameters(BaseModel):
    """Structured bounded history request."""

    artifact_id: str | None = Field(default=None, min_length=1, max_length=100)
    scope: Literal["auto", "source", "worktree"] = "auto"
    revision: str | None = Field(default="HEAD", min_length=1, max_length=256)
    base: str | None = Field(default=None, min_length=1, max_length=256)
    head: str | None = Field(default=None, min_length=1, max_length=256)
    paths: list[str] = Field(default_factory=list, max_length=50)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=30, ge=1, le=100)

    @field_validator("artifact_id", mode="before")
    @classmethod
    def _reject_mistyped_artifact_id(cls, value: object) -> object:
        return validate_artifact_id(value)

    @model_validator(mode="after")
    def _validate_range(self) -> Parameters:
        if (self.base is None) != (self.head is None):
            raise ValueError("log ranges require both base and head.")
        if self.base is not None and self.revision not in (None, "HEAD"):
            raise ValueError(
                "provide either a revision or a base/head range, not both."
            )
        return self


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Return commit records with explicit pagination and resolved revision IDs."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        read_root = await readable_root(
            ctx, artifact_id=params.artifact_id, scope=params.scope, config=config
        )
        pathspecs = []
        for requested_path in params.paths:
            path = resolve_relative_path_at_root(read_root.path, requested_path)
            pathspecs.append(f":(literal){relative_path(read_root.path, path)}")
        if params.base is not None and params.head is not None:
            base_sha = await resolve_commit(read_root.path, params.base, config=config)
            head_sha = await resolve_commit(read_root.path, params.head, config=config)
            revisions = (f"{base_sha}..{head_sha}",)
        else:
            revision = await resolve_commit(
                read_root.path, params.revision or "HEAD", config=config
            )
            base_sha = None
            head_sha = revision
            revisions = (revision,)
        result = await run_git(
            read_root.path,
            (
                "log",
                f"--skip={params.offset}",
                f"--max-count={params.limit}",
                "--format=%H%x1f%P%x1f%s%x1f%an%x1f%aI",
                *revisions,
                *(("--", *pathspecs) if pathspecs else ()),
            ),
            config=config,
        )
        if result.return_code != 0:
            raise CodingWorkflowError(result.stderr.strip() or "Git log failed.")
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    commits: list[dict[str, str | list[str]]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\x1f", 4)
        if len(fields) == 5:
            sha, parents, subject, author, authored_at = fields
            commits.append(
                {
                    "sha": sha,
                    "parents": parents.split() if parents else [],
                    "subject": subject,
                    "author": author,
                    "authored_at": authored_at,
                }
            )
    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Read {len(commits)} commits.",
        output_json={
            "scope": read_root.scope,
            "artifact_id": read_root.artifact_id,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "commits": commits,
            "offset": params.offset,
            "limit": params.limit,
            "next_offset": params.offset + len(commits)
            if result.output_truncated or len(commits) == params.limit
            else None,
            "output_truncated": result.output_truncated,
        },
    )
