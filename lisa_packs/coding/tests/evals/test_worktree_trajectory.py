"""Worktree-trajectory eval: the full coding pack write chain, in-process.

Drives ``create_worktree → status → read_file → apply_patch → run_check →
commit → push → status → cleanup_worktree`` against a throwaway Git repo and
bare remote. Asserts the artifact state machine transitions
(``created → patched → committed → pushed → cleaned``) and per-tool result
fields. Deterministic: the only I/O is Git inside tmp dirs; no model calls.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from kernel.capability_binding import ExecutionFloorView
from kernel.models import ToolResult
from packstest.catalog import build_tool_catalog
from packstest.context import ToolContextFactory
from packstest.git import GitRepoFixture, git_repo
from packstest.report import Trajectory, write_trajectory
from packstest.runner import ToolRun, run_tool

from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.workflow import run_git

PACK_PATH = Path(__file__).resolve().parents[4]
SHA1_HEX_LENGTH = 40

PATCH = """--- a/app.txt
+++ b/app.txt
@@ -1 +1 @@
-hello
+hello from lisa
"""


def _config(root: Path) -> dict[str, object]:
    return {
        "workspace_root": str(root),
        "checks": [{"id": "smoke", "argv": ["true"], "timeout_seconds": 60}],
        "required_check_ids": [],
    }


def _verified_sandbox(factory: ToolContextFactory) -> None:
    factory.execution_floor_for = lambda _tool_id: ExecutionFloorView(
        execution="sandbox",
        persistence="direct-write",
        paths={str(factory.root.parent): "rw"},
    )

    async def dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        assert tool_id == "bwrap_run"
        assert parameters["argv"] == ["true"]
        worktree = str(parameters["cwd"])
        assert parameters["overrides"] == {
            "persistence": "direct-write",
            "paths": {worktree: "rw"},
        }
        return ToolResult(
            output_text="ok",
            output_json={
                "sandboxed": True,
                "return_code": 0,
                "persistence": "direct-write",
                "cwd": worktree,
                "writable_paths": [worktree],
            },
        )

    factory.delegated_tool_dispatch = dispatch


@pytest.fixture()
def catalog() -> dict[str, dict[str, object]]:
    from packstest.catalog import _ensure_source_importable

    _ensure_source_importable(PACK_PATH)
    return build_tool_catalog(PACK_PATH)


async def _step(
    catalog: dict[str, dict[str, object]],
    factory: ToolContextFactory,
    trajectory: Trajectory,
    tool_id: str,
    params: dict[str, object],
) -> ToolRun:
    run = await run_tool(catalog, tool_id, factory, params)
    trajectory.record(run)
    return run


def _real_sandbox(
    factory: ToolContextFactory, *, bwrap_path: Path, read_only_paths: tuple[Path, ...] = ()
) -> ToolContextFactory:
    """Dispatch contained commands through the real bwrap pack, without mocks."""

    paths = {
        str(factory.root): "ro",
        str(factory.data_dir or factory.root / ".packstest-data" / "git-worktrees"): "rw",
        **{str(path): "ro" for path in read_only_paths},
    }
    floor = {
        "execution": "sandbox",
        "persistence": "direct-write",
        "paths": paths,
    }
    factory.execution_floor_for = lambda _tool_id: ExecutionFloorView(
        execution="sandbox",
        persistence="direct-write",
        paths=paths,
    )
    bwrap_catalog = build_tool_catalog(bwrap_path)
    bwrap_factory = ToolContextFactory(
        root=factory.root,
        pack_config={"workspace_root": str(factory.root), "sandbox": {"floor": floor}},
        identity=factory.identity,
        data_dir=factory.data_dir,
    )

    async def dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        return (await run_tool(bwrap_catalog, tool_id, bwrap_factory, dict(parameters))).result

    factory.delegated_tool_dispatch = dispatch
    return bwrap_factory


async def _greeting_repo(tmp_path: Path) -> tuple[GitRepoFixture, str]:
    """Seed the offline Go fixture without network dependencies."""

    repo = await git_repo(tmp_path)
    await repo.commit_file(".gitignore", ".lisa-go-cache/\n.lisa-go-path/\n", "ignore Go runtime")
    await repo.commit_file("go.mod", "module example.invalid/greeting\n\ngo 1.23.0\n", "Go module")
    original = 'package greeting\n\nfunc Greeting() string { return "hello" }\n'
    test_source = (
        'package greeting\n\nimport "testing"\n\n'
        "func TestGreeting(t *testing.T) {\n"
        '    if got := Greeting(); got != "hello" { t.Fatalf("unexpected greeting: %s", got) }\n'
        "}\n"
    )
    await repo.commit_file("greeting.go", original, "initial greeting")
    await repo.commit_file("greeting_test.go", test_source, "initial check")
    return repo, original


@pytest.mark.eval
@pytest.mark.asyncio
async def test_real_go_worktree_repair_trajectory(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """Real Go failures block commit until a structured repair passes in bwrap."""

    bwrap_path = PACK_PATH.parent.parent / "lisa-packs" / "bwrap"
    if not bwrap_path.is_dir() or shutil.which("bwrap") is None or shutil.which("go") is None:
        pytest.skip("This offline integration requires the bwrap checkout, bwrap, and Go.")
    repo, original = await _greeting_repo(tmp_path)
    source_head = await repo.head()
    config = {
        "workspace_root": str(repo.root),
        "checks": [{"id": "go-test", "argv": ["go", "test", "./..."], "timeout_seconds": 120}],
        "required_check_ids": ["go-test"],
    }
    factory = ToolContextFactory(root=repo.root, pack_config=config, data_dir=tmp_path / "data")
    factory.identity["task_id"] = "go-repair-task"
    _real_sandbox(factory, bwrap_path=bwrap_path)
    trajectory = Trajectory(name="real-go-repair")
    created = await _step(
        catalog, factory, trajectory, "coding_create_worktree", {"summary": "Go repair"}
    )
    assert created.result.error == "", created.result.error
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))
    factory.pack_config["checks"] = [
        {
            "id": "go-test",
            "argv": [
                "env",
                f"GOCACHE={worktree / '.lisa-go-cache'}",
                f"GOPATH={worktree / '.lisa-go-path'}",
                "GOTOOLCHAIN=local",
                "GOPROXY=off",
                "GOSUMDB=off",
                "go",
                "test",
                "-count=1",
                "./...",
            ],
            "timeout_seconds": 120,
        }
    ]
    baseline = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {"artifact_id": artifact_id, "check_id": "go-test"},
    )
    assert baseline.result.error == "", baseline.result.error
    assert baseline.result.output_json["status"] == "passed"
    source_read = await _step(
        catalog, factory, trajectory, "coding_read_file", {"path": "greeting.go"}
    )
    test_read = await _step(
        catalog, factory, trajectory, "coding_read_file", {"path": "greeting_test.go"}
    )
    assert source_read.result.output_json["scope"] == "worktree"
    edited = await _step(
        catalog,
        factory,
        trajectory,
        "coding_edit_files",
        {
            "artifact_id": artifact_id,
            "edits": [
                {
                    "kind": "update",
                    "path": "greeting.go",
                    "expected_sha256": source_read.result.output_json["content_sha256"],
                    "replacements": [{"old_text": 'return "hello"', "new_text": 'return "broken"'}],
                },
                {
                    "kind": "update",
                    "path": "greeting_test.go",
                    "expected_sha256": test_read.result.output_json["content_sha256"],
                    "replacements": [
                        {"old_text": 'got != "hello"', "new_text": 'got != "hello from lisa"'}
                    ],
                },
            ],
        },
    )
    assert edited.result.error == "", edited.result.error
    failed = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {"artifact_id": artifact_id, "check_id": "go-test"},
    )
    assert failed.result.error == "", failed.result.error
    assert failed.result.output_json["status"] == "failed"
    blocked = await _step(
        catalog,
        factory,
        trajectory,
        "coding_commit",
        {
            "artifact_id": artifact_id,
            "message": "must not commit failing code",
        },
    )
    assert "Every required check must pass" in blocked.result.error
    evidence = await _step(
        catalog, factory, trajectory, "coding_status", {"artifact_id": artifact_id}
    )
    assert "unexpected greeting: broken" in str(evidence.result.output_json["checks"])
    fresh = await _step(catalog, factory, trajectory, "coding_read_file", {"path": "greeting.go"})
    assert (
        fresh.result.output_json["content_sha256"]
        != source_read.result.output_json["content_sha256"]
    )
    repaired = await _step(
        catalog,
        factory,
        trajectory,
        "coding_edit_files",
        {
            "artifact_id": artifact_id,
            "edits": [
                {
                    "kind": "update",
                    "path": "greeting.go",
                    "expected_sha256": fresh.result.output_json["content_sha256"],
                    "replacements": [
                        {"old_text": 'return "broken"', "new_text": 'return "hello from lisa"'}
                    ],
                }
            ],
        },
    )
    assert repaired.result.error == "", repaired.result.error
    passed = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {"artifact_id": artifact_id, "check_id": "go-test"},
    )
    assert passed.result.error == "", passed.result.error
    assert passed.result.output_json["status"] == "passed"
    committed = await _step(
        catalog,
        factory,
        trajectory,
        "coding_commit",
        {
            "artifact_id": artifact_id,
            "message": "fix: greet from the owned Go worktree",
        },
    )
    assert committed.result.error == "", committed.result.error
    fact = await _step(
        catalog,
        factory,
        trajectory,
        "coding_record_fact",
        {
            "artifact_id": artifact_id,
            "claim": "The repaired greeting passes its Go test.",
            "at": "greeting.go:3",
            "revision": committed.result.output_json["commit_sha"],
        },
    )
    assert fact.result.error == "", fact.result.error
    assert fact.result.output_json["fact"]["revision"] == committed.result.output_json["commit_sha"]
    published = await _step(
        catalog, factory, trajectory, "coding_push", {"artifact_id": artifact_id}
    )
    assert published.result.error == "", published.result.error
    assert published.result.output_json["pushed_sha"] == committed.result.output_json["commit_sha"]
    cleaned = await _step(
        catalog, factory, trajectory, "coding_cleanup_worktree", {"artifact_id": artifact_id}
    )
    assert cleaned.result.error == "", cleaned.result.error
    assert not worktree.exists()
    assert await repo.head() == source_head
    assert (repo.root / "greeting.go").read_text(encoding="utf-8") == original
    assert [entry["result_error"] for entry in trajectory.entries if entry["result_error"]] == [
        blocked.result.error
    ]


async def _python_preparation_fixture(
    tmp_path: Path, unignored_path: str | None
) -> ToolContextFactory:
    """Seed an offline Python fixture with real sandbox dispatch."""

    bwrap_path = PACK_PATH.parent.parent / "lisa-packs" / "bwrap"
    if not bwrap_path.is_dir() or shutil.which("bwrap") is None or shutil.which("uv") is None:
        pytest.skip("This offline integration requires the bwrap checkout, bwrap, and uv.")
    repo = await git_repo(tmp_path, with_remote=False)
    python_range = (
        f">={sys.version_info.major}.{sys.version_info.minor}, "
        f"<{sys.version_info.major}.{sys.version_info.minor + 1}"
    )
    files = {
        ".gitignore": "".join(
            path + "\n"
            for path in (".venv/", ".lisa-tmp/", "__pycache__/")
            if path != unignored_path
        ),
        ".python-version": sys.version.split()[0] + "\n",
        "pyproject.toml": (
            '[project]\nname = "prepared-fixture"\nversion = "0.1.0"\n'
            f'requires-python = "{python_range}"\n'
            '[tool.uv]\npackage = false\noffline = true\npython-downloads = "never"\n'
        ),
        "uv.lock": (
            "version = 1\nrevision = 3\n"
            f'requires-python = "{python_range}"\n'
            '[[package]]\nname = "prepared-fixture"\nversion = "0.1.0"\n'
            'source = { virtual = "." }\n'
        ),
        "app.py": 'def greeting() -> str:\n    return "hello"\n',
        "test_app.py": (
            "import unittest\nfrom app import greeting\n\n"
            "class GreetingTest(unittest.TestCase):\n"
            "    def test_greeting(self):\n"
            '        self.assertEqual(greeting(), "hello")\n'
        ),
    }
    for path, text in files.items():
        await repo.commit_file(path, text, f"seed {path}")
    factory = ToolContextFactory(
        root=repo.root,
        data_dir=tmp_path / "data",
        pack_config={
            "workspace_root": str(repo.root),
            "checks": [
                {
                    "id": "unit",
                    "argv": ["{workspace_python}", "-m", "unittest", "-v"],
                    "timeout_seconds": 60,
                }
            ],
            "required_check_ids": ["unit"],
            "preparation_recipes": [
                {
                    "id": "python-uv",
                    "project_modules": ["app"],
                    "baseline_check_ids": ["unit"],
                    "timeout_seconds": 60,
                }
            ],
        },
    )
    factory.identity["task_id"] = "python-preparation-task"
    _real_sandbox(factory, bwrap_path=bwrap_path, read_only_paths=(Path(sys.base_prefix),))
    return factory


async def _assert_python_preparation(
    factory: ToolContextFactory, artifact_id: str, worktree: Path
) -> None:
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.preparation is not None
    preparation = artifact.preparation
    assert preparation.status == "ready"
    assert preparation.recipe_digest and preparation.dependency_manifest_digest
    assert preparation.python_path == str(worktree / ".venv" / "bin" / "python")
    assert json.loads(preparation.probe_stdout)["origins"] == {"app": str(worktree / "app.py")}
    assert preparation.baseline_checks["unit"].status == "passed"
    assert preparation.baseline_checks["unit"].checked_tree_sha == preparation.baseline_tree_sha
    assert artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
@pytest.mark.parametrize("unignored_path", [None, ".venv/", ".lisa-tmp/"])
async def test_real_python_preparation_and_repair_trajectory(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
    unignored_path: str | None,
) -> None:
    """Actual uv preparation certifies origins before sandboxed repair and commit."""

    factory = await _python_preparation_fixture(tmp_path, unignored_path)
    source_snapshot = await _source_snapshot(factory)
    trajectory = Trajectory(name="real-python-preparation-repair")
    created = await _step(
        catalog, factory, trajectory, "coding_create_worktree", {"summary": "Python repair"}
    )
    assert created.result.error == "", created.result.error
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))
    unprepared = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {"artifact_id": artifact_id, "check_id": "unit"},
    )
    assert "Workspace preparation is missing or stale" in unprepared.result.error
    prepared = await _step(
        catalog, factory, trajectory, "coding_prepare_workspace", {"artifact_id": artifact_id}
    )
    if unignored_path is not None:
        assert f"{unignored_path.rstrip('/')} must be ignored" in prepared.result.error
        rejected = await factory.artifact_store.get(artifact_id)
        assert rejected is not None and rejected.preparation is not None
        assert rejected.preparation.status == "failed"
        assert not (worktree / ".venv").exists()
        assert not (worktree / ".lisa-tmp").exists()
        cleaned = await _step(
            catalog, factory, trajectory, "coding_cleanup_worktree", {"artifact_id": artifact_id}
        )
        assert cleaned.result.error == "", cleaned.result.error
        assert await _source_snapshot(factory) == source_snapshot
        return
    assert prepared.result.error == "", prepared.result.error
    await _assert_python_preparation(factory, artifact_id, worktree)
    for path, old_text, new_text in (
        ("app.py", 'return "hello"', 'return "broken"'),
        ("test_app.py", 'greeting(), "hello"', 'greeting(), "hello from lisa"'),
    ):
        read = await _step(catalog, factory, trajectory, "coding_read_file", {"path": path})
        assert read.result.error == "", read.result.error
        assert read.result.output_json["scope"] == "worktree"
        edit = await _step(
            catalog,
            factory,
            trajectory,
            "coding_edit_files",
            {
                "artifact_id": artifact_id,
                "edits": [
                    {
                        "kind": "update",
                        "path": path,
                        "expected_sha256": read.result.output_json["content_sha256"],
                        "replacements": [{"old_text": old_text, "new_text": new_text}],
                    }
                ],
            },
        )
        assert edit.result.error == "", edit.result.error
    failed = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {"artifact_id": artifact_id, "check_id": "unit"},
    )
    assert failed.result.error == "", failed.result.error
    assert failed.result.output_json["status"] == "failed"
    blocked = await _step(
        catalog,
        factory,
        trajectory,
        "coding_commit",
        {"artifact_id": artifact_id, "message": "must not commit failing Python"},
    )
    assert "Every required check must pass" in blocked.result.error
    fresh = await _step(catalog, factory, trajectory, "coding_read_file", {"path": "app.py"})
    assert fresh.result.error == "", fresh.result.error
    repaired = await _step(
        catalog,
        factory,
        trajectory,
        "coding_edit_files",
        {
            "artifact_id": artifact_id,
            "edits": [
                {
                    "kind": "update",
                    "path": "app.py",
                    "expected_sha256": fresh.result.output_json["content_sha256"],
                    "replacements": [
                        {"old_text": 'return "broken"', "new_text": 'return "hello from lisa"'}
                    ],
                }
            ],
        },
    )
    assert repaired.result.error == "", repaired.result.error
    checked = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {"artifact_id": artifact_id, "check_id": "unit"},
    )
    assert checked.result.error == "", checked.result.error
    assert checked.result.output_json["status"] == "passed"
    committed = await _step(
        catalog,
        factory,
        trajectory,
        "coding_commit",
        {"artifact_id": artifact_id, "message": "fix: greet from the prepared Python worktree"},
    )
    assert committed.result.error == "", committed.result.error
    assert len(str(committed.result.output_json["commit_sha"])) == SHA1_HEX_LENGTH
    cleaned = await _step(
        catalog, factory, trajectory, "coding_cleanup_worktree", {"artifact_id": artifact_id}
    )
    assert cleaned.result.error == "", cleaned.result.error
    assert not worktree.exists()
    assert await _source_snapshot(factory) == source_snapshot
    assert [entry["result_error"] for entry in trajectory.entries if entry["result_error"]] == [
        unprepared.result.error,
        blocked.result.error,
    ]
    write_trajectory(trajectory, tmp_path / "trajectory-python-preparation-repair.json")


async def _source_snapshot(factory: ToolContextFactory) -> tuple[str, ...]:
    """Capture source HEAD, tracked content, and status without changing its index."""

    config = get_coding_config(factory.context())
    snapshot = []
    for argv in (("rev-parse", "HEAD"), ("diff", "HEAD", "--binary"), ("status", "--porcelain")):
        result = await run_git(factory.root, argv, config=config)
        assert result.return_code == 0 and not result.output_truncated
        snapshot.append(result.stdout)
    return tuple(snapshot)


async def _prepare_go_checks(
    bwrap_catalog: dict[str, dict[str, object]],
    sandbox_factory: ToolContextFactory,
    factory: ToolContextFactory,
    trajectory: Trajectory,
    worktree: Path,
    *,
    module_cache: Path,
    targets: list[str],
) -> dict[str, object]:
    """Prepare only ignored writable runtime paths and finite offline Go checks."""

    runtime_root = worktree / ".toolchains" / "packstest"
    overrides: dict[str, object] = {"persistence": "direct-write", "paths": {str(worktree): "rw"}}
    setup = await _step(
        bwrap_catalog,
        sandbox_factory,
        trajectory,
        "bwrap_run",
        {
            "argv": ["mkdir", "-p", str(runtime_root / "tmp")],
            "cwd": str(worktree),
            "overrides": overrides,
            "timeout_seconds": 30,
        },
    )
    assert setup.result.error == "", setup.result.error
    assert setup.result.output_json["return_code"] == 0
    environment = [
        "env",
        f"GOCACHE={runtime_root / 'go-build'}",
        f"GOPATH={runtime_root / 'go'}",
        f"GOMODCACHE={module_cache}",
        f"TMPDIR={runtime_root / 'tmp'}",
        "GOTOOLCHAIN=local",
        "GOPROXY=off",
        "GOSUMDB=off",
        "CGO_ENABLED=1",
    ]
    factory.pack_config["checks"] = [
        {"id": "gofmt", "argv": ["gofmt", "-l", *targets], "timeout_seconds": 30},
        {"id": "go-vet", "argv": [*environment, "go", "vet", "./..."], "timeout_seconds": 300},
        {
            "id": "go-test",
            "argv": [*environment, "go", "test", "-count=1", "./..."],
            "timeout_seconds": 300,
        },
    ]
    return overrides


@pytest.mark.eval
@pytest.mark.asyncio
async def test_dnsmasq_web_hygiene_workflow(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """Opt in to the remembered Go hygiene task in a fresh real-project worktree."""

    project = os.environ.get("LISA_CODING_GO_PROJECT")
    if not project:
        pytest.skip("Set LISA_CODING_GO_PROJECT to opt into the real dnsmasq-web task.")
    root = Path(project).expanduser().resolve()
    module_cache = Path(
        os.environ.get("LISA_CODING_GO_MODULE_CACHE", str(Path.home() / "go/pkg/mod"))
    )
    bwrap_path = PACK_PATH.parent.parent / "lisa-packs" / "bwrap"
    factory = ToolContextFactory(
        root=root,
        pack_config={
            "workspace_root": str(root),
            "checks": [{"id": "go-test", "argv": ["go", "test", "./..."], "timeout_seconds": 300}],
            "required_check_ids": ["go-test"],
        },
        data_dir=tmp_path / "data",
    )
    factory.identity["task_id"] = "dnsmasq-web-hygiene"
    before = await _source_snapshot(factory)
    sandbox_factory = _real_sandbox(factory, bwrap_path=bwrap_path, read_only_paths=(module_cache,))
    bwrap_catalog = build_tool_catalog(bwrap_path)
    trajectory = Trajectory(name="dnsmasq-web-hygiene")
    created = await _step(
        catalog,
        factory,
        trajectory,
        "coding_create_worktree",
        {"summary": "Go hygiene task from Lisa's retained thread"},
    )
    assert created.result.error == "", created.result.error
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))
    targets = ["internal/client/http.go", "token_gin.go", "token_test.go"]
    overrides = await _prepare_go_checks(
        bwrap_catalog,
        sandbox_factory,
        factory,
        trajectory,
        worktree,
        module_cache=module_cache,
        targets=targets,
    )
    baseline = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {
            "artifact_id": artifact_id,
            "check_id": "go-test",
        },
    )
    assert baseline.result.error == "", baseline.result.error
    assert baseline.result.output_json["status"] == "passed", baseline.result.output_json
    replacements = {
        "internal/client/http.go": [
            {"old_text": '"net/http"\n', "new_text": '"net/http"\n\t"time"\n'},
            {
                "old_text": "SetTimeout(30 * 1e9). // 30 seconds",
                "new_text": "SetTimeout(30 * time.Second).",
            },
            {"old_text": "SetRetryWaitTime(1 * 1e9)", "new_text": "SetRetryWaitTime(time.Second)"},
            {
                "old_text": "body interface{}, result interface{}",
                "new_text": "body any, result any",
            },
            {
                "old_text": "var errResp map[string]interface{}",
                "new_text": "var errResp map[string]any",
            },
            {"old_text": "req := map[string]interface{}{", "new_text": "req := map[string]any{"},
            {"old_text": "update map[string]interface{}", "new_text": "update map[string]any"},
        ],
        "token_gin.go": [
            {
                "old_text": (
                    "r.GET(path, func(c *gin.Context) { c.String(http.StatusOK, ttc.Get()) })"
                ),
                "new_text": (
                    "r.GET(path, func(c *gin.Context) {\n"
                    "\t\tc.String(http.StatusOK, ttc.Get())\n\t})"
                ),
            }
        ],
        "token_test.go": [
            {
                "old_text": (
                    "func TestTokenChecker_GetAfterTimeout(t *testing.T) {\n"
                    "\tttc := NewTokenChecker(1, 2, time.Millisecond*2)"
                ),
                "new_text": (
                    "func TestTokenChecker_GetAfterTimeout(t *testing.T) {\n"
                    "\tttc := NewTokenChecker(1, 2, 2*time.Millisecond)"
                ),
            },
            {
                "old_text": (
                    "func TestTokenChecker_CheckAfterTimeout(t *testing.T) {\n"
                    "\tttc := NewTokenChecker(1, 2, time.Millisecond*2)"
                ),
                "new_text": (
                    "func TestTokenChecker_CheckAfterTimeout(t *testing.T) {\n"
                    "\tttc := NewTokenChecker(1, 2, 2*time.Millisecond)"
                ),
            },
            {
                "old_text": "time.Sleep(time.Millisecond * 3)\n\ttoken3 := ttc.Get()",
                "new_text": "time.Sleep(3 * time.Millisecond)\n\ttoken3 := ttc.Get()",
            },
            {
                "old_text": "token1 := ttc.Get()\n\ttime.Sleep(time.Millisecond * 3)",
                "new_text": "token1 := ttc.Get()\n\ttime.Sleep(3 * time.Millisecond)",
            },
            {
                "old_text": "}\nfunc TestTokenChecker_GetTooManyTokens",
                "new_text": "}\n\nfunc TestTokenChecker_GetTooManyTokens",
            },
        ],
    }
    edits = []
    for path in targets:
        read = await _step(catalog, factory, trajectory, "coding_read_file", {"path": path})
        assert read.result.error == "", read.result.error
        assert read.result.output_json["scope"] == "worktree"
        edits.append(
            {
                "kind": "update",
                "path": path,
                "expected_sha256": read.result.output_json["content_sha256"],
                "replacements": replacements[path],
            }
        )
    edited = await _step(
        catalog,
        factory,
        trajectory,
        "coding_edit_files",
        {
            "artifact_id": artifact_id,
            "edits": edits,
        },
    )
    assert edited.result.error == "", edited.result.error
    formatted = await _step(
        bwrap_catalog,
        sandbox_factory,
        trajectory,
        "bwrap_run",
        {
            "argv": ["gofmt", "-w", *targets],
            "cwd": str(worktree),
            "overrides": overrides,
            "timeout_seconds": 30,
        },
    )
    assert formatted.result.error == "", formatted.result.error
    assert formatted.result.output_json["return_code"] == 0
    for check_id in ("gofmt", "go-vet", "go-test"):
        checked = await _step(
            catalog,
            factory,
            trajectory,
            "coding_run_check",
            {
                "artifact_id": artifact_id,
                "check_id": check_id,
            },
        )
        assert checked.result.error == "", checked.result.error
        assert checked.result.output_json["status"] == "passed", checked.result.output_json
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None
    assert artifact.checks["gofmt"].stdout.strip() == "(no output; rc=0)"
    status = await _step(
        catalog, factory, trajectory, "coding_status", {"artifact_id": artifact_id}
    )
    assert status.result.error == "", status.result.error
    assert set(artifact.changed_paths) == set(targets)
    assert await _source_snapshot(factory) == before
    assert trajectory.all_errors_absent(), trajectory.first_error()
    report = write_trajectory(trajectory, tmp_path)
    print(
        f"Checked worktree: {worktree}\nBranch: {artifact.branch}\n"
        f"Trajectory: {report}\nNo commit or push."
    )


@pytest.mark.eval
@pytest.mark.asyncio
async def test_rejected_patch_can_be_corrected_without_mutating_first(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "format test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))
    rejected = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {
            "artifact_id": artifact_id,
            "patch": "*** Begin Patch\n*** Update File: app.txt\n@@\n-hello\n+fixed\n*** End Patch",
        },
    )
    assert "unified diff file path" in rejected.result.error
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.revision == 0
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello\n"
    corrected = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": PATCH}
    )
    assert corrected.result.error == ""
    assert corrected.result.output_json["revision"] == 1
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello from lisa\n"
    assert "Do not use *** Begin Patch" in catalog["coding_apply_patch"]["description"]


@pytest.mark.eval
@pytest.mark.asyncio
async def test_full_worktree_trajectory(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """The create→patch→check→commit→push chain reaches ``pushed`` cleanly."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    factory.identity["task_id"] = "coding-task"
    _verified_sandbox(factory)
    trajectory = Trajectory(name="worktree-full-chain")
    artifact_id = ""

    run = await _step(catalog, factory, trajectory, "coding_create_worktree", {"summary": "eval"})
    assert run.result.error == "", run.result.error
    artifact_id = str(run.result.output_json["artifact_id"])
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.task_id == "coding-task"
    branch = str(run.result.output_json["branch"])
    worktree = str(run.result.output_json["worktree_path"])
    assert branch.startswith("lisa/")
    assert Path(worktree).is_dir()

    run = await _step(catalog, factory, trajectory, "coding_status", {"artifact_id": artifact_id})
    assert run.result.error == ""
    assert run.result.output_json["status"] == "created"

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_read_file",
        {"artifact_id": artifact_id, "path": "app.txt"},
    )
    assert run.result.error == ""
    assert run.result.output_json["excerpt"] == "1\thello"

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_apply_patch",
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert run.result.error == ""
    assert run.result.output_json["revision"] == 1
    assert run.result.output_json["changed_paths"] == ["app.txt"]

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_run_check",
        {"artifact_id": artifact_id, "check_id": "smoke"},
    )
    assert run.result.error == ""
    assert run.result.output_json["status"] == "passed"

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_commit",
        {"artifact_id": artifact_id, "message": "eval: apply patch"},
    )
    assert run.result.error == ""
    commit_sha = str(run.result.output_json["commit_sha"])
    assert len(commit_sha) == SHA1_HEX_LENGTH

    run = await _step(catalog, factory, trajectory, "coding_push", {"artifact_id": artifact_id})
    assert run.result.error == ""
    assert run.result.output_json["pushed_sha"] == commit_sha

    run = await _step(catalog, factory, trajectory, "coding_status", {"artifact_id": artifact_id})
    assert run.result.error == ""
    final = run.result.output_json
    assert final["status"] == "pushed"
    assert final["checks"]["smoke"]["status"] == "passed"

    assert trajectory.all_errors_absent(), trajectory.first_error()
    assert trajectory.tool_ids == [
        "coding_create_worktree",
        "coding_status",
        "coding_read_file",
        "coding_apply_patch",
        "coding_run_check",
        "coding_commit",
        "coding_push",
        "coding_status",
    ]


@pytest.mark.eval
@pytest.mark.asyncio
async def test_required_check_rejects_changed_candidate_tree(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    config = {**_config(repo.root), "required_check_ids": ["smoke"]}
    factory = ToolContextFactory(root=repo.root, pack_config=config, data_dir=tmp_path / "data")
    _verified_sandbox(factory)
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))
    patched = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert patched.result.error == ""
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    assert checked.result.output_json["status"] == "passed"

    (worktree / "app.txt").write_text("changed after check\n", encoding="utf-8")
    committed = await run_tool(
        catalog,
        "coding_commit",
        factory,
        {"artifact_id": artifact_id, "message": "unchecked file"},
    )
    assert "checked tree" in committed.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.commit_sha == ""


@pytest.mark.eval
@pytest.mark.asyncio
async def test_cleanup_worktree_transitions_to_cleaned(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """Cleanup removes the worktree and the artifact records ``cleaned``."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    trajectory = Trajectory(name="cleanup")

    run = await _step(catalog, factory, trajectory, "coding_create_worktree", {"summary": "eval"})
    artifact_id = str(run.result.output_json["artifact_id"])
    worktree = Path(str(run.result.output_json["worktree_path"]))
    assert worktree.is_dir()

    run = await _step(
        catalog,
        factory,
        trajectory,
        "coding_cleanup_worktree",
        {"artifact_id": artifact_id},
    )
    assert run.result.error == "", run.result.error
    assert not worktree.exists(), "worktree directory should be removed"
    assert trajectory.all_errors_absent()


@pytest.mark.eval
@pytest.mark.asyncio
async def test_worktree_paths_stay_outside_source(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """Owned worktrees live under <data-dir>/git-worktrees, never inside the source."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    run = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "eval"})
    worktree = Path(str(run.result.output_json["worktree_path"]))
    try:
        assert worktree.relative_to(repo.root)
        raise AssertionError("worktree must not live inside the source workspace")
    except ValueError:
        pass
    finally:
        artifact_id = str(run.result.output_json["artifact_id"])
        await run_tool(catalog, "coding_cleanup_worktree", factory, {"artifact_id": artifact_id})
        shutil.rmtree(worktree.parent, ignore_errors=True)


@pytest.mark.eval
@pytest.mark.asyncio
async def test_patch_policy_rejects_paths_outside_workspace(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    """A patch referencing a traversal path is refused before git apply."""

    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    run = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "eval"})
    artifact_id = str(run.result.output_json["artifact_id"])

    bad_patch = "--- a/../outside.txt\n+++ b/../outside.txt\n@@\n-x\n+y\n"
    run = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": bad_patch},
    )
    assert run.result.error != "", "traversal patch must be refused"
    assert "not allowed" in run.result.error, run.result.error

    await run_tool(catalog, "coding_cleanup_worktree", factory, {"artifact_id": artifact_id})


@pytest.mark.eval
@pytest.mark.asyncio
async def test_required_check_refuses_missing_sandbox(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert patched.result.error == ""

    checked = await run_tool(
        catalog,
        "coding_run_check",
        factory,
        {"artifact_id": artifact_id, "check_id": "smoke"},
    )

    assert "sandbox" in checked.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
async def test_required_check_rejects_restricted_worktree_floor(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = str(created.result.output_json["worktree_path"])
    patched = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert patched.result.error == ""

    factory.execution_floor_for = lambda _tool_id: ExecutionFloorView(
        execution="sandbox",
        persistence="direct-write",
        paths={str(repo.root.parent): "rw", worktree: "ro"},
    )

    async def forbidden_dispatch(_tool_id: str, _parameters: Mapping[str, object]) -> ToolResult:
        raise AssertionError("the restricted worktree must not be dispatched")

    factory.delegated_tool_dispatch = forbidden_dispatch
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    assert "floor" in checked.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
async def test_check_rejects_worktree_mutation_during_sandbox_run(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": PATCH}
    )
    assert patched.result.error == ""
    _verified_sandbox(factory)
    original_dispatch = factory.delegated_tool_dispatch
    assert original_dispatch is not None

    async def mutating_dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        (Path(str(parameters["cwd"])) / "app.txt").write_text(
            "changed during check\n", encoding="utf-8"
        )
        return await original_dispatch(tool_id, parameters)

    factory.delegated_tool_dispatch = mutating_dispatch
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    assert "modified app.txt; the edits were reverted" in checked.result.error
    data_root = factory.data_dir or factory.root / ".packstest-data"
    assert (data_root / "git-worktrees" / "source" / artifact_id / "app.txt").read_text(
        encoding="utf-8"
    ) != "changed during check\n"
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
async def test_timed_out_check_cannot_certify_commit(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    config = {**_config(repo.root), "required_check_ids": ["smoke"]}
    factory = ToolContextFactory(root=repo.root, pack_config=config, data_dir=tmp_path / "data")
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": PATCH}
    )
    assert patched.result.error == ""
    _verified_sandbox(factory)
    original_dispatch = factory.delegated_tool_dispatch
    assert original_dispatch is not None

    async def timed_out_dispatch(tool_id: str, parameters: Mapping[str, object]) -> ToolResult:
        result = await original_dispatch(tool_id, parameters)
        return result.model_copy(update={"output_json": {**result.output_json, "timed_out": True}})

    factory.delegated_tool_dispatch = timed_out_dispatch
    checked = await run_tool(
        catalog, "coding_run_check", factory, {"artifact_id": artifact_id, "check_id": "smoke"}
    )
    assert checked.result.output_json["status"] == "timed_out"
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks["smoke"].status == "timed_out"
    committed = await run_tool(
        catalog, "coding_commit", factory, {"artifact_id": artifact_id, "message": "not checked"}
    )
    assert "required check" in committed.result.error.lower()


@pytest.mark.eval
@pytest.mark.asyncio
async def test_no_required_checks_allows_commit_without_running_optional_check(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog, "coding_apply_patch", factory, {"artifact_id": artifact_id, "patch": PATCH}
    )
    assert patched.result.error == ""
    committed = await run_tool(
        catalog,
        "coding_commit",
        factory,
        {"artifact_id": artifact_id, "message": "no required checks"},
    )
    assert committed.result.error == ""
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.status == "committed" and artifact.checks == {}


@pytest.mark.eval
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"sandboxed": False, "return_code": 0},
        {"sandboxed": True, "degraded": True, "return_code": 0},
        {"sandboxed": True, "return_code": 0, "cwd": ""},
    ],
)
async def test_required_check_rejects_unverified_sandbox_result(
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    repo = await git_repo(tmp_path)
    factory = ToolContextFactory(
        root=repo.root,
        pack_config=_config(repo.root),
        data_dir=tmp_path / "data",
    )
    created = await run_tool(catalog, "coding_create_worktree", factory, {"summary": "test"})
    artifact_id = str(created.result.output_json["artifact_id"])
    patched = await run_tool(
        catalog,
        "coding_apply_patch",
        factory,
        {"artifact_id": artifact_id, "patch": PATCH},
    )
    assert patched.result.error == ""

    async def fake_sandbox(_tool_id: str, parameters: dict[str, object]) -> ToolResult:
        worktree = str(parameters["cwd"])
        return ToolResult(
            output_text="ok",
            output_json={
                "persistence": "direct-write",
                "cwd": worktree,
                "writable_paths": [worktree],
                **payload,
            },
        )

    factory.execution_floor_for = lambda _tool_id: ExecutionFloorView(
        execution="sandbox",
        persistence="direct-write",
        paths={str(repo.root.parent): "rw"},
    )
    factory.delegated_tool_dispatch = fake_sandbox

    checked = await run_tool(
        catalog,
        "coding_run_check",
        factory,
        {"artifact_id": artifact_id, "check_id": "smoke"},
    )

    assert "sandbox" in checked.result.error.lower()
    artifact = await factory.artifact_store.get(artifact_id)
    assert artifact is not None and artifact.checks == {}
