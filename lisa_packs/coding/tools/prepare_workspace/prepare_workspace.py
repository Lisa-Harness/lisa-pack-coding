"""Prepare the session-configured environment inside an owned worktree."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field
from sdk.workproducts import (
    WorkProductCheck,
    WorkProductCheckStatus,
    WorkProductPreparation,
)

from lisa_packs.coding.config import CodingPreparationRecipe, get_coding_config
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    candidate_tree_sha,
    dependency_manifest_digest,
    expand_argv_placeholders,
    expand_check_argv,
    load_owned_artifact,
    preparation_recipe_digest,
    run_contained,
    run_git,
    workspace_python,
)

TOOL_ID = "coding_prepare_workspace"

_PROBE = """import importlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
python = root / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
if pathlib.Path(sys.executable).absolute() != python.absolute():
    raise RuntimeError('interpreter is not the worktree-owned virtual environment')
origins = {}
for name in sys.argv[2:]:
    module = importlib.import_module(name)
    origin = getattr(module, '__file__', None)
    if origin:
        resolved = pathlib.Path(origin).resolve()
        if not resolved.is_relative_to(root):
            raise RuntimeError(f'module resolves outside worktree: {name} -> {resolved}')
        origins[name] = str(resolved)
    else:
        # A PEP 420 namespace package has no __init__.py, so __file__ stays
        # unset and __path__ is the merged list of every sys.path entry the
        # finder resolved the name into. (Real case: the Lisa workspace's
        # 'sdk' spans one editable install per distribution; with no
        # __file__ the honest provenance is the directory list.) Certify
        # every entry — a single outside entry means part of the namespace
        # resolves somewhere the worktree does not own.
        paths = [pathlib.Path(p).resolve() for p in getattr(module, '__path__', ())]
        if not paths:
            raise RuntimeError(f'module has no import origin: {name}')
        for resolved in paths:
            if not resolved.is_relative_to(root):
                raise RuntimeError(f'module resolves outside worktree: {name} -> {resolved}')
        origins[name] = [str(resolved) for resolved in paths]
print(json.dumps({'python': str(python), 'origins': origins}, sort_keys=True))
"""


class Parameters(BaseModel):
    """Request a finite session-configured workspace preparation recipe."""

    artifact_id: str = Field(min_length=1, max_length=100)
    recipe_id: str | None = Field(default=None, min_length=1, max_length=80)


async def _require_venv_ignored(worktree: Path, *, config) -> None:
    result = await run_git(
        worktree, ("check-ignore", "-q", "--", ".venv/"), config=config
    )
    if result.return_code != 0:
        raise CodingWorkflowError(
            ".venv must be ignored before preparing a workspace."
        )


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
        "timed_out"
        if result.timed_out
        else "passed"
        if result.return_code == 0
        else "failed"
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
    """Run and verify one finite session-configured preparation recipe."""

    params = Parameters.model_validate(ctx.parameters)
    artifact = None
    recipe: CodingPreparationRecipe | None = None
    try:
        config = get_coding_config(ctx)
        recipe = config.preparation_recipe(params.recipe_id)
        artifact = await load_owned_artifact(
            ctx, artifact_id=params.artifact_id, config=config, claim=True
        )
        worktree = artifact_worktree(artifact, config=config)
        await _require_venv_ignored(worktree, config=config)
        manifest_digest = dependency_manifest_digest(worktree, recipe=recipe)
        recipe_digest = preparation_recipe_digest(recipe)
        prepared = await run_contained(
            ctx,
            expand_argv_placeholders(recipe.prepare_argv, worktree=worktree),
            worktree=worktree,
            timeout_seconds=recipe.timeout_seconds,
        )
        if prepared.timed_out or prepared.return_code != 0:
            detail = prepared.stderr.strip() or prepared.stdout.strip()
            raise CodingWorkflowError(detail or "Preparation command failed")
        python = workspace_python(worktree)
        probes_interpreter = bool(recipe.project_modules)
        probe_stdout = ""
        probe_stderr = ""
        if python.is_file() and probes_interpreter:
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
            probe_stdout = probed.stdout
            probe_stderr = probed.stderr
        elif probes_interpreter:
            raise CodingWorkflowError(
                "Preparation did not create the worktree-owned Python interpreter."
            )
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
            environment_path=str(python.parent.parent) if python.is_file() else "",
            python_path=str(python) if python.is_file() else "",
            baseline_tree_sha=baseline_tree,
            baseline_checks=baseline_checks,
            probe_stdout=probe_stdout,
            probe_stderr=probe_stderr,
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
