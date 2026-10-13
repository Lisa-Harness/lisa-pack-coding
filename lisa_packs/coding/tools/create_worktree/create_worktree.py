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
    #: Optional base tip: branch at this Git revision instead of the source HEAD
    #: (e.g. an upstream commit meant for coding_import_commits). Any commit-ish
    #: revision expression is accepted and verified; the resolved full commit
    #: SHA is what gets recorded on the artifact.
    import_commit: str = Field(
        default="",
        max_length=200,
        description=(
            "Optional Git revision (full or short SHA, branch, tag, or HEAD) the"
            " worktree base anchors to for coding_import_commits; resolved to"
            " that revision's full commit SHA. Empty branches at the source HEAD."
        ),
    )


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
        import_commit = ""
        if params.import_commit:
            exists = await run_git(
                source_root,
                ("rev-parse", "--verify", "--quiet", f"{params.import_commit}^{{commit}}"),
                config=config,
            )
            if exists.return_code != 0 or not exists.stdout.strip():
                raise CodingWorkflowError(f"Unknown import commit '{params.import_commit}'.")
            resolved = await run_git(
                source_root,
                ("rev-parse", "--verify", "--quiet", f"{params.import_commit}^"),
                config=config,
            )
            if resolved.return_code != 0 or not resolved.stdout.strip():
                raise CodingWorkflowError(
                    "import_commit has no parent; the worktree base cannot match it."
                )
            base = resolved.stdout.strip()
            import_commit = exists.stdout.strip()
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
            import_commit=import_commit,
            safe_summary=params.summary.strip(),
        )
        if (
            artifact.branch in config.protected_branches
            or slug in config.protected_branches
        ):
            raise CodingWorkflowError(
                "Configured feature branch conflicts with a protected branch."
            )
        worktree_parent = config.resolve_worktree_parent(
            data_dir=ctx.data_dir, source_root=source_root
        ).resolve()
        # The worktree must never land inside the source tree: the source
        # checkout stays untouched and the artifact owns one tree only.
        if worktree_parent.is_relative_to(source_root):
            raise CodingWorkflowError(
                "Resolved worktree directory must be outside the source workspace."
            )
        # Unnamed worktrees keep the artifact-id directory (historical layout);
        # named worktrees use the slug so the tree is self-describing.
        worktree_path = (
            worktree_parent / (slug if named else artifact.artifact_id)
        ).resolve()
        if worktree_path.is_relative_to(source_root):
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
