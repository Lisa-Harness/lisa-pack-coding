"""Run an artifact's configured checks the way a careful engineer would.

One call syncs the worktree with upstream, prepares the toolchain when its
recipe or manifests went stale, drives the fix checks to a fixed point, then
runs the verify checks in configuration order. A check whose ``needs`` failed
is skipped, and a passing result is reused while none of the check's declared
``inputs`` changed. Reused results are recorded at the current revision and
tree like fresh ones, so the commit gate needs no special case.

The reuse cache lives in the worktree's private Git admin directory, so it
disappears with the worktree and never enters a commit.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from kernel.models import ToolExecutionContext
from sdk.workproducts import WorkProduct, WorkProductCheck, WorkProductCheckStatus

from lisa_packs.coding.config import CodingCheck, CodingPackConfig
from lisa_packs.coding.freshness import SyncOutcome, sync_with_upstream
from lisa_packs.coding.local_files import CodingPathError, resolve_relative_path_at_root
from lisa_packs.coding.tools.prepare_workspace.prepare_workspace import (
    prepare_artifact,
)
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_has_current_required_checks,
    artifact_worktree,
    candidate_tree_sha,
    preparation_is_current,
    restore_tree_paths,
    run_contained,
    run_git,
    tree_changes,
    worktree_tree_sha,
)

__all__ = [
    "CheckOutcome",
    "VerificationAborted",
    "VerifyReport",
    "failure_excerpt",
    "input_matches",
    "plan_checks",
    "verify_artifact",
]

CheckOutcomeStatus = Literal["passed", "failed", "timed_out", "skipped"]

_FAILURE_EXCERPT_BYTES = 2000
_MAX_FIX_PASSES = 3
_CACHE_FILE = "lisa-check-cache.json"
_DIFF_OUTPUT_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class CheckOutcome:
    """One check's result within a verification."""

    check_id: str
    kind: str
    status: CheckOutcomeStatus
    reused: bool = False
    elapsed_seconds: float = 0.0
    return_code: int | None = None
    timed_out: bool = False
    output_truncated: bool = False
    fixed_paths: list[str] = field(default_factory=list)
    #: Failure excerpt or skip reason; empty for a clean pass.
    detail: str = ""

    def summary(self) -> str:
        if self.status == "skipped":
            return f"{self.check_id} skipped ({self.detail})"
        if self.reused:
            return f"{self.check_id} reused"
        text = f"{self.check_id} {self.status} {self.elapsed_seconds:.1f}s"
        if self.fixed_paths:
            text += f" (fixed {', '.join(self.fixed_paths)})"
        return text

    def to_json(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "kind": self.kind,
            "status": self.status,
            "reused": self.reused,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "return_code": self.return_code,
            "timed_out": self.timed_out,
            "output_truncated": self.output_truncated,
            "fixed_paths": self.fixed_paths,
            "detail": self.detail,
        }


class VerificationAborted(CodingWorkflowError):
    """Verification failed after the upstream sync already ran."""

    def __init__(self, message: str, sync: SyncOutcome) -> None:
        super().__init__(message)
        self.sync = sync


@dataclass(slots=True)
class VerifyReport:
    """Everything one verification did, in model-facing and structured form."""

    sync: SyncOutcome
    prepared: bool = False
    outcomes: list[CheckOutcome] = field(default_factory=list)
    fixers_converged: bool = True
    ready_to_commit: bool = False

    @property
    def fixed_paths(self) -> list[str]:
        return list(
            dict.fromkeys(path for item in self.outcomes for path in item.fixed_paths)
        )

    @property
    def passed(self) -> bool:
        return all(item.status == "passed" for item in self.outcomes)

    def text(self, *, revision: int) -> str:
        head = "Checks passed" if self.passed else "Checks FAILED"
        lines = [
            f"{head} at revision {revision}: "
            + "; ".join(item.summary() for item in self.outcomes)
            + "."
        ]
        notes = [self.sync.note()]
        if self.prepared:
            notes.append("Prepared the workspace toolchain.")
        if not self.fixers_converged:
            notes.append(
                f"Fix checks still rewrote files after {_MAX_FIX_PASSES} passes."
            )
        lines.extend(note for note in notes if note)
        for item in self.outcomes:
            if item.detail and item.status in {"failed", "timed_out"}:
                lines.append(f"\n{item.check_id} output:\n{item.detail}")
        return "\n".join(lines)

    def to_json(self, *, artifact: WorkProduct) -> dict[str, object]:
        return {
            "artifact_id": artifact.artifact_id,
            "status": "passed" if self.passed else "failed",
            "revision": artifact.revision,
            "ready_to_commit": self.ready_to_commit,
            "fixed_paths": self.fixed_paths,
            "fixers_converged": self.fixers_converged,
            "prepared": self.prepared,
            "sync": self.sync.to_json(),
            "checks": [item.to_json() for item in self.outcomes],
        }


@lru_cache(maxsize=256)
def _glob_pattern(glob: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            parts.append(".*")
            index += 2
        elif glob[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(glob[index]))
            index += 1
    if glob.endswith("/"):
        # A directory input selects everything below it.
        parts.append(".*")
    return re.compile("".join(parts), re.DOTALL)


def input_matches(path: str, inputs: tuple[str, ...]) -> bool:
    """Whether a repository-relative ``path`` is one of a check's inputs."""

    return any(_glob_pattern(glob).fullmatch(path) for glob in inputs)


def plan_checks(
    config: CodingPackConfig, artifact: WorkProduct, check_ids: list[str]
) -> list[CodingCheck]:
    """Select checks in configuration order: every fixer, the requested (or
    required) checks, and everything those transitively need."""

    by_id = {check.id: check for check in config.checks}
    unknown = sorted(set(check_ids) - set(by_id))
    if unknown:
        configured = ", ".join(by_id) or "none"
        raise CodingWorkflowError(
            f"Unknown configured check(s) {', '.join(unknown)}. "
            f"Configured checks: {configured}."
        )
    selected = set(check_ids or artifact.required_check_ids)
    selected |= {check.id for check in config.checks if check.kind == "fix"}
    pending = list(selected)
    while pending:
        for need in by_id[pending.pop()].needs:
            if need not in selected:
                selected.add(need)
                pending.append(need)
    return [check for check in config.checks if check.id in selected]


def failure_excerpt(stdout: str, stderr: str) -> str:
    """The tail of a failed check's output, so the diagnosis travels with it."""

    combined = (stderr.strip() + "\n" + stdout.strip()).strip()
    if len(combined) > _FAILURE_EXCERPT_BYTES:
        combined = "…\n" + combined[-_FAILURE_EXCERPT_BYTES:]
    return combined


def _editable(worktree: Path, paths: list[str]) -> bool:
    """A fixer may only touch paths the edit tools could also touch."""

    try:
        for path in paths:
            resolve_relative_path_at_root(worktree, path)
    except CodingPathError:
        return False
    return True


def _cache_key(check: CodingCheck, artifact: WorkProduct) -> str:
    preparation = artifact.preparation
    material = {
        "argv": list(check.argv),
        "kind": check.kind,
        "inputs": list(check.inputs),
        "recipe": preparation.recipe_digest if preparation else "",
        "manifests": preparation.dependency_manifest_digest if preparation else "",
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True).encode("utf-8")
    ).hexdigest()


class _ReuseCache:
    """Pack-owned record of the tree each check last passed on."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self.entries: dict[str, dict[str, str]] = {}
        if path is not None:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
            if isinstance(loaded, dict):
                self.entries = {
                    key: value
                    for key, value in loaded.items()
                    if isinstance(value, dict)
                }

    @classmethod
    async def load(cls, worktree: Path, *, config: CodingPackConfig) -> _ReuseCache:
        result = await run_git(worktree, ("rev-parse", "--absolute-git-dir"), config=config)
        if result.return_code != 0 or not result.stdout.strip():
            return cls(None)
        return cls(Path(result.stdout.strip()) / _CACHE_FILE)

    def record(self, check_id: str, *, key: str, tree: str) -> None:
        self.entries[check_id] = {"key": key, "tree": tree}
        self._write()

    def forget(self, check_id: str) -> None:
        if self.entries.pop(check_id, None) is not None:
            self._write()

    def _write(self) -> None:
        if self._path is None:
            return
        temp = self._path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(self.entries, sort_keys=True), encoding="utf-8")
            temp.replace(self._path)
        except OSError:
            self._path = None


async def _reusable(
    check: CodingCheck,
    *,
    key: str,
    tree: str,
    cache: _ReuseCache,
    worktree: Path,
    config: CodingPackConfig,
) -> bool:
    entry = cache.entries.get(check.id)
    if not check.reuse or entry is None or entry.get("key") != key:
        return False
    cached_tree = entry.get("tree", "")
    if cached_tree == tree:
        return True
    if not check.inputs or not cached_tree:
        return False
    diff = await run_git(
        worktree,
        ("diff-tree", "-r", "--no-renames", "--name-only", "-z", cached_tree, tree),
        config=config,
        max_output_bytes=_DIFF_OUTPUT_BYTES,
    )
    if diff.return_code != 0 or diff.output_truncated:
        return False
    return not any(
        input_matches(path, check.inputs) for path in diff.stdout.split("\0") if path
    )


def _record(
    artifact: WorkProduct,
    check: CodingCheck,
    *,
    status: WorkProductCheckStatus,
    tree: str,
    elapsed: float,
    stdout: str,
    stderr: str,
) -> None:
    artifact.checks[check.id] = WorkProductCheck(
        check_id=check.id,
        status=status,
        revision=artifact.revision,
        checked_tree_sha=tree,
        argv=list(check.argv),
        duration_ms=round(elapsed * 1000),
        stdout=stdout,
        stderr=stderr,
        completed_at=datetime.now(UTC),
    )


async def run_check_once(
    ctx: ToolExecutionContext,
    artifact: WorkProduct,
    check: CodingCheck,
    *,
    worktree: Path,
    config: CodingPackConfig,
) -> CheckOutcome:
    """Run one check, keep a successful fixer's edits, and record the result.

    A verify check that rewrites files is a configuration error: its edits are
    reverted and the error propagates.
    """

    started = time.perf_counter()
    before = await worktree_tree_sha(worktree, config=config, paths=artifact.changed_paths)
    result = await run_contained(
        ctx, check.argv, worktree=worktree, timeout_seconds=check.timeout_seconds
    )
    after = await worktree_tree_sha(worktree, config=config, paths=artifact.changed_paths)
    changes = await tree_changes(worktree, config=config, before=before, after=after)
    elapsed = time.perf_counter() - started
    succeeded = not result.timed_out and result.return_code == 0
    changed = [path for _, path in changes]
    keep = check.kind == "fix" and succeeded and _editable(worktree, changed)
    if changes and not keep:
        await restore_tree_paths(worktree, config=config, tree=before, changes=changes)
        if check.kind == "verify":
            raise CodingWorkflowError(
                f"Check '{check.id}' modified {', '.join(changed)}; the edits were "
                "reverted. A check that rewrites files must be configured with kind: fix."
            )
        if succeeded:
            raise CodingWorkflowError(
                f"Fix '{check.id}' modified excluded paths ({', '.join(changed)}); "
                "the edits were reverted."
            )
    fixed_paths = changed if keep else []
    if fixed_paths:
        artifact.revision += 1
        artifact.changed_paths = list(
            dict.fromkeys([*artifact.changed_paths, *fixed_paths])
        )
        artifact.checks = {}
        artifact.status = "patched"
    status: Literal["passed", "failed", "timed_out"] = (
        "timed_out" if result.timed_out else "passed" if succeeded else "failed"
    )
    tree = await candidate_tree_sha(
        worktree, config=config, paths=artifact.changed_paths or None
    )
    _record(
        artifact,
        check,
        status=status,
        tree=tree,
        elapsed=elapsed,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    return CheckOutcome(
        check_id=check.id,
        kind=check.kind,
        status=status,
        elapsed_seconds=elapsed,
        return_code=result.return_code,
        timed_out=result.timed_out,
        output_truncated=result.output_truncated,
        fixed_paths=fixed_paths,
        detail="" if succeeded else failure_excerpt(result.stdout, result.stderr),
    )


async def _ensure_prepared(
    ctx: ToolExecutionContext,
    artifact: WorkProduct,
    *,
    worktree: Path,
    config: CodingPackConfig,
) -> bool:
    if preparation_is_current(artifact, worktree=worktree, config=config):
        return False
    recipe_ids = [recipe.id for recipe in config.preparation_recipes]
    recorded = artifact.preparation.recipe_id if artifact.preparation else ""
    if recorded in recipe_ids:
        recipe_id = recorded
    elif len(recipe_ids) == 1:
        recipe_id = recipe_ids[0]
    else:
        raise CodingWorkflowError(
            "Several preparation recipes are configured; run "
            "coding_prepare_workspace with one of: " + ", ".join(recipe_ids) + "."
        )
    try:
        await prepare_artifact(
            ctx, artifact, recipe=config.preparation_recipe(recipe_id), config=config
        )
    finally:
        await ctx.code_change_artifact_store.save(artifact)
    return True


async def _run_or_reuse(
    ctx: ToolExecutionContext,
    artifact: WorkProduct,
    check: CodingCheck,
    *,
    cache: _ReuseCache,
    worktree: Path,
    config: CodingPackConfig,
) -> CheckOutcome:
    key = _cache_key(check, artifact)
    tree = await candidate_tree_sha(
        worktree, config=config, paths=artifact.changed_paths or None
    )
    if await _reusable(
        check, key=key, tree=tree, cache=cache, worktree=worktree, config=config
    ):
        cached_tree = cache.entries[check.id]["tree"]
        _record(
            artifact,
            check,
            status="passed",
            tree=tree,
            elapsed=0.0,
            stdout=f"Reused the passing result from tree {cached_tree}; "
            "none of the check's inputs changed since.",
            stderr="",
        )
        return CheckOutcome(check_id=check.id, kind=check.kind, status="passed", reused=True)
    try:
        outcome = await run_check_once(
            ctx, artifact, check, worktree=worktree, config=config
        )
    finally:
        await ctx.code_change_artifact_store.save(artifact)
    if outcome.status == "passed":
        cache.record(check.id, key=key, tree=artifact.checks[check.id].checked_tree_sha)
    else:
        cache.forget(check.id)
    return outcome


async def verify_artifact(
    ctx: ToolExecutionContext,
    artifact: WorkProduct,
    *,
    config: CodingPackConfig,
    check_ids: list[str] | None = None,
) -> VerifyReport:
    """Sync, prepare, fix, and verify ``artifact``; persist after every step.

    A failure after the sync raises :class:`VerificationAborted` carrying it,
    so callers can still report a rebase that already landed.
    """

    plan = plan_checks(config, artifact, list(check_ids or ()))
    worktree = artifact_worktree(artifact, config=config)
    sync = await sync_with_upstream(artifact, worktree=worktree, config=config)
    try:
        if sync.status == "synced":
            await ctx.code_change_artifact_store.save(artifact)
        return await _verify_synced(
            ctx, artifact, VerifyReport(sync=sync), plan=plan, worktree=worktree, config=config
        )
    except (CodingWorkflowError, OSError, ValueError) as exc:
        raise VerificationAborted(str(exc), sync) from exc


async def _verify_synced(
    ctx: ToolExecutionContext,
    artifact: WorkProduct,
    report: VerifyReport,
    *,
    plan: list[CodingCheck],
    worktree: Path,
    config: CodingPackConfig,
) -> VerifyReport:
    report.prepared = await _ensure_prepared(
        ctx, artifact, worktree=worktree, config=config
    )
    cache = await _ReuseCache.load(worktree, config=config)

    fixers = [check for check in plan if check.kind == "fix"]
    fix_results: dict[str, CheckOutcome] = {}
    for _ in range(_MAX_FIX_PASSES):
        edited = False
        for check in fixers:
            outcome = await _run_or_reuse(
                ctx, artifact, check, cache=cache, worktree=worktree, config=config
            )
            edited = edited or bool(outcome.fixed_paths)
            previous = fix_results.get(check.id)
            if previous is not None:
                outcome.fixed_paths = list(
                    dict.fromkeys([*previous.fixed_paths, *outcome.fixed_paths])
                )
                outcome.reused = outcome.reused and previous.reused
            fix_results[check.id] = outcome
        if not edited:
            break
    else:
        # The last pass still edited, so earlier fixers' records were cleared
        # by its revision bump; the gate will ask for another verification.
        report.fixers_converged = False

    results: dict[str, CheckOutcome] = dict(fix_results)
    for check in plan:
        if check.kind == "fix":
            continue
        unmet = [need for need in check.needs if results[need].status != "passed"]
        if unmet:
            results[check.id] = CheckOutcome(
                check_id=check.id,
                kind=check.kind,
                status="skipped",
                detail="needs " + ", ".join(unmet),
            )
            continue
        results[check.id] = await _run_or_reuse(
            ctx, artifact, check, cache=cache, worktree=worktree, config=config
        )
    report.outcomes = [results[check.id] for check in plan]
    report.ready_to_commit = artifact_has_current_required_checks(
        artifact,
        candidate_tree=await candidate_tree_sha(
            worktree, config=config, paths=artifact.changed_paths or None
        ),
    )
    await ctx.code_change_artifact_store.save(artifact)
    return report
