"""Scripted model for edit_recovery.scenario.yml (see runtime.testing.scripted).

Deliberately sends a malformed edit batch (object-typed ``edits``) first, so
the kernel's validation-recovery hook rejects it pre-execution with the
authored recovery text and no write happens. The script then reads the file
fresh and applies the corrected edit with an UPPERCASE digest — proving case
normalization on the write path — and finishes. Ids and digests come from
the real tool results.
"""

from __future__ import annotations

from runtime.testing.scripted import Reply, ScriptError, ScriptTurn

_BUG = "return total(values) / (len(values) - 1)"
_FIX = "return total(values) / len(values)"


def _artifact_id(turn: ScriptTurn) -> str:
    created = turn.last("coding_create_worktree")
    artifact_id = created.data.get("artifact_id") if created else None
    if not artifact_id:
        raise ScriptError(f"no artifact_id in create_worktree result: {created}")
    return str(artifact_id)


def _malformed_edit(artifact_id: str) -> Reply:
    # The 2026-10-14 fault family: ``edits`` as one object instead of an
    # array of records. Must reject pre-execution with recovery text.
    return Reply.call(
        "coding_edit_files",
        artifact_id=artifact_id,
        edits={
            "kind": "update",
            "path": "stats.py",
            "expected_sha256": "0" * 64,
            "replacements": [{"old_text": _BUG, "new_text": _FIX}],
        },
    )


def _corrected_edit(turn: ScriptTurn, artifact_id: str) -> Reply:
    read = turn.last("coding_read_file")
    sha = read.data.get("content_sha256") if read else None
    if not sha:
        raise ScriptError(f"no content_sha256 in read result: {read}")
    # Uppercase on purpose: the digest case-normalizer must accept it.
    return Reply.call(
        "coding_edit_files",
        artifact_id=artifact_id,
        edits=[
            {
                "kind": "update",
                "path": "stats.py",
                "expected_sha256": str(sha).upper(),
                "replacements": [{"old_text": _BUG, "new_text": _FIX}],
            }
        ],
    )


def auxiliary(turn: ScriptTurn) -> Reply:
    if turn.request.response_format is not None:
        return Reply.text("{}")
    return Reply.text("stats.mean divides by the sample count, not count minus one.")


def respond(turn: ScriptTurn) -> Reply:
    if "coding_create_worktree" not in turn.tool_names:
        if "transition_stage" not in turn.tool_names or turn.count("transition_stage"):
            raise ScriptError(
                f"coding tools not offered; tools={sorted(turn.tool_names)}"
            )
        return Reply.call(
            "transition_stage",
            target_stage_id="coding_execute",
            rationale="Code change requested.",
            delegated_goal=turn.user_text,
        )
    if turn.count("coding_create_worktree") == 0:
        return Reply.call("coding_create_worktree", name="fix-mean-recovery")
    artifact_id = _artifact_id(turn)
    edits = turn.count("coding_edit_files")
    reads = turn.count("coding_read_file")
    if edits == 0:
        return _malformed_edit(artifact_id)
    if edits == 1 and reads == 0:
        rejected = turn.last("coding_edit_files")
        if rejected is None or "must be one JSON array of edit records" not in rejected.text:
            raise ScriptError(
                f"malformed edit was not rejected with recovery text: "
                f"{rejected.text[:200] if rejected else None}"
            )
        # Recover exactly as the recovery text teaches: read fresh, then
        # resend the corrected array.
        return Reply.call("coding_read_file", artifact_id=artifact_id, path="stats.py")
    if edits == 1 and reads >= 1:
        return _corrected_edit(turn, artifact_id)
    if edits == 2:
        applied = turn.last("coding_edit_files")
        if applied is None or applied.failed:
            raise ScriptError(f"corrected edit failed: {applied.text[:200] if applied else None}")
        if turn.count("coding_verify") == 0:
            return Reply.call("coding_verify", artifact_id=artifact_id)
        if turn.count("coding_commit") == 0:
            return Reply.call(
                "coding_commit", artifact_id=artifact_id, message="Fix stats.mean divisor"
            )
        return Reply.text(
            "Recovered from the malformed batch: read fresh, applied the corrected "
            "edit with the uppercase digest, verified, and committed; no diff came "
            "from the rejected call."
        )
    raise ScriptError(f"too many coding_edit_files calls: {edits}")
