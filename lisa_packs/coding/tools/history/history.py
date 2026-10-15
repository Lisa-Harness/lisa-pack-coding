"""Rewrite owned branch history with bounded, checkpoint-guarded operations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field
from sdk.workproducts import WorkProduct

from lisa_packs.coding.config import CodingPackConfig, get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_branch_is_allowed,
    artifact_worktree,
    load_owned_artifact,
    run_git,
)

TOOL_ID = "coding_history"

#: Tags are refs: reject anything a ref name must not contain. Git is the
#: backstop for rarer shapes (``@{``, control characters).
_TAG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

_REWRITE_OPS = frozenset({"restore", "reword", "squash", "rebase"})


class Parameters(BaseModel):
    """Structured owned-history rewrite request."""

    artifact_id: str = Field(min_length=1, max_length=100)
    op: Literal["tag", "tag_delete", "restore", "reword", "squash", "rebase"]
    #: Tag name for ``tag``/``tag_delete``; the full checkpoint tag for ``restore``.
    name: str = Field(default="", max_length=200)
    #: 40-hex commit: tag target, squash ``upto`` commit (default: the base),
    #: or rebase ``new_base``.
    target: str = Field(default="", pattern=r"^$|^[0-9a-f]{40}$")
    #: Required message for ``squash``/``reword`` and for annotated ``tag``.
    message: str = ""
    #: Create an annotated tag (requires ``message``).
    annotated: bool = False


def _checkpoint_prefix(config: CodingPackConfig) -> str:
    """Return the reserved checkpoint-tag namespace under the branch prefix."""

    normalized = (
        config.branch_prefix
        if config.branch_prefix.endswith("/")
        else f"{config.branch_prefix}/"
    )
    return f"{normalized}ckpt/"


def _validate_tag_name(name: str, *, checkpoint_prefix: str) -> str:
    """Reject tag names the tool cannot safely address or would shadow."""

    if (
        not name
        or not _TAG_NAME_RE.match(name)
        or name.endswith((".lock", "/", "."))
        or ".." in name
    ):
        raise CodingWorkflowError(
            "tag name must match [A-Za-z0-9][A-Za-z0-9._/-]* with no '..', "
            "'.lock' suffix, or trailing '.' or '/'."
        )
    # Git's D/F rule: a ref at any directory component of the checkpoint
    # prefix (``lisa``, ``a/b``) blocks every ref below it, which would make
    # later checkpoints — and therefore every rewrite op — impossible.
    stem = checkpoint_prefix.rstrip("/")
    segments = stem.split("/")
    reserved = {
        stem,
        *("/".join(segments[:index]) for index in range(1, len(segments))),
    }
    if name.startswith(checkpoint_prefix) or name in reserved:
        raise CodingWorkflowError(
            f"Tag name collides with the reserved checkpoint namespace "
            f"'{checkpoint_prefix}'."
        )
    return name


async def _resolve_commit(
    worktree: Path, config: CodingPackConfig, rev: str, *, label: str
) -> str:
    """Resolve one commit-ish or fail with a tool-level message."""

    result = await run_git(
        worktree, ("rev-parse", "--verify", f"{rev}^{{commit}}"), config=config
    )
    if result.return_code != 0:
        raise CodingWorkflowError(f"Unknown {label} '{rev}'.")
    return result.stdout.strip()


async def _is_ancestor(
    worktree: Path, config: CodingPackConfig, ancestor: str, descendant: str
) -> bool:
    """Return whether ``ancestor`` is reachable from ``descendant``."""

    result = await run_git(
        worktree, ("merge-base", "--is-ancestor", ancestor, descendant), config=config
    )
    if result.return_code not in (0, 1):
        raise CodingWorkflowError(
            result.stderr.strip() or "git merge-base --is-ancestor failed"
        )
    return result.return_code == 0


async def _head_of(worktree: Path, *, config: CodingPackConfig) -> str:
    result = await run_git(worktree, ("rev-parse", "HEAD"), config=config)
    if result.return_code != 0:
        raise CodingWorkflowError(result.stderr.strip() or "Cannot resolve HEAD.")
    return result.stdout.strip()


async def _head_tree(worktree: Path, *, config: CodingPackConfig) -> str:
    result = await run_git(worktree, ("rev-parse", "HEAD^{tree}"), config=config)
    if result.return_code != 0:
        raise CodingWorkflowError(result.stderr.strip() or "Cannot resolve HEAD tree.")
    return result.stdout.strip()


async def _tag_exists(worktree: Path, config: CodingPackConfig, name: str) -> bool:
    result = await run_git(
        worktree, ("rev-parse", "--verify", "-q", f"refs/tags/{name}"), config=config
    )
    return result.return_code == 0


async def _commit_count(
    worktree: Path, config: CodingPackConfig, commit_range: str
) -> int:
    result = await run_git(
        worktree, ("rev-list", "--count", commit_range), config=config
    )
    if result.return_code != 0:
        raise CodingWorkflowError(result.stderr.strip() or "git rev-list failed")
    return int(result.stdout.strip() or "0")


async def _require_rewritable_state(
    artifact: WorkProduct, worktree: Path, *, config: CodingPackConfig
) -> tuple[str, str]:
    """Gate every op on a committed, tracked-clean, branch-attached worktree."""

    if not artifact.commit_sha:
        raise CodingWorkflowError(
            "Artifact has no committed revision; commit before history operations."
        )
    current_branch = await run_git(
        worktree, ("branch", "--show-current"), config=config
    )
    if (
        current_branch.return_code != 0
        or current_branch.stdout.strip() != artifact.branch
    ):
        raise CodingWorkflowError(
            "Owned worktree is not on the recorded feature branch."
        )
    head = await _head_of(worktree, config=config)
    if head != artifact.commit_sha:
        raise CodingWorkflowError(
            "Owned worktree HEAD no longer matches the recorded commit."
        )
    if not artifact.base_sha:
        raise CodingWorkflowError("Artifact has no recorded base commit.")
    clean = await run_git(worktree, ("diff", "HEAD", "--quiet"), config=config)
    if clean.return_code != 0:
        raise CodingWorkflowError(
            "Tracked changes must be committed before history operations."
        )
    return head, artifact.base_sha


async def _resolve_owned_commit(
    worktree: Path,
    config: CodingPackConfig,
    rev: str,
    *,
    base: str,
    head: str,
    label: str,
    allow_base: bool = False,
) -> str:
    """Resolve a commit that lives in the owned ``base..head`` range."""

    sha = await _resolve_commit(worktree, config, rev, label=label)
    if sha == base and not allow_base:
        raise CodingWorkflowError(
            f"{label} must be an owned commit (base is excluded); got '{rev}'."
        )
    if not await _is_ancestor(worktree, config, sha, head) or not await _is_ancestor(
        worktree, config, base, sha
    ):
        raise CodingWorkflowError(
            f"{label} must be a commit between the artifact base and HEAD; got '{rev}'."
        )
    return sha


async def _reject_pushed_rewrite(
    worktree: Path, artifact: WorkProduct, *, config: CodingPackConfig, head: str
) -> None:
    """History may never rewrite commits the configured remote already holds."""

    if artifact.pushed_sha and await _is_ancestor(
        worktree, config, artifact.pushed_sha, head
    ):
        raise CodingWorkflowError(
            "The branch was already pushed; rewriting its history would diverge "
            f"from '{config.push_remote}'. Export patches instead."
        )


async def _checkpoint(
    worktree: Path, artifact: WorkProduct, *, config: CodingPackConfig, head: str
) -> str:
    """Tag one lightweight checkpoint at ``head`` before any history rewrite."""

    prefix = _checkpoint_prefix(config)
    name = f"{prefix}{artifact.artifact_id[:8]}-r{artifact.revision}-{uuid4().hex[:8]}"
    tagged = await run_git(worktree, ("tag", name, head), config=config)
    if tagged.return_code != 0:
        raise CodingWorkflowError(tagged.stderr.strip() or "git checkpoint tag failed")
    return name


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Apply one bounded history op to the artifact's owned worktree."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx, artifact_id=params.artifact_id, config=config, claim=True
        )
        if not artifact_branch_is_allowed(artifact, config):
            raise CodingWorkflowError(
                "Artifact branch is not an owned unprotected feature branch."
            )
        worktree = artifact_worktree(artifact, config=config)
        head, base = await _require_rewritable_state(artifact, worktree, config=config)
        checkpoint_prefix = _checkpoint_prefix(config)
        tree_before = await _head_tree(worktree, config=config)
        checkpoint_tag = ""
        details: dict[str, object] = {}

        if params.op == "tag":
            name = _validate_tag_name(
                params.name.strip(), checkpoint_prefix=checkpoint_prefix
            )
            if await _tag_exists(worktree, config, name):
                raise CodingWorkflowError(f"Tag '{name}' already exists.")
            # A tag sharing a name with (any) local branch makes the shared
            # refspec ambiguous and bricks coding_push for that branch.
            branch_hit = await run_git(
                worktree,
                ("rev-parse", "--verify", "-q", f"refs/heads/{name}"),
                config=config,
            )
            if branch_hit.return_code == 0:
                raise CodingWorkflowError(
                    f"Tag '{name}' would shadow branch '{name}'; choose another name."
                )
            if params.annotated and not params.message.strip():
                raise CodingWorkflowError("Annotated tags require a message.")
            target = await _resolve_owned_commit(
                worktree,
                config,
                params.target or head,
                base=base,
                head=head,
                label="tag target",
            )
            argv = (
                ("tag", "-a", name, "-m", params.message, target)
                if params.annotated
                else ("tag", name, target)
            )
            tagged = await run_git(worktree, argv, config=config)
            if tagged.return_code != 0:
                raise CodingWorkflowError(tagged.stderr.strip() or "git tag failed")
            details = {"tag": name, "tag_target": target}
        elif params.op == "tag_delete":
            name = params.name.strip()
            if not _TAG_NAME_RE.match(name):
                raise CodingWorkflowError("name must be a valid tag name.")
            if not await _tag_exists(worktree, config, name):
                raise CodingWorkflowError(f"Tag '{name}' does not exist.")
            deleted = await run_git(worktree, ("tag", "-d", name), config=config)
            if deleted.return_code != 0:
                raise CodingWorkflowError(deleted.stderr.strip() or "git tag -d failed")
            details = {"tag": name}
        elif params.op == "restore":
            name = params.name.strip()
            if not await _tag_exists(worktree, config, name):
                raise CodingWorkflowError(f"Tag '{name}' does not exist.")
            restored = await _resolve_commit(
                worktree, config, f"refs/tags/{name}", label="tag"
            )
            if restored == head:
                raise CodingWorkflowError(
                    f"Tag '{name}' matches the current HEAD; nothing to restore."
                )
            if not await _is_ancestor(worktree, config, base, restored):
                raise CodingWorkflowError(
                    f"Tag '{name}' predates the artifact's current base (the history "
                    "was rebased since it was written); it cannot be restored."
                )
            if artifact.pushed_sha and not await _is_ancestor(
                worktree, config, artifact.pushed_sha, restored
            ):
                raise CodingWorkflowError(
                    "Restoring that tag would rewind past the pushed revision."
                )
            checkpoint_tag = await _checkpoint(
                worktree, artifact, config=config, head=head
            )
            reset = await run_git(
                worktree, ("reset", "--hard", restored), config=config
            )
            if reset.return_code != 0:
                raise CodingWorkflowError(
                    reset.stderr.strip() or "git reset --hard to the checkpoint failed"
                )
            details = {"restored_tag": name, "restored_sha": restored}
        elif params.op == "reword":
            if not params.message.strip():
                raise CodingWorkflowError("reword requires a message.")
            if head == base:
                raise CodingWorkflowError("No owned commits exist to reword.")
            await _reject_pushed_rewrite(worktree, artifact, config=config, head=head)
            checkpoint_tag = await _checkpoint(
                worktree, artifact, config=config, head=head
            )
            amended = await run_git(
                worktree, ("commit", "--amend", "-m", params.message), config=config
            )
            if amended.return_code != 0:
                raise CodingWorkflowError(
                    amended.stderr.strip() or "git commit --amend failed"
                )
            details = {"message": params.message}
        elif params.op == "squash":
            if not params.message.strip():
                raise CodingWorkflowError("squash requires a message.")
            if head == base:
                raise CodingWorkflowError("No owned commits exist to squash.")
            upto = (
                await _resolve_owned_commit(
                    worktree,
                    config,
                    params.target,
                    base=base,
                    head=head,
                    label="squash target",
                    allow_base=True,
                )
                if params.target
                else base
            )
            squashed = await _commit_count(worktree, config, f"{upto}..{head}")
            if squashed == 0:
                raise CodingWorkflowError(
                    "No owned commits exist above the squash target."
                )
            await _reject_pushed_rewrite(worktree, artifact, config=config, head=head)
            checkpoint_tag = await _checkpoint(
                worktree, artifact, config=config, head=head
            )
            created = await run_git(
                worktree,
                ("commit-tree", tree_before, "-p", upto, "-F", "-"),
                config=config,
                stdin_text=params.message.strip() + "\n",
            )
            if created.return_code != 0:
                raise CodingWorkflowError(
                    created.stderr.strip() or "git commit-tree failed"
                )
            new_sha = created.stdout.strip()
            moved = await run_git(worktree, ("reset", "--soft", new_sha), config=config)
            if moved.return_code != 0:
                raise CodingWorkflowError(
                    moved.stderr.strip()
                    or "git reset --soft to the squashed commit failed"
                )
            details = {"squashed_commits": squashed, "squashed_into": new_sha}
        else:  # rebase
            if not params.target:
                raise CodingWorkflowError(
                    "rebase requires target: the new base commit."
                )
            if head == base:
                raise CodingWorkflowError("No owned commits exist to rebase.")
            new_base = await _resolve_commit(
                worktree, config, params.target, label="rebase base commit"
            )
            if await _is_ancestor(worktree, config, new_base, head) or new_base == base:
                raise CodingWorkflowError(
                    "rebase target must be outside the owned commit range "
                    "(neither within base..HEAD nor the base itself)."
                )
            rebased_count = await _commit_count(worktree, config, f"{base}..{head}")
            await _reject_pushed_rewrite(worktree, artifact, config=config, head=head)
            checkpoint_tag = await _checkpoint(
                worktree, artifact, config=config, head=head
            )
            # Rebase the branch ref, not the HEAD sha: rebasing a sha would
            # detach HEAD and leave refs/heads/<branch> on the old history.
            replay = await run_git(
                worktree,
                ("rebase", "--empty=drop", "--onto", new_base, base, artifact.branch),
                config=config,
            )
            if replay.return_code != 0:
                aborted = await run_git(worktree, ("rebase", "--abort"), config=config)
                back = await _head_of(worktree, config=config)
                if aborted.return_code == 0 and back == head:
                    raise CodingWorkflowError(
                        "Rebase stopped on a conflict and was aborted; the branch is "
                        "unchanged. Reconcile the upstream difference and retry."
                    )
                if back == head:
                    raise CodingWorkflowError(
                        replay.stderr.strip() or "git rebase failed before it started"
                    )
                raise CodingWorkflowError(
                    "Rebase failed and the worktree could not be restored to HEAD; "
                    "inspect the worktree."
                )
            artifact.base_sha = new_base
            details = {"rebased_commits": rebased_count, "new_base_sha": new_base}

        tree_after = await _head_tree(worktree, config=config)
        tree_preserved = tree_after == tree_before
        if not tree_preserved:
            artifact.checks = {}
        if params.op in _REWRITE_OPS:
            head_now = await _head_of(worktree, config=config)
            branch_ref = await run_git(
                worktree,
                ("rev-parse", "--verify", f"refs/heads/{artifact.branch}"),
                config=config,
            )
            if branch_ref.return_code != 0 or branch_ref.stdout.strip() != head_now:
                raise CodingWorkflowError(
                    "Branch ref does not match the post-rewrite HEAD; refusing "
                    "to record a commit the branch does not point at."
                )
            if head_now == head:
                raise CodingWorkflowError(
                    "History operation did not move the branch head."
                )
            on_branch = await run_git(
                worktree, ("branch", "--show-current"), config=config
            )
            if (
                on_branch.return_code != 0
                or on_branch.stdout.strip() != artifact.branch
            ):
                raise CodingWorkflowError(
                    "The rewrite left the worktree detached from the feature branch."
                )
            artifact.commit_sha = artifact.head_sha = head_now
            artifact.status = "committed"
        await ctx.code_change_artifact_store.save(artifact)
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    if params.op == "tag":
        op_text = f"Tagged {details['tag']} at {details['tag_target']}."
    elif params.op == "tag_delete":
        op_text = f"Deleted tag {details['tag']}."
    elif params.op == "restore":
        op_text = (
            f"Restored {artifact.branch} to {details['restored_sha']} "
            f"(from tag {details['restored_tag']})."
        )
    elif params.op == "reword":
        op_text = f"Reworded the head of {artifact.branch}: {artifact.commit_sha}."
    elif params.op == "squash":
        op_text = (
            f"Squashed {details['squashed_commits']} commit(s) of {artifact.branch} "
            f"into {details['squashed_into']}."
        )
    else:
        op_text = (
            f"Rebased {details['rebased_commits']} commit(s) of {artifact.branch} "
            f"onto {details['new_base_sha']}."
        )
    if checkpoint_tag:
        op_text += f" Previous head checkpointed at {checkpoint_tag}."
    if params.op in _REWRITE_OPS and tree_preserved:
        op_text += " The tree is unchanged, so recorded checks remain valid."

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=op_text,
        output_json={
            "artifact_id": artifact.artifact_id,
            "op": params.op,
            "branch": artifact.branch,
            "revision": artifact.revision,
            "commit_sha": artifact.commit_sha,
            "base_sha": artifact.base_sha,
            "status": artifact.status,
            "checkpoint_tag": checkpoint_tag,
            "tree_preserved": tree_preserved,
            **details,
        },
    )
