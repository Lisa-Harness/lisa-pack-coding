"""Keep an owned worktree rebased onto its upstream tip.

Every mutation and verification starts here, so edits land on current code
instead of on a base the source checkout has already moved past. The sync is
conservative: it only follows forward upstream movement, never rewrites pushed
commits, never leaves a half-applied rebase, and carries uncommitted
task-owned edits across the rebase as a temporary commit that is unwound
afterward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from kernel.models import ToolExecutionContext
from sdk.workproducts import WorkProduct

from lisa_packs.coding.config import CodingPackConfig
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    checkpoint_prefix,
    run_git,
)

__all__ = [
    "SyncOutcome",
    "resolve_upstream_tip",
    "sync_and_save",
    "sync_with_upstream",
    "with_sync_note",
]

SyncStatus = Literal[
    "current", "synced", "skipped", "diverged", "pushed", "blocked", "conflict"
]

_WIP_MESSAGE = "lisa: carry uncommitted edits across upstream sync"


@dataclass(slots=True)
class SyncOutcome:
    """What the upstream sync did and what the caller should tell the model."""

    status: SyncStatus
    upstream_sha: str = ""
    previous_base_sha: str = ""
    upstream_commits: int = 0
    #: Task-owned paths upstream also changed; reread them before editing.
    overlapping_paths: list[str] = field(default_factory=list)
    conflict_paths: list[str] = field(default_factory=list)
    detail: str = ""

    def note(self) -> str:
        """One model-facing sentence, empty when nothing is worth saying."""

        short = self.upstream_sha[:12]
        if self.status == "synced":
            text = (
                f"Rebased onto upstream {short} ({self.upstream_commits} new "
                "upstream commit(s))."
            )
            if self.overlapping_paths:
                text += (
                    " Upstream also changed "
                    + ", ".join(self.overlapping_paths)
                    + "; reread them before editing."
                )
            return text
        if self.status == "conflict":
            return (
                f"Upstream {short} conflicts with this work in "
                + ", ".join(self.conflict_paths or ["unknown paths"])
                + "; the branch stays on its previous base and will not merge "
                "cleanly. Read the upstream versions with coding_read_file "
                f"revision={short} and report the conflict."
            )
        if self.status in {"diverged", "pushed", "blocked"}:
            return f"Upstream sync skipped: {self.detail}"
        return ""

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "upstream_sha": self.upstream_sha,
            "previous_base_sha": self.previous_base_sha,
            "upstream_commits": self.upstream_commits,
            "overlapping_paths": self.overlapping_paths,
            "conflict_paths": self.conflict_paths,
            "detail": self.detail,
        }


async def _git_ok(worktree: Path, config: CodingPackConfig, *args: str) -> str:
    result = await run_git(worktree, args, config=config)
    if result.return_code != 0:
        raise CodingWorkflowError(
            result.stderr.strip() or f"git {' '.join(args[:2])} failed"
        )
    return result.stdout.strip()


async def _is_ancestor(
    root: Path, config: CodingPackConfig, ancestor: str, descendant: str
) -> bool:
    result = await run_git(
        root, ("merge-base", "--is-ancestor", ancestor, descendant), config=config
    )
    if result.return_code not in (0, 1):
        raise CodingWorkflowError(
            result.stderr.strip() or "git merge-base --is-ancestor failed"
        )
    return result.return_code == 0


async def _names(root: Path, config: CodingPackConfig, *args: str) -> list[str]:
    # ``-z`` must precede any ``--`` pathspec separator to stay an option.
    split = args.index("--") if "--" in args else len(args)
    result = await run_git(root, (*args[:split], "-z", *args[split:]), config=config)
    if result.return_code != 0:
        return []
    return [name for name in result.stdout.split("\0") if name]


async def resolve_upstream_tip(
    source_root: Path, *, config: CodingPackConfig
) -> str:
    """Return the commit owned worktrees branch from and stay rebased onto."""

    ref = f"refs/heads/{config.base_branch}" if config.base_branch else "HEAD"
    result = await run_git(
        source_root, ("rev-parse", "--verify", f"{ref}^{{commit}}"), config=config
    )
    if result.return_code != 0:
        raise CodingWorkflowError(
            f"Cannot resolve upstream {config.base_branch or 'source HEAD'}."
        )
    return result.stdout.strip()


async def _undo_wip(worktree: Path, config: CodingPackConfig, wip: bool) -> None:
    if wip:
        await _git_ok(worktree, config, "reset", "--mixed", "HEAD~1")


async def sync_with_upstream(
    artifact: WorkProduct, *, worktree: Path, config: CodingPackConfig
) -> SyncOutcome:
    """Rebase the owned branch, plus uncommitted owned edits, onto upstream.

    Mutates ``artifact`` (base, head, revision, checks) only on success; the
    caller persists it. Every refusal leaves the branch and working tree
    exactly as they were.
    """

    if artifact.import_commit:
        # The import anchor requires the base to stay the imported commit's
        # parent until the import is committed.
        return SyncOutcome(status="skipped", detail="pending upstream import")
    source_root = config.resolve_workspace_root()
    tip = await resolve_upstream_tip(source_root, config=config)
    base = artifact.base_sha
    outcome = SyncOutcome(status="current", upstream_sha=tip, previous_base_sha=base)
    if not base or tip == base or await _is_ancestor(source_root, config, tip, base):
        return outcome
    if not await _is_ancestor(source_root, config, base, tip):
        outcome.status = "diverged"
        outcome.detail = (
            f"upstream {tip[:12]} does not descend from the artifact base "
            f"{base[:12]} (the source checkout switched branches?)."
        )
        return outcome
    head = await _git_ok(worktree, config, "rev-parse", "HEAD")
    if artifact.pushed_sha and await _is_ancestor(
        worktree, config, artifact.pushed_sha, head
    ):
        outcome.status = "pushed"
        outcome.detail = "the branch is already pushed; rebasing would diverge it."
        return outcome
    branch = await _git_ok(worktree, config, "branch", "--show-current")
    if branch != artifact.branch:
        outcome.status = "blocked"
        outcome.detail = "the worktree is not on the recorded feature branch."
        return outcome
    owned = set(artifact.changed_paths)
    foreign = [
        path
        for path in await _names(
            worktree, config, "diff", "HEAD", "--name-only", "--no-renames"
        )
        if path not in owned
    ]
    if foreign:
        outcome.status = "blocked"
        outcome.detail = "tracked edits outside the task: " + ", ".join(foreign[:10])
        return outcome

    upstream_paths = await _names(
        worktree, config, "diff", "--name-only", "--no-renames", base, tip
    )
    outcome.overlapping_paths = [path for path in upstream_paths if path in owned]
    outcome.upstream_commits = int(
        await _git_ok(worktree, config, "rev-list", "--count", f"{base}..{tip}")
        or "0"
    )
    owned_commits = int(
        await _git_ok(worktree, config, "rev-list", "--count", f"{base}..{head}")
        or "0"
    )
    if owned_commits:
        name = (
            f"{checkpoint_prefix(config)}{artifact.artifact_id[:8]}"
            f"-r{artifact.revision}-{uuid4().hex[:8]}"
        )
        await _git_ok(worktree, config, "tag", name, head)

    wip = False
    dirty_owned = (
        await _names(
            worktree,
            config,
            "--literal-pathspecs",
            "ls-files",
            "--modified",
            "--deleted",
            "--others",
            "--exclude-standard",
            "--",
            *sorted(owned),
        )
        if owned
        else []
    )
    if dirty_owned:
        await _git_ok(
            worktree,
            config,
            "--literal-pathspecs",
            "add",
            "-A",
            "--",
            *sorted(set(dirty_owned)),
        )
        await _git_ok(worktree, config, "commit", "--no-verify", "-m", _WIP_MESSAGE)
        wip = True
    before = await _git_ok(worktree, config, "rev-parse", "HEAD")

    replay = await run_git(
        worktree,
        ("rebase", "--empty=drop", "--onto", tip, base, artifact.branch),
        config=config,
    )
    if replay.return_code != 0:
        outcome.conflict_paths = await _names(
            worktree, config, "diff", "--name-only", "--diff-filter=U"
        )
        await run_git(worktree, ("rebase", "--abort"), config=config)
        if await _git_ok(worktree, config, "rev-parse", "HEAD") != before:
            raise CodingWorkflowError(
                "Upstream sync failed and the worktree could not be restored; "
                "inspect the worktree."
            )
        await _undo_wip(worktree, config, wip)
        outcome.status = "conflict" if outcome.conflict_paths else "blocked"
        outcome.detail = replay.stderr.strip()[-500:]
        return outcome

    replayed = int(
        await _git_ok(worktree, config, "rev-list", "--count", f"{tip}..HEAD") or "0"
    )
    if replayed < owned_commits + int(wip):
        # Upstream already holds part of this work; dropping it silently would
        # lose the task's own record of the change.
        await _git_ok(worktree, config, "reset", "--hard", before)
        await _undo_wip(worktree, config, wip)
        outcome.status = "blocked"
        outcome.detail = "upstream already contains part of this change."
        return outcome
    await _undo_wip(worktree, config, wip)

    new_head = await _git_ok(worktree, config, "rev-parse", "HEAD")
    artifact.base_sha = tip
    artifact.head_sha = new_head
    if owned_commits:
        artifact.commit_sha = new_head
    artifact.revision += 1
    artifact.checks = {}
    if wip:
        artifact.status = "patched"
    outcome.status = "synced"
    return outcome


async def sync_and_save(
    ctx: ToolExecutionContext,
    artifact: WorkProduct,
    *,
    worktree: Path,
    config: CodingPackConfig,
) -> SyncOutcome:
    """Sync before a mutation and persist the rebased artifact right away."""

    outcome = await sync_with_upstream(artifact, worktree=worktree, config=config)
    if outcome.status == "synced":
        await ctx.code_change_artifact_store.save(artifact)
    return outcome


def with_sync_note(text: str, outcome: SyncOutcome | None) -> str:
    """Prefix ``text`` with the sync note, which often explains the result
    (a stale ``expected_sha256`` right after a rebase, say)."""

    note = outcome.note() if outcome is not None else ""
    return f"{note} {text}" if note else text
