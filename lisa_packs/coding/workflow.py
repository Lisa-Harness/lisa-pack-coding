"""Bounded local Git workflow helpers for the coding pack."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from kernel.models import ToolExecutionContext
from sdk.workproducts import WorkProduct, WorkProductStore

from lisa_packs.coding.config import CodingCheck, CodingPackConfig, CodingPreparationRecipe
from lisa_packs.coding.local_files import CodingPathError, resolve_relative_path

__all__ = [
    "CodingWorkflowError",
    "ProcessResult",
    "ReadableRoot",
    "artifact_branch_is_allowed",
    "artifact_has_current_required_checks",
    "artifact_owner_matches",
    "artifact_worktree",
    "candidate_tree_sha",
    "changed_paths",
    "load_owned_artifact",
    "patch_paths",
    "preparation_is_current",
    "preparation_recipe_digest",
    "readable_root",
    "restore_tree_paths",
    "run_contained",
    "run_git",
    "run_process",
    "dependency_manifest_digest",
    "expand_check_argv",
    "tree_changes",
    "workspace_python",
    "worktree_tree_sha",
]

_GIT_STATUS_PATH_OFFSET = 3


class CodingWorkflowError(RuntimeError):
    """Raised for a failed coding workflow precondition or bounded command."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded process result; output is never unbounded host memory."""

    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ReadableRoot:
    """The resolved workspace root and the scope that selected it."""

    path: Path
    scope: Literal["source", "worktree"]
    artifact_id: str | None = None


async def _drain_limited(
    stream: asyncio.StreamReader,
    *,
    limit: int,
) -> tuple[bytes, bool]:
    """Drain a subprocess stream while retaining no more than ``limit`` bytes."""

    retained = bytearray()
    overflowed = False
    while chunk := await stream.read(8192):
        remaining = limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            overflowed = True
    return bytes(retained), overflowed


async def _feed_and_wait(
    process: asyncio.subprocess.Process, stdin_bytes: bytes | None
) -> int:
    if stdin_bytes is not None:
        assert process.stdin is not None
        try:
            process.stdin.write(stdin_bytes)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # The child exited without reading; its exit status reports why.
        process.stdin.close()
    return await process.wait()


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Execute an explicit argument vector without a shell or unbounded output."""

    if not argv or not argv[0]:
        raise CodingWorkflowError("A non-empty executable argument vector is required.")
    # Never inherit the host's stdin: an interactive host's TTY must not be
    # shared with children.
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_drain_limited(process.stdout, limit=max_output_bytes))
    stderr_task = asyncio.create_task(_drain_limited(process.stderr, limit=max_output_bytes))
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
    timed_out = False
    try:
        await asyncio.wait_for(_feed_and_wait(process, stdin_bytes), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        process.kill()
        await process.wait()
    except BaseException:
        # Cancelled (e.g. the caller's tool deadline): never orphan the child.
        if process.returncode is None:
            process.kill()
        stdout_task.cancel()
        stderr_task.cancel()
        raise
    stdout, stdout_overflowed = await stdout_task
    stderr, stderr_overflowed = await stderr_task
    return ProcessResult(
        return_code=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        output_truncated=stdout_overflowed or stderr_overflowed,
    )


async def run_contained(
    ctx: ToolExecutionContext,
    argv: Sequence[str],
    *,
    worktree: Path,
    timeout_seconds: int,
) -> ProcessResult:
    """Run under the bwrap owner's verified direct-write worktree floor."""

    dispatch = ctx.delegated_tool_dispatch
    floor_for = ctx.execution_floor_for
    floor = floor_for("bwrap_run") if floor_for is not None else None
    if (
        dispatch is None
        or floor is None
        or floor.execution != "sandbox"
        or floor.persistence != "direct-write"
    ):
        raise CodingWorkflowError(
            "bwrap sandbox has no direct-write execution floor (found "
            f"execution={getattr(floor, 'execution', None)!r}, "
            f"persistence={getattr(floor, 'persistence', None)!r}). Set "
            "lisa-pack-bwrap sandbox.floor to execution: sandbox, persistence: direct-write."
        )
    matching_paths = (
        (root, access)
        for path, access in floor.paths.items()
        if (root := Path(path).expanduser().resolve()) == worktree
        or worktree.is_relative_to(root)
    )
    nearest = max(matching_paths, key=lambda entry: len(entry[0].parts), default=None)
    if nearest is None or nearest[1] != "rw":
        raise CodingWorkflowError("bwrap sandbox floor does not permit worktree writes.")
    tool_result = await dispatch(
        "bwrap_run",
        {
            "argv": list(argv),
            "cwd": str(worktree),
            "overrides": {
                "persistence": "direct-write",
                "paths": {str(worktree): "rw"},
            },
            "timeout_seconds": min(timeout_seconds, 600),
        },
    )
    payload = dict(tool_result.output_json or {})
    if tool_result.error:
        raise CodingWorkflowError(f"bwrap sandbox failed: {tool_result.error}")
    if (
        payload.get("sandboxed") is not True
        or payload.get("degraded")
        or payload.get("persistence") != "direct-write"
        or payload.get("cwd") != str(worktree)
        or not isinstance(payload.get("writable_paths"), list)
        or str(worktree) not in payload["writable_paths"]
        or type(payload.get("return_code")) is not int
    ):
        raise CodingWorkflowError("bwrap sandbox did not verify the writable worktree check.")
    return ProcessResult(
        return_code=int(payload["return_code"]),
        stdout=tool_result.output_text or "",
        stderr=str(payload.get("stderr", "")),
        timed_out=bool(payload.get("timed_out", False)),
        output_truncated=bool(payload.get("truncated", False)),
    )


def workspace_python(worktree: Path) -> Path:
    """Return the platform-specific interpreter owned by the worktree venv."""

    return worktree / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def expand_check_argv(check: CodingCheck, *, worktree: Path) -> tuple[str, ...]:
    """Resolve the sole preparation-aware command placeholder."""

    return tuple(
        str(workspace_python(worktree)) if part == "{workspace_python}" else part
        for part in check.argv
    )


def preparation_recipe_digest(recipe: CodingPreparationRecipe) -> str:
    """Hash the exact finite recipe selected by session policy."""

    payload = json.dumps(recipe.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dependency_manifest_digest(worktree: Path, *, config: CodingPackConfig) -> str:
    """Hash the bounded uv manifests that determine the local environment."""

    digest = hashlib.sha256()
    found = False
    for name in ("pyproject.toml", "uv.lock"):
        path = worktree / name
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if len(payload) > config.max_file_bytes:
            raise CodingWorkflowError(f"Dependency manifest exceeds byte limit: {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        found = True
    if not found:
        raise CodingWorkflowError("No dependency manifest exists in the worktree.")
    return digest.hexdigest()


def preparation_is_current(
    artifact: WorkProduct, *, worktree: Path, config: CodingPackConfig
) -> bool:
    """Require current configuration, manifests, and owned interpreter evidence."""

    if not config.preparation_recipes:
        return True
    preparation = artifact.preparation
    if preparation is None or preparation.status != "ready":
        return False
    try:
        recipe = config.preparation_recipe(preparation.recipe_id)
        return (
            preparation.recipe_digest == preparation_recipe_digest(recipe)
            and preparation.dependency_manifest_digest
            == dependency_manifest_digest(worktree, config=config)
            and Path(preparation.python_path).absolute() == workspace_python(worktree).absolute()
            and workspace_python(worktree).is_file()
        )
    except (OSError, ValueError, CodingWorkflowError):
        return False


async def run_git(
    root: Path,
    args: Sequence[str],
    *,
    config: CodingPackConfig,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run Git with a fixed executable and bounded output."""

    return await run_process(
        ("git", *args),
        cwd=root,
        timeout_seconds=config.git_timeout_seconds,
        max_output_bytes=config.max_command_output_bytes,
        stdin_text=stdin_text,
        env=env,
    )


async def candidate_tree_sha(
    worktree: Path,
    *,
    config: CodingPackConfig,
    paths: Sequence[str] | None = None,
) -> str:
    """Hash commit candidates without changing the live index.

    When ``paths`` is provided, only those task-owned paths enter the tree;
    check byproducts and unrelated worktree edits cannot certify or alter a
    coding artifact's commit.
    """
    add_paths = list(paths) if paths else ["."]
    return await _hash_tree(
        worktree, config=config, adds=(("--literal-pathspecs", "add", "-A", "--", *add_paths),)
    )


async def worktree_tree_sha(
    worktree: Path, *, config: CodingPackConfig, paths: Sequence[str]
) -> str:
    """Hash every tracked file plus ``paths``, ignoring untracked byproducts.

    Comparing this before and after a check reveals which real files the check
    rewrote, while caches and build output it leaves behind stay invisible.
    """
    adds: list[tuple[str, ...]] = [("add", "-u")]
    if paths:
        adds.append(("--literal-pathspecs", "add", "-A", "--", *paths))
    return await _hash_tree(worktree, config=config, adds=adds)


async def _hash_tree(
    worktree: Path, *, config: CodingPackConfig, adds: Sequence[tuple[str, ...]]
) -> str:
    with tempfile.TemporaryDirectory(prefix="lisa-candidate-index-") as temp_dir:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp_dir) / "index")}
        for args in (("read-tree", "HEAD"), *adds, ("write-tree",)):
            result = await run_git(worktree, args, config=config, env=env)
            if result.return_code != 0:
                raise CodingWorkflowError(
                    result.stderr.strip() or "Cannot hash candidate Git tree."
                )
        return result.stdout.strip()


async def tree_changes(
    worktree: Path, *, config: CodingPackConfig, before: str, after: str
) -> list[tuple[str, str]]:
    """Return ``(status, path)`` for each file that differs between two trees."""

    if before == after:
        return []
    result = await run_git(
        worktree,
        ("diff-tree", "-r", "--no-renames", "--name-status", "-z", before, after),
        config=config,
    )
    if result.return_code != 0:
        raise CodingWorkflowError(result.stderr.strip() or "Cannot diff candidate Git trees.")
    fields = result.stdout.split("\0")
    return list(zip(fields[0::2], fields[1::2], strict=False))


async def restore_tree_paths(
    worktree: Path, *, config: CodingPackConfig, tree: str, changes: Sequence[tuple[str, str]]
) -> None:
    """Put each changed path back to its content in ``tree``."""

    existing = [path for status, path in changes if status != "A"]
    if existing:
        restored = await run_git(
            worktree,
            ("--literal-pathspecs", "restore", f"--source={tree}", "--worktree", "--", *existing),
            config=config,
        )
        if restored.return_code != 0:
            raise CodingWorkflowError(restored.stderr.strip() or "Cannot restore changed files.")
    for status, path in changes:
        if status == "A":
            (worktree / path).unlink(missing_ok=True)


def artifact_owner_matches(artifact: WorkProduct, ctx: ToolExecutionContext) -> bool:
    """Return whether an artifact belongs to the current exact host identity.

    Delegates to :meth:`WorkProduct.belongs_to`, the canonical ownership rule
    shared with the publishing pack, so the identity match cannot drift.
    """

    return artifact.belongs_to(ctx)


def artifact_worktree(artifact: WorkProduct, *, config: CodingPackConfig) -> Path:
    """Return a validated owned worktree that remains outside the source root."""

    source_root = config.resolve_workspace_root()
    if Path(artifact.source_root).resolve() != source_root:
        raise CodingWorkflowError("Artifact source root does not match the configured workspace.")
    if not artifact.worktree_path:
        raise CodingWorkflowError("Artifact has no owned worktree.")
    worktree = Path(artifact.worktree_path).resolve()
    try:
        worktree.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise CodingWorkflowError("Artifact worktree must live outside the source workspace.")
    if not worktree.is_dir() or not (worktree / ".git").exists():
        raise CodingWorkflowError("Artifact worktree is not available as a Git worktree.")
    return worktree


async def load_owned_artifact(
    ctx: ToolExecutionContext,
    *,
    artifact_id: str,
    config: CodingPackConfig,
    require_worktree: bool = True,
    claim: bool = False,
) -> WorkProduct:
    """Load an artifact only when the host and workspace ownership match.

    ``claim`` hands the artifact to the calling task (persisted by the caller's
    save); tools that advance the artifact claim it, reads never do.
    """

    store = ctx.code_change_artifact_store
    if store is None:
        raise CodingWorkflowError("No code change artifact store is available.")
    artifact_store = cast(WorkProductStore, store)
    artifact = await artifact_store.get(artifact_id)
    if artifact is None:
        raise CodingWorkflowError(f"Unknown code change artifact '{artifact_id}'.")
    if not artifact_owner_matches(artifact, ctx):
        raise CodingWorkflowError("Artifact does not belong to this workspace, user, or session.")
    if require_worktree:
        artifact_worktree(artifact, config=config)
    if claim:
        artifact.claim_for_task(ctx)
    return artifact


async def readable_root(
    ctx: ToolExecutionContext,
    *,
    artifact_id: str | None,
    scope: Literal["auto", "source", "worktree"] = "auto",
    config: CodingPackConfig,
) -> ReadableRoot:
    """Resolve a safe source/worktree read target without stale fallback."""

    source_root = config.resolve_workspace_root()
    if artifact_id is not None:
        if scope == "source":
            raise CodingWorkflowError("artifact_id cannot be combined with source scope.")
        artifact = await load_owned_artifact(ctx, artifact_id=artifact_id, config=config)
        return ReadableRoot(
            path=artifact_worktree(artifact, config=config),
            scope="worktree",
            artifact_id=artifact.artifact_id,
        )
    if scope == "source":
        return ReadableRoot(path=source_root, scope="source")

    store = ctx.code_change_artifact_store
    if store is None:
        if scope == "worktree":
            raise CodingWorkflowError("No code change artifact store is available.")
        return ReadableRoot(path=source_root, scope="source")
    artifacts = [
        artifact
        for artifact in await cast(WorkProductStore, store).list_owned(ctx)
        if Path(artifact.source_root).resolve() == source_root
    ]
    task_id = getattr(ctx, "task_id", "")
    if not task_id:
        if artifacts:
            raise CodingWorkflowError(
                "Cannot select a worktree without task_id; use source scope or artifact_id."
            )
        if scope == "worktree":
            raise CodingWorkflowError("worktree scope requires artifact_id or task_id.")
        return ReadableRoot(path=source_root, scope="source")
    task_artifacts = [artifact for artifact in artifacts if artifact.task_id == task_id]
    if not task_artifacts:
        if scope == "worktree":
            raise CodingWorkflowError("No owned worktree exists for this task.")
        return ReadableRoot(path=source_root, scope="source")
    if len(task_artifacts) != 1:
        raise CodingWorkflowError(
            "Multiple owned worktrees exist for this task; provide artifact_id explicitly."
        )
    artifact = task_artifacts[0]
    return ReadableRoot(
        path=artifact_worktree(artifact, config=config),
        scope="worktree",
        artifact_id=artifact.artifact_id,
    )


def artifact_branch_is_allowed(artifact: WorkProduct, config: CodingPackConfig) -> bool:
    """Return whether the artifact branch is owned and outside protection policy."""

    return (
        bool(artifact.branch)
        and artifact.branch.startswith(config.branch_prefix)
        and artifact.branch not in config.protected_branches
    )


def artifact_has_current_required_checks(
    artifact: WorkProduct, *, candidate_tree: str
) -> bool:
    """Require every configured mandatory check to pass at the current revision."""

    return all(
        (record := artifact.checks.get(check_id)) is not None
        and record.status == "passed"
        and record.revision == artifact.revision
        and record.checked_tree_sha == candidate_tree
        for check_id in artifact.required_check_ids
    )


def patch_paths(patch: str, *, config: CodingPackConfig) -> list[str]:
    """Extract and validate every non-dev/null path mentioned by a unified diff."""

    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw = line[4:].split("\t", maxsplit=1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        if not raw:
            raise CodingWorkflowError("Patch contains an empty file path.")
        try:
            resolve_relative_path(config, raw)
        except CodingPathError as exc:
            raise CodingWorkflowError(f"Patch path is not allowed: {raw}") from exc
        if raw not in paths:
            paths.append(raw)
    if not paths:
        raise CodingWorkflowError("Patch must contain at least one unified diff file path.")
    return paths


async def changed_paths(worktree: Path, *, config: CodingPackConfig) -> list[str]:
    """Return bounded changed paths from Git's machine-readable status output."""

    result = await run_git(worktree, ("status", "--porcelain", "-z"), config=config)
    if result.return_code != 0:
        raise CodingWorkflowError(result.stderr.strip() or "git status failed")
    paths: list[str] = []
    for record in result.stdout.split("\0"):
        if len(record) <= _GIT_STATUS_PATH_OFFSET:
            continue
        path = record[_GIT_STATUS_PATH_OFFSET:]
        if path and path not in paths:
            paths.append(path)
    return paths
