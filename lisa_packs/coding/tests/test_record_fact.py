"""Unit tests for the verified-facts recording tool's revision gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from packstest.catalog import build_tool_catalog
from packstest.context import ToolContextFactory
from packstest.git import git_repo
from packstest.runner import run_tool
from sdk.workproducts import WorkProductCheck

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.tools.record_fact.record_fact import revisions_match
from lisa_packs.coding.workflow import candidate_tree_sha, run_git


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


@pytest.mark.eval
@pytest.mark.asyncio
async def test_fact_requires_current_required_checks_and_clean_owned_commit(
    tmp_path: Path,
) -> None:
    pack_path = Path(__file__).resolve().parents[3]
    catalog = build_tool_catalog(pack_path)
    repo = await git_repo(tmp_path)
    await repo.commit_file("app.txt", "original\n", "initial")
    factory = ToolContextFactory(
        root=repo.root,
        data_dir=tmp_path / "data",
        pack_config={
            "workspace_root": str(repo.root),
            "checks": [{"id": "smoke", "argv": ["true"], "timeout_seconds": 60}],
            "required_check_ids": ["smoke"],
        },
    )
    factory.identity["task_id"] = "record-fact-task"
    created = await run_tool(
        catalog, "coding_create_worktree", factory, {"summary": "fact"}
    )
    assert not created.result.error, created.result.error
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))

    def parameters(revision: str) -> dict[str, str]:
        return {
            "artifact_id": artifact_id,
            "claim": "app.txt contains the original text",
            "at": "app.txt:1",
            "revision": revision,
        }

    config = get_coding_config(
        factory.context(parameters("0" * 40), tool_id="coding_record_fact")
    )
    head = await run_git(worktree, ("rev-parse", "HEAD"), config=config)
    assert head.return_code == 0
    commit_sha = head.stdout.strip()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None
    artifact.commit_sha = commit_sha
    artifact.head_sha = commit_sha
    await factory.artifact_store.save(artifact)
    unchecked = await run_tool(
        catalog, "coding_record_fact", factory, parameters(commit_sha)
    )
    assert "required check" in unchecked.result.error

    # A matching HEAD alone cannot certify missing or stale check receipts.
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None
    artifact.checks["smoke"] = WorkProductCheck(
        check_id="smoke",
        status="passed",
        revision=artifact.revision,
        checked_tree_sha="stale",
    )
    await factory.artifact_store.save(artifact)
    stale = await run_tool(
        catalog, "coding_record_fact", factory, parameters(commit_sha)
    )
    assert "required check" in stale.result.error

    wrong_revision = await run_tool(
        catalog, "coding_record_fact", factory, parameters("a" * 40)
    )
    assert "does not match HEAD" in wrong_revision.result.error

    artifact.checks["smoke"].checked_tree_sha = await candidate_tree_sha(
        worktree, config=config, paths=artifact.changed_paths
    )
    await factory.artifact_store.save(artifact)
    recorded = await run_tool(
        catalog, "coding_record_fact", factory, parameters(commit_sha)
    )
    assert not recorded.result.error, recorded.result.error
    assert recorded.result.output_json["fact"] == {
        "claim": "app.txt contains the original text",
        "at": "app.txt:1",
        "revision": commit_sha,
    }

    (worktree / "app.txt").write_text("modified\n", encoding="utf-8")
    dirty = await run_tool(
        catalog, "coding_record_fact", factory, parameters(commit_sha)
    )
    assert "dirty" in dirty.result.error
