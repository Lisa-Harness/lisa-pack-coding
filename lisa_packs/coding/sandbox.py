"""The coding pack's sandbox-floor contribution.

The pack's tools run contained (checks, preparation, commit verification)
inside owned worktrees under ``resolve_worktree_parent`` — a *runtime-derived*
path the harness cannot know when authoring the session floor. This module
declares those needs through the ``lisa.extensions.sandbox_floors`` entry
point: the runtime merges the contribution exclusively onto the
harness-authored floor at session assembly (see
``kernel.sandbox_floor``), so the floor and the actual worktree location can
never drift apart.

The same holds for the language-platform caches the configured toolchain
resolves into (uv's ``~/.cache/uv`` for a Python session; the Go-era floor
carried ``~/.cache/go-build`` + ``~/go/pkg/mod`` instead). The platform —
not the harness — sets the location, so the contribution derives cache
roots from the *configured* checks and preparation recipes: whatever
toolchain the session declares, its cache paths land in the floor
automatically.

The contribution is data derived from this pack's own validated config —
the same ``CodingPackConfig`` and the same ``resolve_worktree_parent`` the
worktree tools use — so a change in resolution automatically flows into
the floor. No side effects beyond existence checks on the cache roots
(floor ``rw`` binds have no existence filter; a missing source directory
would abort every sandboxed run).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lisa_packs.coding.config import CodingPackConfig


def _toolchain_cache_roots(config: CodingPackConfig) -> list[Path]:
    """Cache roots the configured toolchain resolves into, host-normalized.

    The sandbox child env is scrubbed to an allowlist that passes ``HOME``
    but no ``*_CACHE_DIR`` overrides, so a tool's *default* cache location
    resolves identically inside and outside the sandbox — binding the
    default is correct by construction. A toolchain contributes its cache
    root when any configured check or preparation recipe invokes it:

    - ``uv``: ``~/.cache/uv`` (wheel resolution for ``uv sync`` / ``uv run``).
    - ``go``: ``~/.cache/go-build`` (build cache) and ``~/go/pkg/mod``
      (module cache) — the roots the Go-era floor carried by hand.
    - ``npm``/``node``: ``~/.npm`` (fetch cache; ``node_modules`` lives in
      the worktree, which the worktree-parent grant already covers).

    Only roots that exist on the host are returned: floor ``rw`` paths bind
    with no existence check, so a missing directory would abort every
    sandboxed run. A machine without a cache yet simply contributes
    nothing — the first ``uv sync`` outside the sandbox creates it.
    """
    argvs = [list(check.argv) for check in config.checks]
    argvs += [list(recipe.prepare_argv) for recipe in config.preparation_recipes]
    joined = " " + " ".join(" ".join(argv) for argv in argvs if argv) + " "
    roots: list[Path] = []
    if " uv " in joined:
        roots.append(Path("~/.cache/uv").expanduser())
    if " go " in joined:
        roots.extend(
            [Path("~/.cache/go-build").expanduser(), Path("~/go/pkg/mod").expanduser()]
        )
    if " npm " in joined:
        roots.append(Path("~/.npm").expanduser())
    seen: list[Path] = []
    for root in roots:
        if root not in seen and root.is_dir():
            seen.append(root)
    return seen


def floor_contribution(
    pack_config: Mapping[str, Any],
    context: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return this pack's sandbox floor needs, or ``None`` to opt out.

    Structural needs only: the pack's checks run *inside* the sandbox
    (``execution: sandbox``) with direct-write persistence into owned
    worktrees, and the worktree parent must be writable. The parent is a
    runtime-derived path the harness cannot know when authoring the
    session floor — this contribution is what keeps the floor and the
    actual worktree location from drifting apart. The toolchain cache
    roots follow the same principle: the platform sets the location, the
    session config names the platform, and the floor follows.

    Network is deliberately *not* contributed: whether a session's
    preparation recipe needs network is toolchain-dependent (``uv sync``
    does; pytest does not). The authored floor's network axis flows to
    checks unchanged, and a recipe that needs network under a ``deny``
    floor fails at recipe time with a precise cause — a clearer signal
    than refusing to load the pack at all.

    Path expansion mirrors ``resolve_worktree_parent``: ``~`` expands and
    the parent resolves so the bind matches the real path bwrap sees.
    ``context`` carries the kernel's :class:`SandboxFloorContext` fields
    (``data_dir``, ``workspace_root``) as a mapping.
    """
    config = CodingPackConfig.model_validate(dict(pack_config))
    data_dir = str(context.get("data_dir") or "")
    source_root = config.resolve_workspace_root()
    worktree_parent = config.resolve_worktree_parent(
        data_dir=data_dir, source_root=source_root
    ).resolve()
    paths = {str(worktree_parent): "rw"}
    # The workspace itself stays writable for in-place fixes and format
    # checks only when it is outside the worktree parent; nested paths with
    # different access would conflict in the exclusive merge, and a
    # worktree parent containing the workspace means one grant covers both.
    workspace = source_root.resolve()
    if not workspace.is_relative_to(worktree_parent):
        paths[str(workspace)] = "rw"
    for cache_root in _toolchain_cache_roots(config):
        paths.setdefault(str(cache_root), "rw")
    return {
        "execution": "sandbox",
        "persistence": "direct-write",
        "paths": paths,
    }


__all__ = ["floor_contribution"]
