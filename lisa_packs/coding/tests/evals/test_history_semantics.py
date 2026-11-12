"""History-semantics eval: the coding pack's ``history:`` claims on real results.

Drives ``create_worktree → status → read → read → apply_patch → read →
read@revision → git_diff → verify → status`` against a throwaway repo and
feeds every real ``ToolResult`` through the kernel's history ledger exactly
as ``run_turn`` does. Asserts which results go stale (and why), that
revision-pinned reads survive edits, that the anchor resolver reproduces a
pinned excerpt, and that the snapshot hook summarizes the live artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kernel.context.mcp_catalog import _deserialize_tool
from kernel.history import HistoryLedger, HistoryRecord, resolve_history_keys
from packstest.catalog import build_tool_catalog
from packstest.context import ToolContextFactory
from packstest.git import git_repo
from packstest.report import Trajectory
from packstest.runner import run_tool

from lisa_packs.coding.tests.evals.test_worktree_trajectory import (
    PATCH,
    _config,
    _verified_sandbox,
)
from lisa_packs.coding.workflow import ProcessResult

PACK_PATH = Path(__file__).resolve().parents[4]


@pytest.fixture()
def catalog() -> dict[str, dict[str, object]]:
    from packstest.catalog import _ensure_source_importable

    _ensure_source_importable(PACK_PATH)
    return build_tool_catalog(PACK_PATH)


class _Driver:
    """Run real tools and record their history claims like the turn runner."""

    def __init__(
        self, catalog: dict[str, dict[str, object]], factory: ToolContextFactory
    ) -> None:
        self.catalog = catalog
        self.factory = factory
        self.ledger = HistoryLedger()
        self.trajectory = Trajectory(name="history-semantics")
        self.results: dict[str, Any] = {}

    async def call(
        self,
        call_id: str,
        tool_id: str,
        params: dict[str, Any],
        *,
        fails: bool = False,
    ) -> Any:
        run = await run_tool(self.catalog, tool_id, self.factory, params)
        self.trajectory.record(run)
        assert bool(run.result.error) is fails, f"{tool_id}: {run.result.error}"
        definition = _deserialize_tool(self.catalog[tool_id])
        claims = resolve_history_keys(
            definition.history, definition.implementation_module, params, run.result
        )
        if claims is not None:
            self.ledger.record(
                HistoryRecord(
                    call_id=call_id,
                    tool_id=tool_id,
                    owner_id=definition.pack_id,
                    keys=list(claims.keys),
                    invalidates=list(claims.invalidates),
                    scope=claims.scope,
                    anchor=claims.anchor,
                )
            )
        self.results[call_id] = run.result
        return run.result

    def record(self, call_id: str) -> HistoryRecord:
        return next(r for r in self.ledger.records if r.call_id == call_id)


@pytest.mark.eval
@pytest.mark.asyncio
async def test_coding_history_claims_fold_only_stale_results(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = await git_repo(tmp_path)
    await repo.commit_file("other.txt", "untouched\n", "other")
    base = await repo.head()
    factory = ToolContextFactory(
        root=repo.root, pack_config=_config(repo.root), data_dir=tmp_path / "data"
    )
    factory.identity["task_id"] = "history-task"
    _verified_sandbox(factory)
    drive = _Driver(catalog, factory)

    created = await drive.call("c0", "coding_create_worktree", {"summary": "history"})
    artifact = str(created.output_json["artifact_id"])
    read = {"artifact_id": artifact, "path": "app.txt"}
    await drive.call("s1", "coding_status", {"artifact_id": artifact})
    await drive.call("r1", "coding_read_file", read)
    await drive.call("r2", "coding_read_file", read)
    await drive.call(
        "o1", "coding_read_file", {"artifact_id": artifact, "path": "other.txt"}
    )
    await drive.call("p1", "coding_read_file", {**read, "revision": base})
    await drive.call(
        "d1", "coding_git_diff", {"artifact_id": artifact, "comparison": "working_tree"}
    )

    await drive.call(
        "d2",
        "coding_git_diff",
        {"artifact_id": artifact, "comparison": "commit", "revision": base},
    )
    # A smaller page of the same diff is a different result, not a supersession.
    await drive.call(
        "d3",
        "coding_git_diff",
        {"artifact_id": artifact, "comparison": "commit", "revision": base, "limit": 1},
    )
    assert drive.record("r1").dead and drive.record("r1").stale_reason == "superseded"
    assert drive.record("r1").stale_by == "r2" and not drive.record("r2").dead
    assert not drive.record("d2").dead

    await drive.call(
        "e1", "coding_apply_patch", {"artifact_id": artifact, "patch": PATCH}
    )
    assert drive.record("e1").scope == "turn"
    for stale in ("r2", "s1", "d1"):
        assert drive.record(stale).dead, stale
        assert drive.record(stale).stale_reason == "invalidated"
    # Unrelated paths, immutable revision reads, and SHA-pinned diffs survive the edit.
    for live in ("o1", "p1", "d2", "d3"):
        assert not drive.record(live).dead, live

    await drive.call("r3", "coding_read_file", read)
    assert "hello from lisa" in drive.results["r3"].output_json["excerpt"]
    await drive.call("v1", "coding_verify", {"artifact_id": artifact})
    await drive.call("v2", "coding_verify", {"artifact_id": artifact})
    assert drive.record("v1").dead and not drive.record("v2").dead
    await drive.call("s2", "coding_status", {"artifact_id": artifact})
    assert not drive.record("s2").dead and not drive.record("r3").dead

    # Only revision-pinned reads carry an anchor; it reproduces the excerpt.
    assert drive.record("r3").anchor == ""
    anchor = drive.record("p1").anchor
    assert anchor.startswith(f"coding_read_file:{artifact}:app.txt@{base}:")
    module = _deserialize_tool(catalog["coding_read_file"]).implementation_module
    ctx = factory.context(tool_id="coding_read_file")
    resolved = await module.resolve_history_anchor(ctx, anchor)
    assert (
        resolved is not None and "\thello" in resolved and "from lisa" not in resolved
    )

    status = _deserialize_tool(catalog["coding_status"]).implementation_module
    keys = [k for r in drive.ledger.records for k in r.keys]
    snapshot = await status.history_snapshot(
        factory.context(tool_id="coding_status"), keys
    )
    assert snapshot.startswith(f"artifact {artifact}: ")
    assert "M app.txt" in snapshot

    async def _failing_git(*_args: Any, **_kwargs: Any) -> Any:
        return ProcessResult(return_code=128, stdout="", stderr="fatal")

    monkeypatch.setattr(status, "run_git", _failing_git)
    failed = await status.history_snapshot(
        factory.context(tool_id="coding_status"), keys
    )
    assert failed.endswith("worktree: unknown (git status failed)")

    dead = {r.call_id for r in drive.ledger.records if r.dead}
    assert dead == {"r1", "r2", "s1", "d1", "v1"}
    turn_scoped = {r.call_id for r in drive.ledger.foldable(turn_finished=True)} - dead
    assert turn_scoped == {"e1"}


@pytest.mark.eval
@pytest.mark.asyncio
async def test_auto_rebase_stales_reads_of_upstream_paths_even_on_failure(
    tmp_path: Path, catalog: dict[str, dict[str, object]]
) -> None:
    repo = await git_repo(tmp_path)
    await repo.commit_file("other.txt", "untouched\n", "other")
    await repo.commit_file("calm.txt", "calm\n", "calm")
    base = await repo.head()
    factory = ToolContextFactory(
        root=repo.root, pack_config=_config(repo.root), data_dir=tmp_path / "data"
    )
    factory.identity["task_id"] = "rebase-task"
    _verified_sandbox(factory)
    drive = _Driver(catalog, factory)

    created = await drive.call("c0", "coding_create_worktree", {"summary": "rebase"})
    artifact = str(created.output_json["artifact_id"])
    other = {"artifact_id": artifact, "path": "other.txt"}
    await drive.call("o1", "coding_read_file", other)
    await drive.call("k1", "coding_read_file", {"artifact_id": artifact, "path": "calm.txt"})
    await drive.call("p1", "coding_read_file", {**other, "revision": base})

    # Upstream moves other.txt; the edit to app.txt rebases first.
    await repo.commit_file("other.txt", "upstream 1\n", "upstream 1")
    edited = await drive.call(
        "e1", "coding_apply_patch", {"artifact_id": artifact, "patch": PATCH}
    )
    assert edited.output_json["sync"]["upstream_paths"] == ["other.txt"]
    assert drive.record("o1").dead and drive.record("o1").stale_by == "e1"
    # Paths upstream left alone and SHA-pinned reads survive the rebase.
    assert not drive.record("k1").dead and not drive.record("p1").dead

    # A rejected edit still rebased first, so its reads of moved paths go stale.
    await drive.call("o2", "coding_read_file", other)
    await repo.commit_file("other.txt", "upstream 2\n", "upstream 2")
    stale_hash = {
        "kind": "update",
        "path": "app.txt",
        "expected_sha256": "0" * 64,
        "replacements": [{"old_text": "hello", "new_text": "bye"}],
    }
    failed = await drive.call(
        "e2",
        "coding_edit_files",
        {"artifact_id": artifact, "edits": [stale_hash]},
        fails=True,
    )
    assert "changed since it was read" in failed.error
    assert drive.record("o2").dead and drive.record("o2").stale_by == "e2"
    assert drive.record("e2").keys == [] and drive.record("e2").scope == "task"

    # Verification and commit sync too.
    await drive.call("o3", "coding_read_file", other)
    await repo.commit_file("other.txt", "upstream 3\n", "upstream 3")
    await drive.call("v1", "coding_verify", {"artifact_id": artifact})
    assert drive.record("o3").dead and drive.record("o3").stale_by == "v1"

    await drive.call("o4", "coding_read_file", other)
    await repo.commit_file("other.txt", "upstream 4\n", "upstream 4")
    await drive.call(
        "m1", "coding_commit", {"artifact_id": artifact, "message": "Rebased change"}
    )
    assert drive.record("o4").dead and drive.record("o4").stale_by == "m1"
    assert not drive.record("k1").dead and not drive.record("p1").dead
