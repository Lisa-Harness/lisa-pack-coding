"""Shared, bounded Git revision helpers for read-only inspection tools."""

from __future__ import annotations

from pathlib import Path

from lisa_packs.coding.config import CodingPackConfig
from lisa_packs.coding.workflow import CodingWorkflowError, run_git


async def resolve_commit(root: Path, revision: str, *, config: CodingPackConfig) -> str:
    """Resolve a user-supplied revision expression to an immutable commit SHA."""

    result = await run_git(
        root,
        (
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ),
        config=config,
    )
    if result.return_code != 0:
        raise CodingWorkflowError(
            result.stderr.strip() or f"Cannot resolve Git revision {revision!r}."
        )
    commit = result.stdout.strip()
    if not commit or any(
        character not in "0123456789abcdefABCDEF" for character in commit
    ):
        raise CodingWorkflowError(f"Git returned an invalid commit for {revision!r}.")
    return commit


async def workspace_prefix(root: Path, *, config: CodingPackConfig) -> str:
    """Return the repository-relative prefix of a workspace directory."""

    result = await run_git(root, ("rev-parse", "--show-prefix"), config=config)
    if result.return_code != 0:
        raise CodingWorkflowError(
            result.stderr.strip() or "Cannot resolve workspace Git prefix."
        )
    return result.stdout.strip()
