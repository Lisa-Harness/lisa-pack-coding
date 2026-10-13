"""Create one owned feature worktree without mutating the source checkout."""

from __future__ import annotations

import re
from uuid import uuid4

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import CodingWorkflowError, run_git
from pydantic import BaseModel, Field
from sdk.workproducts import WorkProduct

TOOL_ID = "coding_create_worktree"

#: Branch-ref-safe slug: lowercase alnum runs separated by single dashes.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


def _normalize_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    if not normalized or not _SLUG_RE.match(normalized) or len(normalized) > 60:
        raise CodingWorkflowError(
            "name must normalize to a 1-60 char lowercase dash slug (a-z, 0-9)."
        )
    return normalized


class Parameters(BaseModel):
    """Structured worktree creation request."""

    summary: str = ""
    #: Optional model-chosen slug for the branch/worktree (e.g. "auth-refactor");
    #: falls back to a random id so unnamed worktrees stay colliding-free.
    name: str = Field(default="", max_length=100)
    #: Optional base tip: branch at this commit instead of the source HEAD
    #: (e.g. the parent of an upstream commit meant for coding_import_commits).
    import_commit: str = Field(default="", pattern=r"^$|^[0-9a-f]{40}$")


def _branch_name(prefix: str, slug: str) -> str:
    normalized = prefix if prefix.endswith("/") else f"{prefix}/"
    return f"{normalized}{slug}"


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Create and persist an isolated worktree rooted outside the source repository."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        source_root = config.resolve_workspace_root()
        store = ctx.code_change_artifact_store
        if store is None:
            raise CodingWorkflowError("No code change artifact store is available.")
        base = await run_git(source_root, ("rev-parse", "HEAD"), config=config)
        if base.return_code != 0:
            raise CodingWorkflowError(
                base.stderr.strip() or "Cannot resolve the source HEAD."
            )
        base = base.stdout.strip()
        if params.import_commit:
            exists = await run_git(
                source_root,
                ("rev-parse", "--verify", f"{params.import_commit}^{{commit}}"),
                config=config,
            )
            if exists.return_code != 0:
                raise CodingWorkflowError(f"Unknown import commit '{params.import_commit}'.")
            parent = await run_git(
                source_root,
                ("rev-parse", f"{params.import_commit}^"),
                config=config,
            )
            if parent.return_code != 0 or not parent.stdout.strip():
                raise CodingWorkflowError(
                    "import_commit has no parent; the worktree base cannot match it."
                )
            base = parent.stdout.strip()
        named = bool(params.name.strip())
        slug = _normalize_name(params.name) if named else str(uuid4().hex[:12])
        artifact = WorkProduct(
            task_id=ctx.task_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            user_slug=ctx.user_slug,
            surface_type=ctx.surface_type,
            surface_id=ctx.surface_id,
            session_plan_digest=ctx.session_plan_digest,
            source_root=str(source_root),
            base_sha=base,
            head_sha=base,
            branch=_branch_name(config.branch_prefix, slug),
            required_check_ids=list(config.required_check_ids),
            import_commit=params.import_commit or "",
            safe_summary=params.summary.strip(),
        )
        if (
            artifact.branch in config.protected_branches
            or slug in config.protected_branches
        ):
            raise CodingWorkflowError(
                "Configured feature branch conflicts with a protected branch."
            )
        worktree_parent = source_root.parent / ".lisa-worktrees"
        # Unnamed worktrees keep the artifact-id directory (historical layout);
        # named worktrees use the slug so the tree is self-describing.
        worktree_path = (
            worktree_parent / (slug if named else artifact.artifact_id)
        ).resolve()
        try:
            worktree_path.relative_to(source_root)
        except ValueError:
            pass
        else:
            raise CodingWorkflowError(
                "Derived worktree path must be outside the source workspace."
            )
        existing = await run_git(
            source_root,
            ("rev-parse", "--verify", f"refs/heads/{artifact.branch}"),
            config=config,
        )
        if existing.return_code == 0:
            raise CodingWorkflowError(
                f"Branch '{artifact.branch}' already exists; choose another name "
                "or reuse its worktree (coding_status with its artifact_id)."
            )
        if worktree_path.exists():
            raise CodingWorkflowError(
                f"Worktree path '{worktree_path}' already exists on disk."
            )
        created = await run_git(
            source_root,
            (
                "worktree",
                "add",
                "-b",
                artifact.branch,
                str(worktree_path),
                artifact.base_sha,
            ),
            config=config,
        )
        if created.return_code != 0:
            raise CodingWorkflowError(
                created.stderr.strip() or "git worktree add failed"
            )
        artifact.worktree_path = str(worktree_path)
        await store.create(artifact)
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Created worktree {artifact.artifact_id} on branch {artifact.branch}.",
        output_json={
            "artifact_id": artifact.artifact_id,
            "branch": artifact.branch,
            "worktree_path": artifact.worktree_path,
            "base_sha": artifact.base_sha,
            "import_commit": artifact.import_commit,
        },
    )
