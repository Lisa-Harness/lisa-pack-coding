"""Edit-recovery eval: pre-execution rejection → recovery text → corrected edit.

Deterministic, in-process, no model calls. Exercises the full kernel dispatch
path (``execute_tool`` → ``_validate_parameters`` → the validation-recovery
hook → the pack's ``execute``) with the coding pack registered through an
in-process catalog source and the authored recovery content served through
an in-process content source — the same transports production wires at
startup.

The scripted trajectory mirrors the 2026-10-14/15 incident family and the
coding-edit-schema-recovery acceptance criteria:

1. a malformed batch (``edits`` as an object or unparseable string) is
   rejected pre-execution with the authored ``edits_not_array`` recovery
   text, and no write happens;
2. an uppercase 64-hex digest normalizes to lowercase and the real edit
   applies (the digest safety check still runs against the snapshot);
3. a second update requires a freshly read digest — the stale one rejects;
4. the recovery → correction → fresh-read → successful-edit trajectory
   completes from evidence, not prose.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from kernel.approval import ApprovalRequired, InMemoryApprovalService
from kernel.capability_binding import CapabilityBinding, CapabilityRef
from kernel.context.content_registry import set_content_registry
from kernel.models import ToolResult
from kernel.tools.registry import ToolExecutionError, execute_tool, register_tool_mcp
from packstest.context import ToolContextFactory
from packstest.git import git_repo
from packstest.runner import run_tool
from packstest.store import MemoryWorkProductStore
from sdk.catalog_sources import CatalogSourceConfig, DirectCatalogSource, DirectContentSource

PACK_PATH = Path(__file__).resolve().parents[4]
CONTENT_ROOT = PACK_PATH / "lisa_packs" / "coding"
_PROVIDER_ID = "lisa-pack-coding"

_HELLO_DIGEST = hashlib.sha256(b"hello\n").hexdigest()


class _SourceConfig(CatalogSourceConfig):
    """Config over just the coding pack checkout."""

    def __init__(self) -> None:
        super().__init__(
            content_roots=[CONTENT_ROOT],
            disabled_packs=frozenset(),
            provider_selections_json="",
        )


class _CodingBindingProvider:
    """Session-style binding provider returning the factory's pack config."""

    def __init__(self, configuration: dict[str, Any]) -> None:
        self._configuration = configuration

    def binding_for(self, capability: CapabilityRef) -> CapabilityBinding | None:
        if capability.capability_id == "coding_edit_files" and capability.owner_id in (
            "",
            _PROVIDER_ID,
        ):
            return CapabilityBinding(
                owner_id=capability.owner_id or _PROVIDER_ID,
                configuration=self._configuration,
            )
        return None


@pytest.fixture()
def kernel_catalog() -> Any:
    """Register the coding pack's tools in the kernel catalog in-process."""

    from sdk.content import invalidate_catalog_cache

    invalidate_catalog_cache()
    register_tool_mcp(
        None,  # type: ignore[arg-type]  # in-process source: no stdio config read
        source=DirectCatalogSource(_SourceConfig()),
    )
    from kernel.tools.registry import get_tool_catalog

    yield get_tool_catalog()
    invalidate_catalog_cache()


@pytest.fixture(autouse=True)
def authored_content() -> Any:
    """Serve the pack's authored content through the in-process source."""

    source = DirectContentSource(_SourceConfig())
    set_content_registry(source)
    yield source
    set_content_registry(None)  # type: ignore[arg-type]


async def _factory(tmp_path: Path, repo_root: Path) -> ToolContextFactory:
    return ToolContextFactory(
        root=repo_root,
        pack_config={"workspace_root": str(repo_root), "required_check_ids": []},
        artifact_store=MemoryWorkProductStore(),
        data_dir=tmp_path / "data",
    )


async def _create_worktree(factory: ToolContextFactory) -> tuple[str, Path]:
    created = await run_tool(
        PACK_PATH, "coding_create_worktree", factory, {"summary": "recovery eval"}
    )
    assert created.result.error == "", created.result.error
    artifact_id = str(created.result.output_json["artifact_id"])
    worktree = Path(str(created.result.output_json["worktree_path"]))
    return artifact_id, worktree


async def _dispatch_edit(factory: ToolContextFactory, artifact_id: str, edits: Any) -> ToolResult:
    service = InMemoryApprovalService()
    ctx = factory.context(
        {"artifact_id": artifact_id, "edits": edits}, tool_id="coding_edit_files"
    )
    ctx.capability_binding_provider = _CodingBindingProvider(dict(factory.pack_config))
    ctx.approval_service = service
    # The full approval round-trip on the write path: request pauses with
    # ``ApprovalRequired``, approve, then resume with the receipt id. The
    # resume context is a fresh context built from the same raw parameters —
    # the first pass's parameter validation rewrites ``ctx.parameters`` to
    # the parsed dump, and the approval action digest covers the parameters
    # as first submitted.
    try:
        return await execute_tool("coding_edit_files", ctx)
    except ApprovalRequired as pending:
        receipt = await service.approve(
            pending.invocation.request_id, pending.invocation.identity
        )
        assert receipt is not None, "auto-approve failed (identity or expiry)"
        resume = factory.context(
            {"artifact_id": artifact_id, "edits": edits}, tool_id="coding_edit_files"
        )
        resume.capability_binding_provider = _CodingBindingProvider(dict(factory.pack_config))
        resume.approval_service = service
        resume.approval_request_id = pending.invocation.request_id
        try:
            return await execute_tool("coding_edit_files", resume)
        except ToolExecutionError as exc:
            return ToolResult(tool_id="coding_edit_files", error=str(exc))
    except ToolExecutionError as exc:
        return ToolResult(tool_id="coding_edit_files", error=str(exc))


def _update(edits_sha: str) -> list[dict[str, Any]]:
    return [
        {
            "kind": "update",
            "path": "app.txt",
            "expected_sha256": edits_sha,
            "replacements": [{"old_text": "hello", "new_text": "hello edited"}],
        }
    ]


@pytest.mark.eval
@pytest.mark.asyncio
async def test_pack_validates_from_source() -> None:
    from packstest.catalog import validate_pack

    report = validate_pack(PACK_PATH)
    assert report.ok, report.summary()


@pytest.mark.eval
@pytest.mark.asyncio
async def test_edit_files_declares_recovery_content(kernel_catalog: Any) -> None:
    definition = kernel_catalog["coding_edit_files"]
    assert definition.validation_recovery is not None
    assert definition.validation_recovery.content_path == "tools/edit_files/recovery.md"
    assert callable(getattr(definition.implementation_module, "validation_recovery_key", None))


@pytest.mark.eval
@pytest.mark.asyncio
async def test_malformed_batch_rejects_with_recovery_text_and_no_write(
    tmp_path: Path, kernel_catalog: Any
) -> None:
    repo = await git_repo(tmp_path)
    factory = await _factory(tmp_path, repo.root)
    artifact_id, worktree = await _create_worktree(factory)

    # edits as an object (the 2026-10-14 family): rejected before any write,
    # with the authored edits_not_array recovery appended.
    rejected = await _dispatch_edit(
        factory,
        artifact_id,
        {"kind": "update", "path": "app.txt", "expected_sha256": _HELLO_DIGEST},
    )
    assert rejected.error != ""
    assert "received invalid parameters" in rejected.error
    assert "must be one JSON array of edit records" in rejected.error
    # No write: the worktree file is untouched.
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.eval
@pytest.mark.asyncio
async def test_uppercase_digest_normalizes_and_applies(
    tmp_path: Path, kernel_catalog: Any
) -> None:
    repo = await git_repo(tmp_path)
    factory = await _factory(tmp_path, repo.root)
    artifact_id, worktree = await _create_worktree(factory)

    edited = await _dispatch_edit(factory, artifact_id, _update(_HELLO_DIGEST.upper()))

    assert edited.error == "", edited.error
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello edited\n"


@pytest.mark.eval
@pytest.mark.asyncio
async def test_stale_digest_rejects_after_a_successful_edit(
    tmp_path: Path, kernel_catalog: Any
) -> None:
    repo = await git_repo(tmp_path)
    factory = await _factory(tmp_path, repo.root)
    artifact_id, worktree = await _create_worktree(factory)

    first = await _dispatch_edit(factory, artifact_id, _update(_HELLO_DIGEST.upper()))
    assert first.error == "", first.error

    # The previous digest is stale after the write; reusing it must reject
    # with the file-changed message, never silently overwrite.
    stale = await _dispatch_edit(
        factory,
        artifact_id,
        [
            {
                "kind": "update",
                "path": "app.txt",
                "expected_sha256": _HELLO_DIGEST.upper(),
                "replacements": [{"old_text": "hello edited", "new_text": "hello twice"}],
            }
        ],
    )
    assert "File changed since it was read" in stale.error
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello edited\n"


@pytest.mark.eval
@pytest.mark.asyncio
async def test_rejection_recovery_correction_trajectory(
    tmp_path: Path, kernel_catalog: Any
) -> None:
    """Rejection → corrected arguments → fresh read → successful edit."""

    repo = await git_repo(tmp_path)
    factory = await _factory(tmp_path, repo.root)
    artifact_id, worktree = await _create_worktree(factory)

    # 1. A malformed string batch rejects with recovery text naming the
    #    corrective action and no filesystem mutation.
    rejected = await _dispatch_edit(factory, artifact_id, "definitely not a JSON array")
    assert "must be one JSON array of edit records" in rejected.error
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello\n"

    # 2. Fresh read supplies the current digest.
    read = await run_tool(
        PACK_PATH,
        "coding_read_file",
        factory,
        {"artifact_id": artifact_id, "path": "app.txt"},
    )
    assert read.result.error == "", read.result.error
    digest = str(read.result.output_json["content_sha256"])

    # 3. The corrected batch applies exactly the intended diff.
    corrected = await _dispatch_edit(factory, artifact_id, _update(digest.upper()))
    assert corrected.error == "", corrected.error
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "hello edited\n"
