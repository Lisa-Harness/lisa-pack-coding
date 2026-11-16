"""Validation-recovery classification and digest normalization for edit_files.

Covers the two pack-owned halves of the coding-edit-schema-recovery design:

- :func:`validation_recovery_key` — the pure classifier the kernel's
  validation-recovery hook calls on a pre-execution rejection;
- ``FileEdit`` digest case normalization — uppercase/mixed-case 64-hex
  digests apply against the same snapshot digest; every invalid digest is
  still rejected.

The rendered recovery text and the missing-content failure path are
exercised end-to-end in the packstest evals (``tests/evals``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from packstest.store import MemoryWorkProductStore
from pydantic import ValidationError

from lisa_packs.coding.tests.test_git_workflow import _context, _repository
from lisa_packs.coding.tools.create_worktree.create_worktree import (
    execute as create_worktree,
)
from lisa_packs.coding.tools.edit_files.edit_files import (
    RECOVERY_KEYS,
    FileEdit,
    Parameters,
    validation_recovery_key,
)
from lisa_packs.coding.tools.edit_files.edit_files import (
    execute as edit_files,
)


def _update(path: str = "app.txt", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": "update",
        "path": path,
        "expected_sha256": "a" * 64,
        "replacements": [{"old_text": "hello", "new_text": "hello edited"}],
    }
    record.update(overrides)
    return record


def _rejects(payload: object) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        Parameters.model_validate(payload)
    return caught.value


def _classify(payload: object) -> str:
    return validation_recovery_key(_rejects(payload))


# ── Classifier: recovery keys ───────────────────────────────────────────


def test_scalar_edits_classify_as_not_array() -> None:
    assert _classify({"artifact_id": "wt", "edits": "not json at all"}) == "edits_not_array"


def test_object_edits_classify_as_not_array() -> None:
    assert (
        _classify({"artifact_id": "wt", "edits": _update()}) == "edits_not_array"
    )


def test_bad_digest_classifies_as_digest_format() -> None:
    record = _update(expected_sha256="0" * 63)  # short
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "digest_format"

    record = _update(expected_sha256="z" * 64)  # non-hex
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "digest_format"

    record = _update(expected_sha256=" " + "a" * 64)  # whitespace-padded
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "digest_format"

    record = _update(expected_sha256="")  # empty
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "digest_format"


def test_null_digest_on_update_classifies_as_digest_format() -> None:
    # None fails the shape validator first ("Updates require..."); the
    # digest-format key still applies because the model's only recovery is
    # to obtain the real digest.
    record = _update(expected_sha256=None)
    assert _classify({"artifact_id": "wt", "edits": [record]}) in {
        "digest_format",
        "record_shape",
    }


def test_replacement_shape_classifies_correctly() -> None:
    record = _update(replacements=[{"old_text": "hello"}])  # missing new_text
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "replacement_shape"

    record = _update(replacements="not a list")
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "replacement_shape"


def test_missing_record_fields_classify_as_missing_required() -> None:
    record = _update()
    del record["path"]
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "missing_required"


def test_missing_artifact_id_classifies_as_missing_required() -> None:
    assert _classify({"edits": [_update()]}) == "missing_required"


def test_kind_shape_violation_classifies_as_record_shape() -> None:
    # update with content: wrong shape for the declared kind
    record = _update(content="whole file")
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "record_shape"

    # create with replacements: wrong shape
    record = {
        "kind": "create",
        "path": "app.txt",
        "content": "text",
        "expected_sha256": "a" * 64,
    }
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "record_shape"


def test_legacy_flat_record_without_lift_source_classifies_as_missing_required() -> None:
    # Pre-Oct-1 flat record with no arguments-level lift source: the exact
    # legacy lift refuses it, and shape validation reports the record's own
    # missing ``kind``/``path`` — the recovery names exactly those fields.
    record = {"old_text": "hello", "new_text": "hello edited"}
    assert _classify({"artifact_id": "wt", "edits": [record]}) == "missing_required"


def test_non_validation_error_classifies_as_unknown() -> None:
    assert validation_recovery_key(ValueError("boom")) == "unknown_or_other"


def test_every_declared_key_has_an_authored_section() -> None:
    module_dir = Path(__file__).resolve().parents[1] / "tools" / "edit_files"
    content = (module_dir / "recovery.md").read_text(encoding="utf-8")
    missing = [key for key in RECOVERY_KEYS if f"## {key}" not in content]
    assert not missing, f"recovery.md missing sections: {missing}"


# ── Digest case normalization ───────────────────────────────────────────


def test_uppercase_digest_normalizes_and_validates() -> None:
    digest = "A" + "F" * 31 + "0" * 32
    edit = FileEdit.model_validate(
        _update(expected_sha256=digest.upper())
    )
    assert edit.expected_sha256 == digest.upper().lower()


def test_mixed_case_digest_normalizes_and_validates() -> None:
    edit = FileEdit.model_validate(_update(expected_sha256="AbCdEf0189" + "0" * 54))
    assert edit.expected_sha256 == ("AbCdEf0189" + "0" * 54).lower()


def test_short_digest_stays_rejected() -> None:
    with pytest.raises(ValidationError):
        FileEdit.model_validate(_update(expected_sha256="AB" * 31))  # 62 chars


def test_padded_digest_stays_rejected() -> None:
    with pytest.raises(ValidationError):
        FileEdit.model_validate(_update(expected_sha256=" " + "A" * 64))


def test_non_hex_uppercase_stays_rejected() -> None:
    with pytest.raises(ValidationError):
        FileEdit.model_validate(_update(expected_sha256="G" * 64))


@pytest.mark.asyncio
async def test_uppercase_digest_applies_the_real_edit(tmp_path: Path) -> None:
    """An uppercase digest applies against the same snapshot digest."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "upper digest"}))
    artifact_id = created.output_json["artifact_id"]
    digest = hashlib.sha256(b"hello\n").hexdigest().upper()

    edited = await edit_files(
        _context(
            root,
            store,
            {
                "artifact_id": artifact_id,
                "edits": [_update(expected_sha256=digest)],
            },
        )
    )

    assert edited.error == "", edited.error
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == (
        "hello edited\n"
    )


@pytest.mark.asyncio
async def test_stale_digest_still_fails_after_normalization(tmp_path: Path) -> None:
    """A well-formed but stale digest must never silently overwrite."""

    root = await _repository(tmp_path)
    store = MemoryWorkProductStore()
    created = await create_worktree(_context(root, store, {"summary": "stale digest"}))
    artifact_id = created.output_json["artifact_id"]
    stale = ("b" * 63 + "c").upper()  # well-formed, wrong value

    rejected = await edit_files(
        _context(
            root,
            store,
            {"artifact_id": artifact_id, "edits": [_update(expected_sha256=stale)]},
        )
    )

    assert rejected.error != ""
    assert "File changed since it was read" in rejected.error
    artifact = await store.get(artifact_id)
    assert artifact is not None
    assert Path(artifact.worktree_path, "app.txt").read_text(encoding="utf-8") == "hello\n"
