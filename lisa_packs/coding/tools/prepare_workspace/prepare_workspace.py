"""Prepare the session-configured environment inside an owned worktree."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field
from sdk.workproducts import (
    WorkProduct,
    WorkProductCheck,
    WorkProductCheckStatus,
    WorkProductPreparation,
)

from lisa_packs.coding.config import (
    CodingPackConfig,
    CodingPreparationRecipe,
    get_coding_config,
)
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    candidate_tree_sha,
    dependency_manifest_digest,
    load_owned_artifact,
    preparation_recipe_digest,
    run_contained,
    run_git,
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

#: Node analogue of ``_PROBE``: certify that every declared package resolves
#: into the worktree's own ``node_modules``. ``createRequire`` is anchored at
#: the worktree's ``package.json``, so it walks exactly the resolution the
#: project's own imports use — including pnpm's symlinked virtual store, whose
#: real path stays inside ``<worktree>/node_modules/.pnpm``. Resolution that
#: escapes (a hoisted host install, a global package) fails loudly instead of
#: letting checks silently exercise the wrong tree.
_NODE_PROBE = """const { createRequire } = require('node:module');
const fs = require('node:fs');
const path = require('node:path');

const root = fs.realpathSync(path.resolve(process.argv[1]));
const requireFromWorktree = createRequire(path.join(root, 'package.json'));
const modulesRoot = path.join(root, 'node_modules');
const origins = {};
for (const name of process.argv.slice(2)) {
    let resolved;
    try {
        resolved = requireFromWorktree.resolve(name);
    } catch (error) {
        throw new Error(`package does not resolve from the worktree: ${name}`);
    }
    const real = fs.realpathSync(resolved);
    const relative = path.relative(modulesRoot, real);
    if (relative.startsWith('..') || path.isAbsolute(relative)) {
        throw new Error(`package resolves outside worktree node_modules: ${name} -> ${real}`);
    }
    origins[name] = real;
}
process.stdout.write(JSON.stringify({ node_modules: modulesRoot, origins }, null, 0));
"""


class Parameters(BaseModel):
    """Request a finite session-configured workspace preparation recipe."""

    artifact_id: str = Field(min_length=1, max_length=100)
    recipe_id: str | None = Field(default=None, min_length=1, max_length=80)


async def _require_ignored_paths(worktree: Path, *, recipe, config) -> None:
    """Require every runtime directory preparation creates to be Git-ignored.

    The directories are the recipe's own toolchain-derived set (see
    :meth:`CodingPreparationRecipe.required_ignored_paths`), so a pnpm
    project is never asked to ignore a ``.venv`` it does not create. The
    check runs *before* the prepare command, so an unignored directory can
    never pollute the worktree or the baseline tree hash.
    """

    for raw in recipe.required_ignored_paths():
        # ``git check-ignore`` matches a directory rule only against a
        # trailing-slash pathspec; the derivation already normalizes to one,
        # but declaring ``/node_modules`` without the slash also ignores the
        # tree, so the probe stays tolerant of the rule's spelling.
        candidate = raw if raw.endswith("/") else f"{raw}/"
        result = await run_git(
            worktree, ("check-ignore", "-q", "--", candidate), config=config
        )
        if result.return_code != 0:
            stripped = raw.rstrip("/")
            raise CodingWorkflowError(
                f"{stripped} must be ignored before preparing a workspace."
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


async def prepare_artifact(
    ctx: ToolExecutionContext,
    artifact: WorkProduct,
    *,
    recipe: CodingPreparationRecipe,
    config: CodingPackConfig,
) -> Path:
    """Run, probe, and record one preparation; return the worktree node_modules.

    Mutates ``artifact`` (preparation evidence, cleared checks) without saving
    it; a failure records a failed preparation before raising. Shared by this
    tool and by verification, which prepares on demand.
    """

    try:
        worktree = artifact_worktree(artifact, config=config)
        await _require_ignored_paths(worktree, recipe=recipe, config=config)
        manifest_digest = dependency_manifest_digest(worktree, recipe=recipe)
        recipe_digest = preparation_recipe_digest(recipe)
        prepared = await run_contained(
            ctx, recipe.prepare_argv, worktree=worktree,
            timeout_seconds=recipe.timeout_seconds,
        )
        if prepared.timed_out or prepared.return_code != 0:
            detail = prepared.stderr.strip() or prepared.stdout.strip()
            raise CodingWorkflowError(detail or "Preparation command failed")
        python = worktree / ".venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        venv_exists = python.is_file()
        probes_interpreter = bool(recipe.project_modules)
        probe_stdout = ""
        probe_stderr = ""
        if probes_interpreter and venv_exists:
            # Run the probe exactly the way checks do: through the session's
            # uv-mediated interpreter address, so its own sys.executable
            # assertion certifies that uv run resolves to the owned venv
            # rather than a PATH or host interpreter. ``--no-cache`` keeps
            # the probe independent of a writable uv cache — existence and
            # writability both: the pack's floor contribution
            # (``lisa_packs.coding.sandbox``) grants ``~/.cache/uv`` when
            # the configured toolchain names uv and the root exists, but
            # the probe must also survive floors without that grant (hand,
            # harness-authored, or a cold machine). The address itself is
            # pack-owned mechanics, like the probe script.
            probed = await run_contained(
                ctx,
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "--no-cache",
                    "python",
                    "-c",
                    _PROBE,
                    str(worktree),
                    *recipe.project_modules,
                ),
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
        node_modules = worktree / "node_modules"
        node_evidence: dict[str, object] = {}
        if recipe.node_packages and not node_modules.is_dir():
            raise CodingWorkflowError(
                "Preparation did not create the worktree-owned node_modules."
            )
        if recipe.node_packages:
            # Node runs the probe from the worktree's own module graph via
            # ``createRequire(package.json)``, so the resolution under test is
            # the one the project's imports use — pnpm's symlinked virtual
            # store certified through its real path inside ``node_modules``.
            # ``node`` needs no cache grant, so unlike the uv probe there is
            # no floor interaction to absorb.
            node_probed = await run_contained(
                ctx,
                ("node", "-e", _NODE_PROBE, "--", str(worktree), *recipe.node_packages),
                worktree=worktree,
                timeout_seconds=recipe.timeout_seconds,
            )
            if node_probed.timed_out or node_probed.return_code != 0:
                raise CodingWorkflowError(
                    node_probed.stderr.strip()
                    or "worktree node package provenance probe failed"
                )
            node_evidence = json.loads(node_probed.stdout)
            # Merge under namespaced keys: a Python-only recipe's evidence
            # keeps its exact historical shape (``python`` plus ``origins``),
            # which recorded artifacts and their assertions read.
            merged = json.loads(probe_stdout) if probe_stdout else {}
            merged["node_modules"] = node_evidence.get("node_modules", "")
            merged["node_origins"] = node_evidence.get("origins", {})
            probe_stdout = json.dumps(merged, sort_keys=True)
            probe_stderr = "\n".join(
                part for part in (probe_stderr, node_probed.stderr) if part.strip()
            )
        baseline_tree = await candidate_tree_sha(worktree, config=config)
        baseline_checks: dict[str, WorkProductCheck] = {}
        for check_id in recipe.baseline_check_ids:
            check = next(item for item in config.checks if item.id == check_id)
            argv = check.argv
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
            environment_path=str(python.parent.parent) if venv_exists else "",
            python_path=str(python) if venv_exists else "",
            baseline_tree_sha=baseline_tree,
            baseline_checks=baseline_checks,
            probe_stdout=probe_stdout,
            probe_stderr=probe_stderr,
            completed_at=datetime.now(UTC),
        )
        artifact.checks = {}
    except (CodingWorkflowError, OSError, ValueError, json.JSONDecodeError) as exc:
        artifact.preparation = WorkProductPreparation(
            status="failed",
            recipe_id=recipe.id,
            recipe_digest=preparation_recipe_digest(recipe),
            probe_stderr=str(exc),
            completed_at=datetime.now(UTC),
        )
        raise CodingWorkflowError(str(exc)) from exc
    return node_modules


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Run and verify one finite session-configured preparation recipe."""

    params = Parameters.model_validate(ctx.parameters)
    artifact = None
    try:
        config = get_coding_config(ctx)
        recipe = config.preparation_recipe(params.recipe_id)
        artifact = await load_owned_artifact(
            ctx, artifact_id=params.artifact_id, config=config, claim=True
        )
        node_modules = await prepare_artifact(
            ctx, artifact, recipe=recipe, config=config
        )
        await ctx.code_change_artifact_store.save(artifact)
    except (CodingWorkflowError, OSError, ValueError) as exc:
        if artifact is not None and artifact.preparation is not None and (
            artifact.preparation.status == "failed"
        ):
            await ctx.code_change_artifact_store.save(artifact)
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    preparation = artifact.preparation
    assert preparation is not None
    return ToolResult(
        tool_id=TOOL_ID,
        output_text=f"Prepared '{recipe.id}' in the owned worktree.",
        output_json={
            "artifact_id": artifact.artifact_id,
            "recipe_id": recipe.id,
            "python_path": preparation.python_path,
            "node_modules": (
                str(node_modules)
                if recipe.node_packages and node_modules.is_dir()
                else ""
            ),
            "baseline_checks": sorted(preparation.baseline_checks),
        },
    )
