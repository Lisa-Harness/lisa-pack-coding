"""Unit tests for the verified-facts recording tool's revision gate."""

from __future__ import annotations

from lisa_packs.coding.tools.record_fact.record_fact import revisions_match


def test_revision_gate_accepts_exact_match() -> None:
    head = "6d4ffbc5a9f50f1fd972316acbe84f5b93e265e4"
    assert revisions_match(head, head) is True
    assert revisions_match("  " + head + "\n", head) is True
    assert revisions_match(head.upper(), head) is True


def test_revision_gate_refuses_prefixes_and_mismatches() -> None:
    """Facts must be recorded against the full checked commit, not a prefix."""
    head = "6d4ffbc5a9f50f1fd972316acbe84f5b93e265e4"
    assert revisions_match(head[:7], head) is False
    assert revisions_match("0dfdda53693ce7d5eba39456afc3ffac26b8c7a4", head) is False
    assert revisions_match("", head) is False
