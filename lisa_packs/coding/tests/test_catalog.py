"""Catalog tests for the coding pack.

The pack owns its bounded tools, two Copilot host commands, and three modes:
``coding_execute`` (write lifecycle), ``coding_review`` (read-only review), and
``coding_design`` (code research + design docs). The legacy ``coding``/``learn``
modes were removed in cc8ca59 ("feat(personas): export coding tools and delete
legacy mode"); the executor/reviewer personas moved home from lisa-pack-personas
and were namespaced to ``coding_*`` in 2026-10-04. These tests read the real
catalog so they stay honest if the tool or mode inventory changes.
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
    "coding_list_worktrees",
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
    "coding_import_commits",
    "coding_prepare_workspace",
    "coding_run_check",
    "coding_record_fact",
    "coding_commit",
    "coding_export_patches",
    "coding_cleanup_worktree",
    "coding_history",
}

# Publication reaches outside the workspace, so it is ``external_write``.
_EXTERNAL_WRITE_TOOLS = {"coding_push"}

_ALL_TOOLS = _READ_TOOLS | _LOCAL_WRITE_TOOLS | _EXTERNAL_WRITE_TOOLS

# The pack's mode surface. Read-only modes share the same read tools; the
# execute mode adds the write lifecycle.
_MODES = {"coding_execute", "coding_review", "coding_design"}
_READ_ONLY_MODE_TOOLS = {
    "coding_list_worktrees",
    "coding_list_directory",
    "coding_find_files",
    "coding_read_file",
    "coding_search_code",
    "coding_outline_source",
    "coding_status",
    "coding_read_copilot_customizations",
    # fetch_page is a read (no effect) but opt-in rather than ambient since
    # the sdk-core ambient re-scope; coding modes list it explicitly.
    "fetch_page",
}


def _catalog() -> tuple[dict, dict]:
    root = get_content_root()
    tools = json.loads(build_tool_catalog_json(resolve_tool_roots([root])))
    modes = json.loads(build_mode_catalog_json(resolve_mode_roots([root])))
    return tools, modes


def test_pack_exposes_tool_only_surface_with_no_modes() -> None:
    """The pack owns its tools and exactly its three mode surfaces."""
    tools, modes = _catalog()

    assert set(tools) == _ALL_TOOLS
    assert set(modes) == _MODES


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


def test_read_only_modes_carry_no_write_tools() -> None:
    """coding_review and coding_design are strictly read-only."""
    _, modes = _catalog()

    for mode_id in ("coding_review", "coding_design"):
        allowed = set(modes[mode_id].get("allowed_tool_ids") or ())
        assert allowed == _READ_ONLY_MODE_TOOLS, (mode_id, allowed)


def test_execute_mode_carries_full_write_lifecycle() -> None:
    """coding_execute owns the worktree lifecycle tools plus reads."""
    _, modes = _catalog()

    allowed = set(modes["coding_execute"].get("allowed_tool_ids") or ())
    assert _ALL_TOOLS <= allowed, allowed
    assert "github_open_pull_request" in allowed


def test_edit_descriptions_prefer_original_snapshot_replacements() -> None:
    tools, _ = _catalog()

    description = tools["coding_edit_files"]["description"]
    assert "preferred edit tool" in description
    assert "original file snapshot" in description
    assert "must not overlap" in description
    assert "Prefer coding_edit_files" in tools["coding_apply_patch"]["description"]


def test_modes_route_by_mode_id() -> None:
    """Mode ids are namespaced to the pack and routable."""
    _, modes = _catalog()

    for mode_id in _MODES:
        assert modes[mode_id]["mode_id"] == mode_id
        assert modes[mode_id]["mode_provider_id"] == "lisa-pack-coding"
        assert modes[mode_id]["routable"] is True


def test_every_tool_reports_this_pack_id() -> None:
    """Each tool is attributed to the coding distribution in the catalog."""
    tools, _ = _catalog()

    assert tools, "expected at least one tool in the catalog"
    for tool_id, spec in tools.items():
        assert spec["pack_id"] == "lisa-pack-coding", tool_id
