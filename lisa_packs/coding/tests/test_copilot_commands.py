"""Tests for Copilot customization host commands.

Tests the prompt and agent command handlers directly with a mock runtime,
avoiding the conftest.py catalog setup that triggers pre-existing
operation registration issues.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lisa_packs.coding.copilot_commands import (
    _discover_bundle,
    _stem_from_path,
    get_host_commands,
    handle_agent_command,
    handle_prompt_command,
)


class TestStemFromPath:
    def test_prompt_stem(self) -> None:
        assert _stem_from_path(".github/prompts/code-review.prompt.md") == "code-review"

    def test_agent_stem(self) -> None:
        assert _stem_from_path(".github/agents/refactor.agent.md") == "refactor"

    def test_instructions_stem(self) -> None:
        assert _stem_from_path(".github/instructions/python.instructions.md") == "python"

    def test_no_suffix(self) -> None:
        assert _stem_from_path("AGENTS.md") == "AGENTS"

    def test_backslash_path(self) -> None:
        assert _stem_from_path(r".github\prompts\code-review.prompt.md") == "code-review"


class TestGetHostCommands:
    def test_registers_prompt_and_agent_commands(self) -> None:
        commands = get_host_commands()
        prefixes = {c.prefix for c in commands}
        assert "/_copilot_prompt" in prefixes
        assert "/_copilot_agent" in prefixes

    def test_help_text_present(self) -> None:
        commands = get_host_commands()
        for c in commands:
            assert c.help_text


class TestHandlePromptCommand:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self) -> None:
        runtime = MagicMock()
        result = await handle_prompt_command(runtime, "", "")
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_unknown_prompt_shows_available(self) -> None:
        runtime = MagicMock()
        # Mock _discover_bundle to return empty prompts.
        import lisa_packs.coding.copilot_commands as mod

        orig = mod._discover_bundle
        mod._discover_bundle = lambda _runtime: ((), ())
        try:
            result = await handle_prompt_command(runtime, "nonexistent", "")
            assert "No prompt named" in result
        finally:
            mod._discover_bundle = orig

    @pytest.mark.asyncio
    async def test_matched_prompt_injects_body(self) -> None:
        from sdk.content import PromptFile

        runtime = MagicMock()
        prompt = PromptFile(
            path=".github/prompts/code-review.prompt.md",
            name="Code Review",
            description="Review code changes",
            agent="Code Reviewer",
            argument_hint=None,
            tools=(),
            model=None,
            body="Resolve the changeset and hand off to the reviewer.",
        )

        import lisa_packs.coding.copilot_commands as mod

        orig = mod._discover_bundle
        mod._discover_bundle = lambda _runtime: ((prompt,), ())
        try:
            result = await handle_prompt_command(runtime, "code-review", "")
            assert "## Prompt: Code Review" in result
            assert "Resolve the changeset" in result
            assert "Target agent: Code Reviewer" in result
        finally:
            mod._discover_bundle = orig

    @pytest.mark.asyncio
    async def test_matched_prompt_with_agent_injects_agent_block(self) -> None:
        from sdk.content import AgentFile, PromptFile

        runtime = MagicMock()
        prompt = PromptFile(
            path=".github/prompts/code-review.prompt.md",
            name="Code Review",
            description="d",
            agent="Code Reviewer",
            argument_hint=None,
            tools=(),
            model=None,
            body="Prompt body.",
        )
        agent = AgentFile(
            path=".github/agents/code-review.agent.md",
            name="Code Reviewer",
            description="Review code",
            tools=("edit", "search"),
            agents=None,
            model=None,
            user_invocable=True,
            disable_model_invocation=False,
            argument_hint=None,
            handoffs=(),
            body="Review body.",
            format="vscode",
        )

        import lisa_packs.coding.copilot_commands as mod

        orig = mod._discover_bundle
        mod._discover_bundle = lambda _runtime: ((prompt,), (agent,))
        try:
            result = await handle_prompt_command(runtime, "code-review", "")
            assert "## Agent: Code Reviewer" in result
            assert "Tools: edit, search" in result
            assert "Review body." in result
        finally:
            mod._discover_bundle = orig

    @pytest.mark.asyncio
    async def test_user_args_passed_through(self) -> None:
        from sdk.content import PromptFile

        runtime = MagicMock()
        prompt = PromptFile(
            path=".github/prompts/code-review.prompt.md",
            name="Code Review",
            description="d",
            agent=None,
            argument_hint=None,
            tools=(),
            model=None,
            body="Prompt body.",
        )

        import lisa_packs.coding.copilot_commands as mod

        orig = mod._discover_bundle
        mod._discover_bundle = lambda _runtime: ((prompt,), ())
        try:
            result = await handle_prompt_command(runtime, "code-review", "review PR #42")
            assert "User request: review PR #42" in result
        finally:
            mod._discover_bundle = orig


class TestHandleAgentCommand:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self) -> None:
        runtime = MagicMock()
        result = await handle_agent_command(runtime, "", "")
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_unknown_agent_shows_available(self) -> None:
        runtime = MagicMock()
        import lisa_packs.coding.copilot_commands as mod

        orig = mod._discover_bundle
        mod._discover_bundle = lambda _runtime: ((), ())
        try:
            result = await handle_agent_command(runtime, "nonexistent", "")
            assert "No agent named" in result
        finally:
            mod._discover_bundle = orig

    @pytest.mark.asyncio
    async def test_matched_agent_injects_instructions(self) -> None:
        from sdk.content import AgentFile

        runtime = MagicMock()
        agent = AgentFile(
            path=".github/agents/refactor.agent.md",
            name="Refactor Engineer",
            description="Refactor agent",
            tools=("edit", "search", "read"),
            agents=None,
            model="gpt-5.1-codex-max",
            user_invocable=True,
            disable_model_invocation=False,
            argument_hint="file path or glob",
            handoffs=(),
            body="Steps for refactoring.",
            format="vscode",
        )

        import lisa_packs.coding.copilot_commands as mod

        orig = mod._discover_bundle
        mod._discover_bundle = lambda _runtime: ((), (agent,))
        try:
            result = await handle_agent_command(runtime, "refactor", "")
            assert "## Agent: Refactor Engineer" in result
            assert "Allowed tools: edit, search, read" in result
            assert "Preferred model: gpt-5.1-codex-max" in result
            assert "Steps for refactoring." in result
        finally:
            mod._discover_bundle = orig

    @pytest.mark.asyncio
    async def test_user_args_passed_through(self) -> None:
        from sdk.content import AgentFile

        runtime = MagicMock()
        agent = AgentFile(
            path=".github/agents/refactor.agent.md",
            name="Refactor Engineer",
            description="d",
            tools=(),
            agents=None,
            model=None,
            user_invocable=True,
            disable_model_invocation=False,
            argument_hint=None,
            handoffs=(),
            body="Refactor body.",
            format="vscode",
        )

        import lisa_packs.coding.copilot_commands as mod

        orig = mod._discover_bundle
        mod._discover_bundle = lambda _runtime: ((), (agent,))
        try:
            result = await handle_agent_command(runtime, "refactor", "refactor kernel/ipsar_fsm.py")
            assert "User request: refactor kernel/ipsar_fsm.py" in result
        finally:
            mod._discover_bundle = orig

    @pytest.mark.asyncio
    async def test_match_by_front_matter_name(self) -> None:
        from sdk.content import AgentFile

        runtime = MagicMock()
        agent = AgentFile(
            path=".github/agents/cr.agent.md",
            name="Code Reviewer",
            description="d",
            tools=(),
            agents=None,
            model=None,
            user_invocable=True,
            disable_model_invocation=False,
            argument_hint=None,
            handoffs=(),
            body="Review body.",
            format="vscode",
        )

        import lisa_packs.coding.copilot_commands as mod

        orig = mod._discover_bundle
        mod._discover_bundle = lambda _runtime: ((), (agent,))
        try:
            # Match by front-matter name "Code Reviewer", not stem "cr"
            result = await handle_agent_command(runtime, "Code Reviewer", "")
            assert "## Agent: Code Reviewer" in result
        finally:
            mod._discover_bundle = orig


class TestDiscoverBundle:
    def test_graceful_on_discovery_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_discover_bundle returns empty tuples when discovery raises."""
        runtime = MagicMock()
        import lisa_packs.coding.copilot_commands as mod

        def _failing_discover(_workspace_root):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "discover_copilot_customizations", _failing_discover, raising=False)
        # Force re-import of sdk.content path by clearing the cached failure.
        # The function catches Exception, so RuntimeError is caught.
        prompts, agents = mod._discover_bundle(runtime)
        assert prompts == ()
        assert agents == ()