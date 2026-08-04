"""Catalog tests for the coding pack."""

from __future__ import annotations

import json

from sdk.content import (
    build_mode_catalog_json,
    build_tool_catalog_json,
    resolve_mode_roots,
    resolve_tool_roots,
)
from lisa_packs.coding import get_content_root


def test_coding_pack_catalog_exposes_read_tools_and_workflow_mode() -> None:
    """The pack owns a catalog-native coding mode and bounded read tools."""

    root = get_content_root()
    tool_catalog = json.loads(build_tool_catalog_json(resolve_tool_roots([root])))
    assert set(tool_catalog) >= {
        "coding_list_directory",
        "coding_find_files",
        "coding_read_file",
        "coding_search_code",
        "coding_outline_source",
    }
    assert tool_catalog["coding_read_file"]["pack_id"] == "lisa-pack-coding"

    mode_catalog = json.loads(build_mode_catalog_json(resolve_mode_roots([root])))
    assert mode_catalog["coding"]["tools_authoritative"] is True
    assert "coding_apply_patch" in mode_catalog["coding"]["allowed_tool_ids"]
    assert "memory.search" in mode_catalog["coding"]["allowed_tool_ids"]
    assert "recall_memory" in mode_catalog["coding"]["allowed_operation_ids"]


def test_coding_pack_catalog_exposes_learn_mode() -> None:
    """The pack owns a read-only, memory-synthesizing learn mode."""

    root = get_content_root()
    mode_catalog = json.loads(build_mode_catalog_json(resolve_mode_roots([root])))
    assert "learn" in mode_catalog
    learn = mode_catalog["learn"]
    assert learn["mode_kind"] == "chat"
    assert learn["tools_authoritative"] is True
    assert "coding_apply_patch" not in learn["allowed_tool_ids"]
    assert "coding_run_check" not in learn["allowed_tool_ids"]
    assert "coding_commit" not in learn["allowed_tool_ids"]
    assert "memory.add" in learn["allowed_tool_ids"]
    assert "synthesize_and_store_learning" in learn["allowed_operation_ids"]
    assert "recall_memory" in learn["allowed_operation_ids"]
    assert "dispute_memory" in learn["allowed_operation_ids"]
