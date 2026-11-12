"""Harness-run verification (reuse, needs, fixers) and upstream freshness."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from kernel.models import ToolResult
from packstest.store import MemoryWorkProductStore
from pydantic import ValidationError

from lisa_packs.coding.config import CodingPackConfig
from lisa_packs.coding.tests.test_git_workflow import _context, _git, _repository
from lisa_packs.coding.tools.apply_patch.apply_patch import execute as apply_patch
from lisa_packs.coding.tools.commit.commit import execute as commit
from lisa_packs.coding.tools.create_worktree.create_worktree import (
    execute as create_worktree,
)
from lisa_packs.coding.tools.verify.verify import execute as verify
from lisa_packs.coding.verification import input_matches

_PATCH = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-hello\n+hello world\n"
_ADD_NOTES = "--- /dev/null\n+++ b/notes.md\n@@ -0,0 +1 @@\n+notes\n"


def _check(check_id: str, **extra: object) -> dict[str, object]:
    return {"id": check_id, "argv": [sys.executable, "-c", f"print('{check_id}')"], **extra}


class _Counter:
    """Count sandboxed check runs by worktree."""

    def __init__(self) -> None:
        self.runs = 0

    def __call__(self, _worktree: Path) -> None:
        self.runs += 1


async def _patched(
    root: Path,
    store: MemoryWorkProductStore,
    checks: list[dict[str, object]],
    required: list[str] | None = None,
) -> tuple[str, Path]:
    # The artifact snapshots required_check_ids when it is created.
    created = await create_worktree(
        _context(root, store, {"summary": "s"}, checks=checks, required_check_ids=required)
    )
    artifact_id = str(created.output_json["artifact_id"])
    patched = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": _PATCH}, checks=checks)
    )
    assert patched.error == "", patched.error
    return artifact_id, Path(str(created.output_json["worktree_path"]))


@pytest.mark.parametrize(
    ("path", "inputs", "expected"),
    [
        ("app.py", ("**/*.py",), True),
        ("pkg/sub/mod.py", ("**/*.py",), True),
        ("pkg/mod.pyi", ("**/*.py",), False),
        ("pyproject.toml", ("pyproject.toml",), True),
        ("typings/x/y.pyi", ("typings/",), True),
        ("src/a.py", ("src/*.py",), True),
        ("src/deep/a.py", ("src/*.py",), False),
        ("packages/a/src/x.py", ("packages/*/src/",), True),
        ("packages/a/b/src/x.py", ("packages/*/src/",), False),
        ("README.md", ("**/*.py", "pyproject.toml"), False),
    ],
)
def test_input_matches_globs(path: str, inputs: tuple[str, ...], expected: bool) -> None:
    assert input_matches(path, inputs) is expected


@pytest.mark.parametrize(
    ("checks", "message"),
    [
        ([_check("test", needs=["lint"]), _check("lint")], "needs must name earlier checks"),
        ([_check("lint", inputs=["../outside/*.py"])], "normalized relative glob"),
        ([_check("lint", inputs=["/abs/*.py"])], "normalized relative glob"),
        ([_check("lint", inputs=["./src/"])], "normalized relative glob"),
        ([_check("lint", inputs=["src//a.py"])], "normalized relative glob"),
        ([_check("lint"), _check("fmt", kind="fix", needs=["lint"])], "must not declare needs"),
    ],
)
def test_check_attributes_are_validated(
    tmp_path: Path, checks: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CodingPackConfig.model_validate(
            {"workspace_root": str(tmp_path), "checks": checks, "required_check_ids": []}
        )


@pytest.mark.asyncio
async def test_unrelated_change_reuses_check_by_inputs(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    checks = [_check("unit", inputs=["*.txt"]), _check("always", reuse=False)]
    required = ["unit", "always"]
    artifact_id, _ = await _patched(root, store, checks, required)
    counter = _Counter()

    def run(params: dict[str, object]):
        return verify(
            _context(
                root,
                store,
                params,
                checks=checks,
                required_check_ids=required,
                sandbox_effect=counter,
            )
        )

    first = await run({"artifact_id": artifact_id})
    assert first.error == "", first.error
    assert counter.runs == 2

    # A change outside unit's inputs leaves it reusable; ``reuse: false``
    # always reruns.
    added = await apply_patch(
        _context(
            root, store, {"artifact_id": artifact_id, "patch": _ADD_NOTES},
            checks=checks, required_check_ids=required,
        )
    )
    assert added.error == "", added.error
    second = await run({"artifact_id": artifact_id})
    assert second.error == "", second.error
    assert counter.runs == 3
    reused = {item["check_id"]: item["reused"] for item in second.output_json["checks"]}
    assert reused == {"unit": True, "always": False}
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert artifact.checks["unit"].status == "passed"
    assert artifact.checks["unit"].revision == artifact.revision

    # A reused check certifies the commit without another run.
    committed = await commit(
        _context(
            root, store, {"artifact_id": artifact_id, "message": "Change"},
            checks=checks, required_check_ids=required, sandbox_effect=counter,
        )
    )
    assert committed.error == "", committed.error
    assert counter.runs == 4


@pytest.mark.asyncio
async def test_change_inside_inputs_reruns_check(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    checks = [_check("unit", inputs=["*.md"])]
    artifact_id, _ = await _patched(root, store, checks)
    counter = _Counter()
    first = await verify(
        _context(root, store, {"artifact_id": artifact_id}, checks=checks, sandbox_effect=counter)
    )
    assert first.error == "", first.error
    added = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": _ADD_NOTES}, checks=checks)
    )
    assert added.error == "", added.error
    second = await verify(
        _context(root, store, {"artifact_id": artifact_id}, checks=checks, sandbox_effect=counter)
    )
    assert second.error == "", second.error
    assert counter.runs == 2
    assert [item["reused"] for item in second.output_json["checks"]] == [False]


@pytest.mark.asyncio
async def test_failed_dependency_skips_dependent_check(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    checks = [
        {"id": "lint", "argv": [sys.executable, "-c", "raise SystemExit(1)"]},
        _check("unit", needs=["lint"]),
    ]
    artifact_id, _ = await _patched(root, store, checks)
    seen: list[str] = []

    failing = ToolResult(output_text="lint failed", output_json={"return_code": 1})
    report = await verify(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "check_ids": ["unit"]},
            checks=checks,
            sandbox_result=failing,
            sandbox_effect=lambda worktree: seen.append(str(worktree)),
        )
    )

    assert report.error == "", report.error
    statuses = {item["check_id"]: item["status"] for item in report.output_json["checks"]}
    assert statuses == {"lint": "failed", "unit": "skipped"}
    assert len(seen) == 1
    assert report.output_json["ready_to_commit"] is False


@pytest.mark.asyncio
async def test_edit_rebases_dirty_work_onto_moved_upstream(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id, worktree = await _patched(root, store, [_check("unit")])
    before = await store.get(artifact_id)
    assert before is not None

    (root / "upstream.txt").write_text("from upstream\n", encoding="utf-8")
    await _git(root, "add", "upstream.txt")
    await _git(root, "commit", "-m", "upstream moves")

    added = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": _ADD_NOTES})
    )

    assert added.error == "", added.error
    assert "Rebased onto upstream" in added.output_text
    assert (worktree / "upstream.txt").read_text(encoding="utf-8") == "from upstream\n"
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello world\n"
    assert (worktree / "notes.md").exists()
    after = await store.get(artifact_id)
    assert after is not None
    assert after.base_sha != before.base_sha
    assert after.changed_paths == ["app.txt", "notes.md"]

    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "On new base"})
    )
    assert committed.error == "", committed.error


@pytest.mark.asyncio
async def test_conflicting_upstream_leaves_branch_on_previous_base(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id, worktree = await _patched(root, store, [_check("unit")])
    before = await store.get(artifact_id)
    assert before is not None

    (root / "app.txt").write_text("upstream wins\n", encoding="utf-8")
    await _git(root, "commit", "-am", "conflicting upstream")

    added = await apply_patch(
        _context(root, store, {"artifact_id": artifact_id, "patch": _ADD_NOTES})
    )

    assert added.error == "", added.error
    assert "conflicts with this work in app.txt" in added.output_text
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello world\n"
    assert (worktree / "notes.md").exists()
    after = await store.get(artifact_id)
    assert after is not None
    assert after.base_sha == before.base_sha
    assert after.changed_paths == ["app.txt", "notes.md"]


@pytest.mark.asyncio
async def test_create_worktree_branches_from_configured_base_branch(tmp_path: Path) -> None:
    root = await _repository(tmp_path)
    await _git(root, "branch", "-M", "main")
    await _git(root, "checkout", "-b", "feature")
    (root / "feature.txt").write_text("feature only\n", encoding="utf-8")
    await _git(root, "add", "feature.txt")
    await _git(root, "commit", "-m", "feature work")
    store = MemoryWorkProductStore()
    ctx = _context(root, store, {"summary": "from main"})
    ctx = ctx.model_copy(update={"pack_config": {**ctx.pack_config, "base_branch": "main"}})

    created = await create_worktree(ctx)

    assert created.error == "", created.error
    worktree = Path(str(created.output_json["worktree_path"]))
    assert not (worktree / "feature.txt").exists()
