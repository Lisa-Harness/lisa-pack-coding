"""Import one upstream commit's diff into an owned worktree as one patch."""

from __future__ import annotations

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.tools.apply_patch.apply_patch import _APPLY_FLAGS, patch_paths
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    load_owned_artifact,
    run_git,
)
from pydantic import BaseModel, Field

TOOL_ID = "coding_import_commits"


class Parameters(BaseModel):
    """Structured upstream import request."""

    artifact_id: str = Field(min_length=1, max_length=100)
    import_commit: str = Field(
        max_length=200,
        description=(
            "Git revision to replay: the full or short SHA, branch, tag, or"
            " HEAD reference the artifact was anchored to when created."
        ),
    )


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Apply an upstream commit's diff to the worktree, bump the revision, and record it.

    ``import_commit`` accepts any Git revision expression (full or short SHA,
    branch, tag, HEAD); it resolves to that revision's full commit SHA, and
    every guard compares canonical SHAs, so an artifact anchored at ``HEAD``
    imports cleanly from the bare SHA or an equivalent reference. The worktree
    is created at ``import_commit``'s parent (see ``coding_create_worktree``),
    so its tree matches that commit; ``git diff
    import_commit^..import_commit`` is therefore exactly the change the
    commit introduces, and applying it reproduces the commit's content.
    """

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        source_root = config.resolve_workspace_root()
        artifact = await load_owned_artifact(
            ctx,
            artifact_id=params.artifact_id,
            config=config,
            claim=True,
        )
        # Resolve to the canonical full SHA first: the guards then compare
        # canonical forms, so an artifact anchored via one revision text
        # (HEAD) imports cleanly with any equivalent reference to the same
        # commit, and the artifact records the resolved SHA whatever text the
        # caller passed.
        worktree = artifact_worktree(artifact, config=config)
        created = await run_git(
            source_root,
            ("rev-parse", "--verify", "--quiet", f"{params.import_commit}^{{commit}}"),
            config=config,
        )
        import_commit = created.stdout.strip() if created.return_code == 0 else ""
        # An anchored artifact keeps its recorded-anchor error for an unknown
        # revision, matching the guard order before resolution existed.
        if not import_commit:
            if artifact.import_commit:
                raise CodingWorkflowError(
                    "Artifact must import the commit recorded at worktree creation."
                )
            raise CodingWorkflowError(f"Unknown import commit '{params.import_commit}'.")
        if import_commit in artifact.imported_commits:
            raise CodingWorkflowError(
                "Upstream commit was already imported; commit or export patches first."
            )
        if artifact.import_commit and artifact.import_commit != import_commit:
            raise CodingWorkflowError(
                "Artifact must import the commit recorded at worktree creation."
            )
        parent = await run_git(
            source_root,
            ("rev-parse", "--verify", "--quiet", f"{params.import_commit}^"),
            config=config,
        )
        if parent.return_code != 0 or not parent.stdout.strip():
            raise CodingWorkflowError(
                "Import commit has no parent; the worktree base cannot match its parent."
            )
        parent_sha = parent.stdout.strip()
        if parent_sha != artifact.base_sha:
            raise CodingWorkflowError(
                "The artifact must branch from the import commit's parent: "
                f"expected {parent_sha}, got {artifact.base_sha}."
            )
        recorded = await run_git(
            worktree,
            ("rev-parse", "HEAD"),
            config=config,
        )
        if recorded.return_code != 0:
            raise CodingWorkflowError(
                recorded.stderr.strip() or "Cannot resolve the worktree HEAD."
            )
        base = recorded.stdout.strip()
        if base != artifact.base_sha:
            raise CodingWorkflowError(
                "Owned worktree HEAD no longer matches the recorded creation base."
            )
        diff = await run_git(
            source_root,
            ("diff", f"{parent_sha}..{params.import_commit}", "--full-index", "--binary"),
            config=config,
            stdin_text=None,
        )
        if diff.return_code != 0:
            raise CodingWorkflowError(diff.stderr.strip() or "git diff failed")
        patch = diff.stdout
        if not patch.strip():
            raise CodingWorkflowError("Import commit is empty: it introduces no changes.")
        if len(patch.encode("utf-8")) > config.max_patch_bytes:
            raise CodingWorkflowError("Imported diff exceeds the configured patch byte limit.")
        affected_paths = patch_paths(patch, config=config)
        apply = await run_git(
            worktree,
            ("apply", "--check", *_APPLY_FLAGS, "-"),
            config=config,
            stdin_text=patch,
        )
        if apply.return_code != 0:
            raise CodingWorkflowError(
                apply.stderr.strip() or "git apply --check of the imported diff failed"
            )
        checked = await run_git(
            worktree,
            ("apply", *_APPLY_FLAGS, "-"),
            config=config,
            stdin_text=patch,
        )
        if checked.return_code != 0:
            raise CodingWorkflowError(checked.stderr.strip() or "git apply failed")
        artifact.imported_commits = {
            **artifact.imported_commits,
            import_commit: base,
        }
        artifact.import_commit = import_commit
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
            f"Imported upstream commit {import_commit} onto {artifact.branch} "
            f"at revision {artifact.revision}."
        ),
        output_json={
            "artifact_id": artifact.artifact_id,
            "import_commit": import_commit,
            "base_sha": base,
            "changed_paths": affected_paths,
            "branch": artifact.branch,
            "revision": artifact.revision,
        },
    )