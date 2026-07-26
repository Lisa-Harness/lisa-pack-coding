"""Copilot customization host commands for the coding pack.

Registers each discovered ``.prompt.md`` and ``.agent.md`` as a slash
command. Prompts inject their body and hand off to the named agent.
Agents inject their instructions as system context.

Command prefixes are derived from the filename stem (Copilot convention):
``code-review.prompt.md`` → ``/code-review``,
``refactor.agent.md`` → ``/refactor``.

Entry point: ``lisa.extensions.host_commands`` → ``get_host_commands()``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kernel.host_commands import HostCommandSpec

if TYPE_CHECKING:
    from hosts.core.runtime import HostRuntime

logger = logging.getLogger(__name__)

__all__ = [
    "get_host_commands",
    "handle_agent_command",
    "handle_prompt_command",
]


def _resolve_workspace_root(runtime: HostRuntime) -> Path:
    """Resolve the workspace root for Copilot customization discovery."""
    try:
        pack_config = runtime.session_plan.manifest.resources.packs.configuration
        coding_config = pack_config.get("lisa-pack-coding", {})
        raw = coding_config.get("workspace_root")
        if raw:
            return Path(raw).expanduser().resolve()
    except Exception:
        pass
    return Path.cwd()


def _discover_bundle(runtime: HostRuntime) -> tuple:
    """Discover Copilot customizations, returning (prompts, agents) tuples.

    Returns empty tuples on failure (graceful degradation).
    """
    try:
        from sdk.content import discover_copilot_customizations

        workspace_root = _resolve_workspace_root(runtime)
        bundle = discover_copilot_customizations(workspace_root)
        return bundle.prompts, bundle.agents
    except Exception:
        logger.warning(
            "Failed to discover Copilot customizations for host commands.",
            exc_info=True,
        )
        return (), ()


def _stem_from_path(path: str) -> str:
    """Derive a slash-command-safe prefix from a customization file path.

    ``code-review.prompt.md`` → ``code-review``
    ``refactor.agent.md`` → ``refactor``
    """
    from pathlib import PurePosixPath

    stem = PurePosixPath(path.replace("\\", "/")).stem
    # Strip known suffixes: .prompt, .agent, .instructions
    for suffix in (".prompt", ".agent", ".instructions"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def get_host_commands() -> list[HostCommandSpec]:
    """Return host commands for discovered Copilot prompts and agents.

    Called by the host at startup via the ``lisa.extensions.host_commands``
    entry point. Discovery is deferred to first invocation of each handler
    so the runtime is available.
    """
    return [
        HostCommandSpec(
            prefix="/_copilot_prompt",
            handler=handle_prompt_command,
            help_text="Invoke a Copilot prompt by name. Usage: /_copilot_prompt <name> [args]",
        ),
        HostCommandSpec(
            prefix="/_copilot_agent",
            handler=handle_agent_command,
            help_text="Invoke a Copilot agent by name. Usage: /_copilot_agent <name> [args]",
        ),
    ]


# --------------------------------------------------------------------------- #
# Name-resolution helpers
# --------------------------------------------------------------------------- #


def _resolve_name_and_args(
    stripped: str,
    items: tuple,
    *,
    stem_fn,
) -> tuple[str | None, str]:
    """Resolve a name from ``stripped``, trying multi-word matches first.

    Front-matter names like "Code Reviewer" contain spaces. We try the
    full ``stripped`` string first (no args), then progressively shorter
    prefixes until we find a match or fall back to the first word.
    """
    if not stripped.strip():
        return None, ""

    # Try the full string as the name (no args).
    for item in items:
        if item.name == stripped or stem_fn(item.path) == stripped:
            return stripped, ""

    # Try progressively shorter prefixes.
    words = stripped.split()
    for i in range(len(words) - 1, 0, -1):
        candidate = " ".join(words[:i])
        for item in items:
            if item.name == candidate or stem_fn(item.path) == candidate:
                return candidate, " ".join(words[i:])

    # Fall back: first word is the name, rest is args.
    parts = stripped.split(None, 1)
    return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""


def _find_by_name_or_stem(
    name: str,
    items: tuple,
    *,
    stem_fn,
) -> object | None:
    """Find an item by front-matter name or filename stem."""
    for item in items:
        if item.name == name or stem_fn(item.path) == name:
            return item
    return None


async def handle_prompt_command(
    runtime: HostRuntime,
    stripped: str,
    message: str,
) -> str:
    """Handle ``/_copilot_prompt <name> [args]``.

    Looks up the named prompt from discovered ``.prompt.md`` files, injects
    its body as context, and hands off to the prompt's target agent if one
    is named.
    """
    if not stripped.strip():
        return "Usage: /_copilot_prompt <name> [args]\n\nAvailable prompts: use /help to list."

    prompts, _agents = _discover_bundle(runtime)
    name, user_args = _resolve_name_and_args(stripped, prompts, stem_fn=_stem_from_path)

    if name is None:
        available = ", ".join(
            sorted(set(_stem_from_path(p.path) for p in prompts))
        )
        return (
            f"No prompt matching '{stripped}' found.\n"
            f"Available prompts: {available or '(none)'}"
        )

    matched = _find_by_name_or_stem(name, prompts, stem_fn=_stem_from_path)
    if matched is None:
        available = ", ".join(
            sorted(set(_stem_from_path(p.path) for p in prompts))
        )
        return (
            f"No prompt named '{name}' found.\n"
            f"Available prompts: {available or '(none)'}"
        )

    # Build the context block.
    lines: list[str] = []
    lines.append(f"## Prompt: {matched.name}")
    if matched.description:
        lines.append(f"Description: {matched.description}")
    if matched.agent:
        lines.append(f"Target agent: {matched.agent}")
    lines.append("")
    lines.append(matched.body.strip())

    context_block = "\n".join(lines)

    # If the prompt names an agent, look up the agent's instructions too.
    agent_block = ""
    if matched.agent:
        for a in _agents:
            if a.name == matched.agent or _stem_from_path(a.path) == matched.agent:
                agent_lines = [
                    f"## Agent: {a.name}",
                    f"Tools: {', '.join(a.tools) if a.tools else '(none)'}",
                    "",
                    a.body.strip(),
                ]
                agent_block = "\n".join(agent_lines)
                break

    query = user_args or message
    return (
        f"{context_block}\n\n"
        + (f"{agent_block}\n\n" if agent_block else "")
        + f"---\nUser request: {query}"
    )


async def handle_agent_command(
    runtime: HostRuntime,
    stripped: str,
    message: str,
) -> str:
    """Handle ``/_copilot_agent <name> [args]``.

    Looks up the named agent from discovered ``.agent.md`` files and injects
    its instructions as system context. The model self-regulates tool usage
    based on the agent's stated tool list.
    """
    if not stripped.strip():
        return "Usage: /_copilot_agent <name> [args]\n\nAvailable agents: use /help to list."

    _prompts, agents = _discover_bundle(runtime)
    name, user_args = _resolve_name_and_args(stripped, agents, stem_fn=_stem_from_path)

    if name is None:
        available = ", ".join(
            sorted(set(_stem_from_path(a.path) for a in agents))
        )
        return (
            f"No agent matching '{stripped}' found.\n"
            f"Available agents: {available or '(none)'}"
        )

    matched = _find_by_name_or_stem(name, agents, stem_fn=_stem_from_path)
    if matched is None:
        available = ", ".join(
            sorted(set(_stem_from_path(a.path) for a in agents))
        )
        return (
            f"No agent named '{name}' found.\n"
            f"Available agents: {available or '(none)'}"
        )

    lines: list[str] = []
    lines.append(f"## Agent: {matched.name}")
    if matched.description:
        lines.append(f"Description: {matched.description}")
    if matched.tools:
        lines.append(f"Allowed tools: {', '.join(matched.tools)}")
    if matched.model:
        lines.append(f"Preferred model: {matched.model}")
    lines.append("")
    lines.append(matched.body.strip())

    context_block = "\n".join(lines)

    query = user_args or message
    return f"{context_block}\n\n---\nUser request: {query}"
