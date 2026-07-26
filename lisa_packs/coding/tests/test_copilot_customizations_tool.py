"""Integration tests for the coding_read_copilot_customizations tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from kernel.models import ToolExecutionContext
from lisa_packs.coding.tools.read_copilot_customizations.read_copilot_customizations import (
    execute as read_copilot_customizations,
)


def _context(root: Path, parameters: dict[str, object]) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="test",
        mode_id="coding",
        parameters=parameters,
        pack_config={"workspace_root": str(root)},
    )


def _workspace(tmp_path: Path) -> Path:
    """Build a minimal git-worktree workspace with Copilot customizations."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".github" / "instructions").mkdir(parents=True)
    (tmp_path / ".github" / "skills").mkdir(parents=True)
    (tmp_path / ".github" / "agents").mkdir(parents=True)
    (tmp_path / ".github" / "prompts").mkdir(parents=True)

    (tmp_path / ".github" / "copilot-instructions.md").write_text(
        "Project-wide rules.\n", encoding="utf-8"
    )
    (tmp_path / "AGENTS.md").write_text("Agent rules.\n", encoding="utf-8")
    (tmp_path / ".github" / "instructions" / "python.instructions.md").write_text(
        "---\nname: 'Python'\napplyTo: '**/*.py'\n---\nFollow PEP 8.\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "instructions" / "md.instructions.md").write_text(
        "---\nname: 'Markdown'\napplyTo: '**/*.md'\n---\nUse fenced blocks.\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "skills" / "webapp-testing" / "SKILL.md").parent.mkdir(
        parents=True, exist_ok=True
    )
    (tmp_path / ".github" / "skills" / "webapp-testing" / "SKILL.md").write_text(
        "---\nname: webapp-testing\ndescription: 'Webapp testing skill.'\n---\nSteps.\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "agents" / "reviewer.agent.md").write_text(
        "---\nname: 'Reviewer'\ntools: [edit, search]\n---\nReview body.\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "prompts" / "review.prompt.md").write_text(
        "---\ndescription: 'd'\nagent: 'Reviewer'\n---\nHand off body.\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_read_all_customizations(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    result = await read_copilot_customizations(_context(root, {}))

    assert result.error == ""
    out = result.output_json
    assert out is not None
    assert len(out["always_on"]) == 2
    assert len(out["instructions"]) == 2
    assert len(out["agents"]) == 1
    assert len(out["prompts"]) == 1
    assert len(out["skills"]) == 1
    assert out["counts"]["diagnostics"] == 0

    skill_names = [s["name"] for s in out["skills"]]
    assert skill_names == ["webapp-testing"]


@pytest.mark.asyncio
async def test_filter_instructions_by_file_path(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    result = await read_copilot_customizations(
        _context(root, {"file_path": "kernel/ipsar_fsm.py"})
    )

    out = result.output_json
    assert out is not None
    # Only the Python instruction matches the .py file.
    assert len(out["instructions"]) == 1
    assert out["instructions"][0]["name"] == "Python"
    # Other kinds are not affected by file_path filtering.
    assert len(out["agents"]) == 1
    assert len(out["skills"]) == 1


@pytest.mark.asyncio
async def test_filter_by_kind(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    result = await read_copilot_customizations(_context(root, {"kind": "skills"}))

    out = result.output_json
    assert out is not None
    assert len(out["skills"]) == 1
    assert out["counts"]["instructions"] == 0
    assert out["counts"]["agents"] == 0
    assert out["counts"]["prompts"] == 0
    assert out["counts"]["always_on"] == 0


@pytest.mark.asyncio
async def test_empty_workspace_returns_empty_bundle(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    result = await read_copilot_customizations(_context(tmp_path, {}))

    assert result.error == ""
    out = result.output_json
    assert out is not None
    assert out["counts"]["always_on"] == 0
    assert out["counts"]["instructions"] == 0
    assert out["counts"]["skills"] == 0


@pytest.mark.asyncio
async def test_skill_name_mismatch_surfaces_as_diagnostic(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    # Overwrite the skill with a name that does not match its directory.
    (root / ".github" / "skills" / "webapp-testing" / "SKILL.md").write_text(
        "---\nname: wrong-name\ndescription: 'd'\n---\nbody\n",
        encoding="utf-8",
    )
    result = await read_copilot_customizations(_context(root, {}))

    out = result.output_json
    assert out is not None
    assert out["counts"]["diagnostics"] == 1
    assert "wrong-name" in out["diagnostics"][0]["message"]