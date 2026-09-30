"""Prepare a configured Python/uv environment inside an owned worktree."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import CodingPreparationRecipe, get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    candidate_tree_sha,
    dependency_manifest_digest,
    expand_check_argv,
    load_owned_artifact,
    preparation_recipe_digest,
    run_contained,
    run_git,
    workspace_python,
)
from pydantic import BaseModel, Field
from sdk.workproducts import WorkProductCheck, WorkProductPreparation, WorkProductCheckStatus

TOOL_ID = "coding_prepare_workspace"

_PROBE = """import importlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
python = root / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
if pathlib.Path(sys.executable).absolute() != python.absolute():
    raise RuntimeError('interpreter is not the worktree-owned virtual environment')
origins = {}
for name in sys.argv[2:]:
    origin = getattr(importlib.import_module(name), '__file__', None)
    if not origin:
        raise RuntimeError(f'module has no import origin: {name}')
    resolved = pathlib.Path(origin).resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError(f'module resolves outside worktree: {name} -> {resolved}')
    origins[name] = str(resolved)
print(json.dumps({'python': str(python), 'origins': origins}, sort_keys=True))
"""


class Parameters(BaseModel):
    """Request a finite session-configured workspace preparation recipe."""

    artifact_id: str = Field(min_length=1, max_length=100)
    recipe_id: str | None = Field(default=None, min_length=1, max_length=80)


def _is_uv_project(worktree: Path) -> bool:
    """Recognize only projects that explicitly declare uv metadata or a lockfile."""

    lock = worktree / "uv.lock"
    project = worktree / "pyproject.toml"
    if lock.is_file():
        return True
    return project.is_file() and "[tool.uv]" in project.read_text(encoding="utf-8")


async def _require_ignored_runtime_paths(worktree: Path, *, config) -> None:
    for path in (".venv", ".lisa-tmp"):
        result = await run_git(worktree, ("check-ignore", "-q", "--", f"{path}/"), config=config)
        if result.return_code != 0:
            raise CodingWorkflowError(f"{path} must be ignored before preparing a workspace.")


def _record_baseline_check(
    *,
    check_id: str,
    argv: tuple[str, ...],
    revision: int,
    tree_sha: str,
    result,
    duration_ms: int,
) -> WorkProductCheck:
    status: WorkProductCheckStatus = (
        "timed_out" if result.timed_out else "passed" if result.return_code == 0 else "failed"
    )
    return WorkProductCheck(
        check_id=check_id,
        revision=revision,
        checked_tree_sha=tree_sha,
        status=status,
        argv=list(argv),
        duration_ms=duration_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        completed_at=datetime.now(UTC),
    )


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Sync and verify one finite Python/uv workspace preparation recipe."""

    params = Parameters.model_validate(ctx.parameters)
    artifact = None
    recipe: CodingPreparationRecipe | None = None
    try:
        config = get_coding_config(ctx)
        recipe = config.preparation_recipe(params.recipe_id)
        artifact = await load_owned_artifact(ctx, artifact_id=params.artifact_id, config=config)
        worktree = artifact_worktree(artifact, config=config)
        if not _is_uv_project(worktree):
            raise CodingWorkflowError(
                "Preparation supports only uv projects with uv.lock or [tool.uv]."
            )
        if not (worktree / "uv.lock").is_file():
            raise CodingWorkflowError(
                "uv preparation requires a committed uv.lock for --locked sync."
            )
        await _require_ignored_runtime_paths(worktree, config=config)
        manifest_digest = dependency_manifest_digest(worktree, config=config)
        recipe_digest = preparation_recipe_digest(recipe)
        temporary_directory = await run_contained(
            ctx,
            ("mkdir", "-p", ".lisa-tmp"),
            worktree=worktree,
            timeout_seconds=recipe.timeout_seconds,
        )
        if temporary_directory.timed_out or temporary_directory.return_code != 0:
            raise CodingWorkflowError(
                temporary_directory.stderr.strip()
                or "Could not create workspace temporary directory."
            )
        synced = await run_contained(
            ctx,
            ("env", "TMPDIR=.lisa-tmp", "uv", "sync", "--locked", "--no-cache"),
            worktree=worktree,
            timeout_seconds=recipe.timeout_seconds,
        )
        if synced.timed_out or synced.return_code != 0:
            raise CodingWorkflowError(synced.stderr.strip() or "uv sync failed")
        python = workspace_python(worktree)
        if not python.is_file():
            raise CodingWorkflowError(
                "uv sync did not create the worktree-owned Python interpreter."
            )
        probed = await run_contained(
            ctx,
            (str(python), "-c", _PROBE, str(worktree), *recipe.project_modules),
            worktree=worktree,
            timeout_seconds=recipe.timeout_seconds,
        )
        if probed.timed_out or probed.return_code != 0:
            raise CodingWorkflowError(
                probed.stderr.strip() or "worktree import provenance probe failed"
            )
        json.loads(probed.stdout)
        baseline_tree = await candidate_tree_sha(worktree, config=config)
        baseline_checks: dict[str, WorkProductCheck] = {}
        for check_id in recipe.baseline_check_ids:
            check = next(item for item in config.checks if item.id == check_id)
            argv = expand_check_argv(check, worktree=worktree)
            started = time.perf_counter()
            result = await run_contained(
                ctx, argv, worktree=worktree, timeout_seconds=check.timeout_seconds
            )
            baseline_checks[check.id] = _record_baseline_check(
                check_id=check.id,
                argv=argv,
                revision=artifact.revision,
                tree_sha=baseline_tree,
                result=result,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        if await candidate_tree_sha(worktree, config=config) != baseline_tree:
            raise CodingWorkflowError("Preparation changed the candidate Git tree.")
        artifact.preparation = WorkProductPreparation(
            status="ready",
            recipe_id=recipe.id,
            recipe_digest=recipe_digest,
            dependency_manifest_digest=manifest_digest,
            environment_path=str(python.parent.parent),
            python_path=str(python),
            baseline_tree_sha=baseline_tree,
            baseline_checks=baseline_checks,
            probe_stdout=probed.stdout,
            probe_stderr=probed.stderr,
            completed_at=datetime.now(UTC),
        )
        artifact.checks = {}
        await ctx.code_change_artifact_store.save(artifact)
    except (CodingWorkflowError, OSError, ValueError, json.JSONDecodeError) as exc:
        if artifact is not None and recipe is not None:
            artifact.preparation = WorkProductPreparation(
                status="failed",
                recipe_id=recipe.id,
                recipe_digest=preparation_recipe_digest(recipe),
                probe_stderr=str(exc),
                completed_at=datetime.now(UTC),
            )
            await ctx.code_change_artifact_store.save(artifact)
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Prepared '{recipe.id}' in the owned worktree.",
        output_json={
            "artifact_id": artifact.artifact_id,
            "recipe_id": recipe.id,
            "python_path": artifact.preparation.python_path,
            "baseline_checks": sorted(artifact.preparation.baseline_checks),
        },
    )
