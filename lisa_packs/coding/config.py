"""Typed, session-owned policy for local coding tools."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from kernel.models import ToolExecutionContext
from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The one placeholder session-authored command vectors (checks and
#: preparation recipes) may use; the pack resolves it to the worktree-owned
#: interpreter. Session text never expands it, and the pack invents nothing
#: else. Single source of truth shared with the workflow helpers.
ARGV_WORKSPACE_PYTHON = "{workspace_python}"

__all__ = [
    "CodingCheck",
    "CodingPackConfig",
    "CodingPreparationRecipe",
    "get_coding_config",
]


class CodingCheck(BaseModel):
    """One finite command vector allowed to run in an owned worktree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=1800)
    #: ``verify`` must leave files untouched; ``fix`` (formatters, autofixers)
    #: may rewrite them and its edits become a new artifact revision.
    kind: Literal["verify", "fix"] = "verify"


class CodingPreparationRecipe(BaseModel):
    """One finite, session-authorized workspace preparation recipe.

    The prepare command is a session-authored argument vector, exactly like
    the pack's checks: the pack owns the mechanics (sandbox containment,
    interpreter probe, baseline evidence), the session owns the toolchain.
    ``{workspace_python}`` is the only placeholder, resolved like a check's.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    prepare_argv: tuple[str, ...] = Field(min_length=1, max_length=100)
    #: Worktree-relative dependency-manifest paths that feed staleness
    #: evidence. An empty tuple means the recipe is self-describing and
    #: staleness rides the recipe hash alone. Declared so the gate matches
    #: the recipe's toolchain: a uv recipe sees uv.lock, an npm recipe sees
    #: package-lock.json.
    manifest_paths: tuple[str, ...] = Field(default=(), max_length=8)
    #: Modules (import names) that must resolve from the worktree-owned
    #: interpreter after preparation. Empty skips the interpreter probe for
    #: recipes that do not produce one.
    project_modules: tuple[str, ...] = Field(default=(), max_length=20)
    baseline_check_ids: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=600, ge=1, le=1800)


class CodingPackConfig(BaseModel):
    """Portable policy supplied only through session pack configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_root: str = Field(min_length=1)
    excluded_parts: tuple[str, ...] = Field(
        default=(
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        )
    )
    checks: tuple[CodingCheck, ...] = ()
    required_check_ids: tuple[str, ...] = ()
    preparation_recipes: tuple[CodingPreparationRecipe, ...] = ()
    branch_prefix: str = Field(default="lisa/", min_length=1, max_length=80)
    protected_branches: tuple[str, ...] = ("main", "master")
    max_directory_entries: int = Field(default=200, ge=1, le=1000)
    max_file_bytes: int = Field(default=512_000, ge=1024, le=5_000_000)
    max_read_lines: int = Field(default=400, ge=1, le=2000)
    max_search_results: int = Field(default=50, ge=1, le=200)
    max_search_output_bytes: int = Field(default=200_000, ge=4096, le=2_000_000)
    search_timeout_seconds: int = Field(default=20, ge=1, le=120)
    max_patch_bytes: int = Field(default=200_000, ge=1024, le=2_000_000)
    max_command_output_bytes: int = Field(default=100_000, ge=4096, le=2_000_000)
    git_timeout_seconds: int = Field(default=60, ge=1, le=300)
    push_remote: str = Field(default="origin", min_length=1, max_length=100)
    #: Absolute parent for exported patch series; empty means ``<source-parent>/.lisa-patches``.
    patch_export_dir: str = ""
    #: Absolute parent under which owned worktrees are created; empty means
    #: ``<data-dir>/git-worktrees/<workspace-name>`` from the host-injected data dir.
    worktree_dir: str = ""

    @model_validator(mode="after")
    def _validate_checks(self) -> CodingPackConfig:
        check_ids = [check.id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("checks must use unique IDs")
        unknown_required = sorted(set(self.required_check_ids) - set(check_ids))
        if unknown_required:
            raise ValueError(
                "required_check_ids reference unknown checks: " + ", ".join(unknown_required)
            )
        recipe_ids = [recipe.id for recipe in self.preparation_recipes]
        if len(recipe_ids) != len(set(recipe_ids)):
            raise ValueError("preparation_recipes must use unique IDs")
        probes_interpreter = False
        for recipe in self.preparation_recipes:
            unknown_baseline = sorted(set(recipe.baseline_check_ids) - set(check_ids))
            if unknown_baseline:
                raise ValueError(
                    f"recipe '{recipe.id}' references unknown baseline checks: "
                    + ", ".join(unknown_baseline)
                )
            probes_interpreter = probes_interpreter or bool(recipe.project_modules)
            for raw in recipe.manifest_paths:
                candidate = Path(raw)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError(
                        f"recipe '{recipe.id}' manifest path must be relative "
                        f"without traversal: {raw!r}"
                    )
        if probes_interpreter:
            checks_without_owned_python = [
                check.id for check in self.checks if ARGV_WORKSPACE_PYTHON not in check.argv
            ]
            if checks_without_owned_python:
                raise ValueError(
                    "checks must include {workspace_python} when a preparation "
                    "recipe declares project_modules: "
                    + ", ".join(checks_without_owned_python)
                )
        if any(branch in self.protected_branches for branch in ("", self.branch_prefix)):
            raise ValueError("branch_prefix must not be an empty or protected branch")
        if self.worktree_dir and not Path(self.worktree_dir).is_absolute():
            raise ValueError("worktree_dir must be an absolute path")
        return self

    def resolve_worktree_parent(self, *, data_dir: str, source_root: Path) -> Path:
        """Resolve the parent directory under which owned worktrees are created.

        An explicit ``worktree_dir`` wins; otherwise the default lives inside
        the host data dir at ``git-worktrees/<workspace-name>``, named after
        the source checkout so several repositories share one data root. The
        host always injects a resolved ``data_dir``; without one (a pack-only
        context) creation fails fast rather than scattering worktrees in an
        unpredictable location.
        """

        if self.worktree_dir:
            return Path(self.worktree_dir).expanduser()
        root = (data_dir or "").strip()
        if not root:
            raise ValueError(
                "worktree_dir is not configured and the host injected no data_dir; "
                "set worktree_dir to an absolute path."
            )
        return Path(root) / "git-worktrees" / source_root.name

    def preparation_recipe(self, recipe_id: str | None) -> CodingPreparationRecipe:
        """Return an explicitly or uniquely selected finite preparation recipe."""

        if recipe_id is not None:
            recipe = next((item for item in self.preparation_recipes if item.id == recipe_id), None)
            if recipe is None:
                raise ValueError(f"Unknown configured preparation recipe '{recipe_id}'.")
            return recipe
        if len(self.preparation_recipes) != 1:
            raise ValueError("recipe_id is required when preparation is not uniquely configured.")
        return self.preparation_recipes[0]

    def resolve_workspace_root(self) -> Path:
        """Resolve the configured root and require it to be a Git worktree.

        Delegates expansion/traversal/directory checking to the shared
        ``sdk.workspace.resolve_workspace_root`` (strict mode); the strictness
        policy (Git-worktree required) is the single shared rule, not a
        coding-private one. The config object already holds the validated
        ``workspace_root`` string, which the SDK resolver reads back via a
        plain mapping-shaped shim.
        """

        from sdk.workspace import resolve_workspace_root

        class _Ctx:
            pack_config = {"workspace_root": self.workspace_root}
            data_dir = ""

        try:
            return resolve_workspace_root(_Ctx(), require_git=True)
        except Exception as exc:
            # Preserve the module's historical error type for callers.
            raise ValueError(str(exc)) from exc


def get_coding_config(ctx: ToolExecutionContext) -> CodingPackConfig:
    """Parse the current target's immutable coding configuration."""

    return CodingPackConfig.model_validate(ctx.pack_config)
