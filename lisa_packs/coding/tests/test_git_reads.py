"""Integration tests for bounded Git history, diff, and snapshot reads."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kernel.models import ToolExecutionContext

from lisa_packs.coding.tools.git_diff.git_diff import execute as git_diff
from lisa_packs.coding.tools.git_log.git_log import execute as git_log
from lisa_packs.coding.tools.read_file.read_file import execute as read_file


async def _git(root: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    return stdout.decode("utf-8").strip()


async def _repository(root: Path) -> dict[str, str]:
    root.mkdir()
    await _git(root, "init", "-b", "main")
    await _git(root, "config", "user.email", "test@example.invalid")
    await _git(root, "config", "user.name", "Coding Test")
    (root / "app.txt").write_text("base\n", encoding="utf-8")
    await _git(root, "add", "app.txt")
    await _git(root, "commit", "-m", "base commit")
    base = await _git(root, "rev-parse", "HEAD")

    await _git(root, "switch", "-c", "feature")
    (root / "app.txt").write_text("feature\n", encoding="utf-8")
    (root / "large.txt").write_text("x" * 2048 + "\n", encoding="utf-8")
    await _git(root, "add", "app.txt", "large.txt")
    await _git(root, "commit", "-m", "feature implementation")
    feature = await _git(root, "rev-parse", "HEAD")

    await _git(root, "switch", "main")
    (root / "app.txt").write_text("main\n", encoding="utf-8")
    await _git(root, "commit", "-am", "main update")
    main = await _git(root, "rev-parse", "HEAD")
    return {"base": base, "feature": feature, "main": main}


def _context(
    root: Path, params: dict[str, object], *, patch_bytes: int = 1024
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="test",
        mode_id="coding_review",
        parameters=params,
        pack_config={
            "workspace_root": str(root),
            "max_patch_bytes": patch_bytes,
            "max_file_bytes": 4096,
        },
    )


@pytest.mark.asyncio
async def test_git_diff_branch_and_commit_comparisons_return_resolved_changes(
    tmp_path: Path,
) -> None:
    """Branch mode compares from merge-base; commit mode compares first parent."""

    root = tmp_path / "repo"
    revisions = await _repository(root)

    branch = await git_diff(
        _context(
            root,
            {
                "comparison": "branch",
                "base": "main",
                "head": "feature",
                "include_patches": True,
            },
            patch_bytes=4096,
        )
    )
    assert branch.error == ""
    assert branch.output_json["merge_base_sha"] == revisions["base"]
    assert branch.output_json["base_sha"] == revisions["main"]
    assert branch.output_json["diff_base_sha"] == revisions["base"]
    assert {change["path"] for change in branch.output_json["changes"]} == {
        "app.txt",
        "large.txt",
    }
    assert all(patch["complete"] for patch in branch.output_json["patches"])

    commit = await git_diff(
        _context(root, {"comparison": "commit", "revision": revisions["feature"]})
    )
    assert commit.error == ""
    assert commit.output_json["base_sha"] == revisions["base"]
    assert commit.output_json["head_sha"] == revisions["feature"]


@pytest.mark.asyncio
async def test_git_diff_exact_trees_and_patch_budget_never_return_partial_patch(
    tmp_path: Path,
) -> None:
    """Exact trees are distinct from merge-base comparisons and oversized patches are omitted."""

    root = tmp_path / "repo"
    revisions = await _repository(root)
    diff = await git_diff(
        _context(
            root,
            {
                "comparison": "trees",
                "base": revisions["main"],
                "head": revisions["feature"],
                "include_patches": True,
            },
        )
    )

    assert diff.error == ""
    assert diff.output_json["merge_base_sha"] is None
    assert diff.output_json["base_sha"] == revisions["main"]
    assert diff.output_json["diff_base_sha"] == revisions["main"]
    large_patch = next(
        patch for patch in diff.output_json["patches"] if patch["path"] == "large.txt"
    )
    assert large_patch["complete"] is False
    assert large_patch["diff"] == ""


@pytest.mark.asyncio
async def test_git_log_ranges_and_historical_file_reads_use_immutable_revisions(
    tmp_path: Path,
) -> None:
    """History and source excerpts can be anchored to the same resolved commit."""

    root = tmp_path / "repo"
    revisions = await _repository(root)
    history = await git_log(
        _context(root, {"base": "main", "head": "feature", "paths": ["app.txt"]})
    )
    assert history.error == ""
    assert [commit["subject"] for commit in history.output_json["commits"]] == [
        "feature implementation"
    ]

    historical = await read_file(
        _context(root, {"path": "app.txt", "revision": revisions["base"]})
    )
    assert historical.error == ""
    assert historical.output_json["excerpt"] == "1\tbase"
    assert historical.output_json["revision"] == revisions["base"]
    assert historical.output_json["content_sha256"] is None
    assert historical.output_json["editable"] is False


@pytest.mark.asyncio
async def test_git_read_tools_report_invalid_revisions(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    await _repository(root)

    result = await git_diff(
        _context(root, {"comparison": "commit", "revision": "--help"})
    )

    assert result.error
    assert result.output_json == {}


@pytest.mark.asyncio
async def test_working_tree_comparisons_separate_staged_unstaged_and_untracked(
    tmp_path: Path,
) -> None:
    """Stage-aware diffs keep untracked paths separate from tracked patches."""

    root = tmp_path / "repo"
    await _repository(root)
    (root / "app.txt").write_text("staged\n", encoding="utf-8")
    (root / "staged-only.txt").write_text("staged file\n", encoding="utf-8")
    await _git(root, "add", "app.txt", "staged-only.txt")
    (root / "app.txt").write_text("working tree\n", encoding="utf-8")
    (root / "untracked-a.txt").write_text("not staged\n", encoding="utf-8")
    (root / "untracked-b.txt").write_text("also not staged\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "ignored-by-coding-tools.txt").write_text(
        "generated\n", encoding="utf-8"
    )

    staged = await git_diff(_context(root, {"comparison": "staged"}))
    assert staged.error == ""
    assert staged.output_json["from_endpoint"] == "HEAD"
    assert staged.output_json["to_endpoint"] == "index"
    assert {change["path"] for change in staged.output_json["changes"]} == {
        "app.txt",
        "staged-only.txt",
    }

    unstaged = await git_diff(_context(root, {"comparison": "unstaged"}))
    assert unstaged.error == ""
    assert unstaged.output_json["from_endpoint"] == "index"
    assert unstaged.output_json["to_endpoint"] == "worktree"
    assert [change["path"] for change in unstaged.output_json["changes"]] == ["app.txt"]

    combined = await git_diff(
        _context(
            root,
            {"comparison": "working_tree", "include_patches": True, "limit": 1},
            patch_bytes=4096,
        )
    )
    assert combined.error == ""
    assert combined.output_json["from_endpoint"] == "HEAD"
    assert combined.output_json["to_endpoint"] == "worktree"
    assert {change["path"] for change in combined.output_json["changes"]} == {"app.txt"}
    assert combined.output_json["total_changes"] == 2
    assert all(patch["complete"] for patch in combined.output_json["patches"])
    assert combined.output_json["untracked_files"] == ["untracked-a.txt"]
    assert combined.output_json["untracked_files_observed"] == 2
    assert combined.output_json["untracked_next_offset"] == 1
    assert combined.output_json["untracked_list_truncated"] is False

    next_page = await git_diff(
        _context(root, {"comparison": "working_tree", "offset": 1, "limit": 1})
    )
    assert next_page.error == ""
    assert next_page.output_json["untracked_files"] == ["untracked-b.txt"]
