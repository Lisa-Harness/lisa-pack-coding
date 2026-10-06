"""Pack-validation eval: the coding pack stays structurally healthy.

Runs under the framework's ``eval`` marker (deterministic, no model calls).
Validates the pack from source via ``packstest.catalog.validate_pack``:
distribution metadata, entry points, tool YAML contract, and in-process
importability of every implementation module. This is the first eval any
pack edit should run — it fails fast and names the exact file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packstest.catalog import validate_pack

PACK_PATH = Path(__file__).resolve().parents[4]  # .../packs-wip/coding checkout

EXPECTED_TOOLS = {
    "coding_apply_patch",
    "coding_cleanup_worktree",
    "coding_commit",
    "coding_create_worktree",
    "coding_edit_files",
    "coding_export_patches",
    "coding_find_files",
    "coding_list_directory",
    "coding_list_worktrees",
    "coding_outline_source",
    "coding_push",
    "coding_read_copilot_customizations",
    "coding_read_file",
    "coding_prepare_workspace",
    "coding_run_check",
    "coding_search_code",
    "coding_status",
}


@pytest.mark.eval
def test_coding_pack_validates_from_source() -> None:
    """Entry points load, every tool YAML parses, modules import in-process."""

    report = validate_pack(PACK_PATH)
    assert report.ok, report.summary()


@pytest.mark.eval
def test_coding_pack_tool_catalog_complete() -> None:
    """Every expected tool is discovered with the coding provider id."""

    report = validate_pack(PACK_PATH)
    missing = EXPECTED_TOOLS - set(report.tools)
    unexpected = set(report.tools) - EXPECTED_TOOLS
    assert not missing, f"tools missing from catalog: {sorted(missing)}"
    assert not unexpected, f"unexpected catalog tools: {sorted(unexpected)}"
    for tool_id, tool_dict in sorted(report.tools.items()):
        assert tool_dict["tool_provider_id"] == "lisa-pack-coding", tool_id
        assert tool_dict["pack_id"] == "lisa-pack-coding", tool_id


@pytest.mark.eval
def test_coding_pack_effect_contract() -> None:
    """Write-ish tools declare their effect; read tools must say 'read'."""

    report = validate_pack(PACK_PATH)
    read_tools = {
        "coding_find_files",
        "coding_list_directory",
        "coding_list_worktrees",
        "coding_outline_source",
        "coding_read_copilot_customizations",
        "coding_read_file",
        "coding_search_code",
        "coding_status",
    }
    for tool_id, tool_dict in report.tools.items():
        effect = tool_dict["effect"]
        if tool_id in read_tools:
            assert effect == "read", f"{tool_id} must be effect=read, got {effect!r}"
        else:
            assert effect in {"write", "external_write", "execute"}, (
                f"{tool_id}: unexpected effect {effect!r}"
            )


@pytest.mark.eval
def test_coding_pack_entry_points_load() -> None:
    """Both extension entry points resolve from source."""

    report = validate_pack(PACK_PATH)
    assert "lisa.extensions.content_roots" in report.entry_points
    assert "lisa.extensions.host_commands" in report.entry_points
    for group, entries in report.entry_points.items():
        for name, value in entries.items():
            assert value is not None, f"{group}:{name} failed to load"