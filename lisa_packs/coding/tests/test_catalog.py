"""Catalog tests for the coding pack.

The pack deliberately exposes a tool-only surface plus Copilot host commands.
The legacy ``coding`` and ``learn`` modes were removed in cc8ca59
("feat(personas): export coding tools and delete legacy mode"); these tests read
the real catalog so they stay honest if the tool inventory changes.
"""

from __future__ import annotations

import json

from sdk.content import (
    build_mode_catalog_json,
    build_tool_catalog_json,
    resolve_mode_roots,
    resolve_tool_roots,
)
from lisa_packs.coding import get_content_root

# Read-only tools that inspect the source workspace or an owned worktree.
_READ_TOOLS = {
    "coding_list_directory",
    "coding_find_files",
    "coding_read_file",
    "coding_search_code",
    "coding_outline_source",
    "coding_status",
    "coding_read_copilot_customizations",
}

# Tools that mutate the worktree / durable artifact or run a check. All are
# effect ``write`` and keep the user-facing approval gate.
_LOCAL_WRITE_TOOLS = {
    "coding_create_worktree",
    "coding_apply_patch",
    "coding_edit_files",
    "coding_prepare_workspace",
    "coding_run_check",
    "coding_commit",
    "coding_export_patches",
    "coding_cleanup_worktree",
}

# Publication reaches outside the workspace, so it is ``external_write``.
_EXTERNAL_WRITE_TOOLS = {"coding_push"}

_ALL_TOOLS = _READ_TOOLS | _LOCAL_WRITE_TOOLS | _EXTERNAL_WRITE_TOOLS


def _catalog() -> tuple[dict, dict]:
    root = get_content_root()
    tools = json.loads(build_tool_catalog_json(resolve_tool_roots([root])))
    modes = json.loads(build_mode_catalog_json(resolve_mode_roots([root])))
    return tools, modes


def test_pack_exposes_tool_only_surface_with_no_modes() -> None:
    """Post-cc8ca59 the pack owns its bounded tools and declares no modes."""
    tools, modes = _catalog()

    assert set(tools) == _ALL_TOOLS
    assert modes == {}


def test_read_tools_have_read_effect_without_approval_gate() -> None:
    """Source/worktree inspection never blocks on an approval prompt."""
    tools, _ = _catalog()

    for tool_id in _READ_TOOLS:
        assert tools[tool_id]["effect"] == "read", tool_id
        assert tools[tool_id]["approval_required"] is False, tool_id


def test_local_write_tools_are_write_effect_and_approval_gated() -> None:
    """Worktree mutation and check execution keep the approval boundary."""
    tools, _ = _catalog()

    for tool_id in _LOCAL_WRITE_TOOLS:
        assert tools[tool_id]["effect"] == "write", tool_id
        assert tools[tool_id]["approval_required"] is True, tool_id


def test_push_is_external_write_and_approval_gated() -> None:
    """Publication mutates remote state, so it is external_write + gated."""
    tools, _ = _catalog()

    for tool_id in _EXTERNAL_WRITE_TOOLS:
        assert tools[tool_id]["effect"] == "external_write", tool_id
        assert tools[tool_id]["approval_required"] is True, tool_id


def test_every_tool_reports_this_pack_id() -> None:
    """Each tool is attributed to the coding distribution in the catalog."""
    tools, _ = _catalog()

    assert tools, "expected at least one tool in the catalog"
    for tool_id, spec in tools.items():
        assert spec["pack_id"] == "lisa-pack-coding", tool_id

