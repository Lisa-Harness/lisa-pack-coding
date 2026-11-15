"""Harness-run verification (reuse, needs, fixers) and upstream freshness."""

from __future__ import annotations

import asyncio
import hashlib
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
from lisa_packs.coding.tools.edit_files.edit_files import execute as edit_files
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
    assert added.output_json["sync"]["upstream_paths"] == ["upstream.txt"]
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


@pytest.mark.parametrize("tool", ["verify", "commit"])
@pytest.mark.asyncio
async def test_failure_after_rebase_still_reports_the_sync(tmp_path: Path, tool: str) -> None:
    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    checks = [_check("unit")]
    artifact_id, _ = await _patched(root, store, checks)
    (root / "upstream.txt").write_text("from upstream\n", encoding="utf-8")
    await _git(root, "add", "upstream.txt")
    await _git(root, "commit", "-m", "upstream moves")

    def rewrite(worktree: Path) -> None:
        (worktree / "app.txt").write_text("rewritten\n", encoding="utf-8")

    params: dict[str, object] = {"artifact_id": artifact_id}
    if tool == "commit":
        params["message"] = "Change"
    result = await (verify if tool == "verify" else commit)(
        _context(root, store, params, checks=checks, sandbox_effect=rewrite)
    )

    assert "must be configured with kind: fix" in result.error
    assert result.error.startswith("Rebased onto upstream")
    sync = result.output_json["sync"] if tool == "verify" else result.output_json[
        "verification"
    ]["sync"]
    assert sync["status"] == "synced" and sync["upstream_paths"] == ["upstream.txt"]
    assert result.output_json["artifact_id"] == artifact_id


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
    # Nothing landed, so no working-tree read went stale.
    assert added.output_json["sync"]["upstream_paths"] == []
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


async def _verified(
    root: Path,
    store: MemoryWorkProductStore,
    artifact_id: str,
) -> ToolResult:
    report = await verify(
        _context(
            root,
            store,
            {"artifact_id": artifact_id},
            checks=[_check("unit")],
            required_check_ids=["unit"],
        )
    )
    assert report.error == "", report.error
    return report


async def _git_ls_tree(worktree: Path, commit_sha: str) -> set[str]:
    """Every path in the worktree's commit tree (recursive, NUL-separated)."""

    process = await asyncio.create_subprocess_exec(
        "git",
        "--literal-pathspecs",
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        commit_sha,
        cwd=str(worktree),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    return set(stdout.decode("utf-8").split("\0")) - {""}


@pytest.mark.asyncio
async def test_deleted_tracked_file_commits_and_verifies(tmp_path: Path) -> None:
    """A tracked file deleted from the worktree must not wedge the pipeline.

    Deleting a file the artifact tracks (or commits) used to fatal every later
    verify and commit with ``pathspec '<path>' did not match any files``; the
    tree hashing now stages the deletion instead. The recorded changed path
    also feeds the candidate tree, so committing carries the deletion.
    """

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id, worktree = await _patched(root, store, [_check("unit")])

    deleted = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "delete",
                        "path": "app.txt",
                        "expected_sha256": hashlib.sha256(b"hello world\n").hexdigest(),
                    }
                ],
            },
        )
    )
    assert deleted.error == "", deleted.error
    assert not (worktree / "app.txt").exists()

    report = await _verified(root, store, artifact_id)
    assert report.output_json["ready_to_commit"] is True

    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "Delete app.txt"})
    )
    assert committed.error == "", committed.error
    after = await store.get(artifact_id)
    assert after is not None
    # HEAD's tree no longer holds the deleted file.
    listing = await _git_ls_tree(worktree, after.commit_sha)
    assert "app.txt" not in listing
    assert deleted.output_json["changed_paths"] == ["app.txt"]


@pytest.mark.asyncio
async def test_deleted_tracked_file_keeps_checks_reusable(tmp_path: Path) -> None:
    """Verify-then-verify with no file changes between the two still reuses."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id, _worktree = await _patched(root, store, [_check("unit")])
    deleted = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "delete",
                        "path": "app.txt",
                        "expected_sha256": hashlib.sha256(b"hello world\n").hexdigest(),
                    }
                ],
            },
        )
    )
    assert deleted.error == "", deleted.error
    counter = _Counter()
    first = await verify(
        _context(
            root, store, {"artifact_id": artifact_id}, checks=[_check("unit")],
            required_check_ids=["unit"], sandbox_effect=counter,
        )
    )
    assert first.error == "", first.error
    second = await verify(
        _context(
            root, store, {"artifact_id": artifact_id}, checks=[_check("unit")],
            required_check_ids=["unit"], sandbox_effect=counter,
        )
    )
    assert second.error == "", second.error
    # Both runs hashed the tree without dying, and the unchanged tree let the
    # check record carry over.
    reused = [item["reused"] for item in second.output_json["checks"]]
    assert reused == [True]


@pytest.mark.asyncio
async def test_forgotten_recorded_path_dropped_from_commit(tmp_path: Path) -> None:
    """A junk path that vanished from disk cannot fatal the commit's staging.

    An artifact's recorded changed paths can name a file that never entered
    Git (a rejected batch's leftover) and was then deleted from the worktree;
    staging it used to fatal with ``pathspec did not match``. The commit now
    drops such paths and lands what remains.
    """

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id, worktree = await _patched(root, store, [_check("unit")])
    assert artifact_id
    junk = worktree / "junk inert.txt"
    junk.write_text("junk\n", encoding="utf-8")
    artifact = await store.get(artifact_id)
    assert artifact is not None
    # Record the junk path exactly like a stale bookkeeping entry would.
    artifact.changed_paths = list(dict.fromkeys([*artifact.changed_paths, "junk inert.txt"]))
    await store.save(artifact)
    junk.unlink()

    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "Real change"})
    )
    assert committed.error == "", committed.error
    after = await store.get(artifact_id)
    assert after is not None
    # HEAD carries the real change and not the vanished junk path.
    listing = await _git_ls_tree(worktree, after.commit_sha)
    assert "app.txt" in listing
    assert "junk inert.txt" not in listing


@pytest.mark.asyncio
async def test_commit_without_stageable_paths_is_refused(tmp_path: Path) -> None:
    """Only vanished paths remain: refuse with guidance instead of failing."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    artifact_id, worktree = await _patched(root, store, [_check("unit")])
    assert artifact_id
    junk = worktree / "junk only.txt"
    junk.write_text("junk\n", encoding="utf-8")
    artifact = await store.get(artifact_id)
    assert artifact is not None
    artifact.changed_paths = ["junk only.txt"]
    await store.save(artifact)
    junk.unlink()

    committed = await commit(
        _context(root, store, {"artifact_id": artifact_id, "message": "Nothing left"})
    )
    assert committed.error != ""
    assert "none of the artifact's recorded paths" in committed.error
    assert "pathspec" not in committed.error
