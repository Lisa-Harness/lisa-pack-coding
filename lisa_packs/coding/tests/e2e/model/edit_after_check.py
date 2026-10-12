"""Scripted model for edit_after_check.scenario.yml (see e2etest.scripted).

Replays the live sequence that stalled: worktree, status, check, read, an
edit with a stale digest, reread, the real edit, the check again, then a
commit. Each
step reads ids and digests from the previous real tool results.
"""

from __future__ import annotations

from e2etest.scripted import Reply, ScriptError, ScriptTurn

_STALE_SHA = "0" * 64
_BUG = "return total(values) / (len(values) - 1)"
_FIX = "return total(values) / len(values)"


def _artifact_id(turn: ScriptTurn) -> str:
    created = turn.last("coding_create_worktree")
    artifact_id = created.data.get("artifact_id") if created else None
    if not artifact_id:
        raise ScriptError(f"no artifact_id in create_worktree result: {created}")
    return str(artifact_id)


def _edit(artifact_id: str, sha: str) -> Reply:
    return Reply.call(
        "coding_edit_files",
        artifact_id=artifact_id,
        edits=[
            {
                "kind": "update",
                "path": "stats.py",
                "expected_sha256": sha,
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
        return Reply.call("coding_create_worktree", name="fix-mean")
    artifact_id = _artifact_id(turn)
    reads = turn.count("coding_read_file")
    edits = turn.count("coding_edit_files")
    if turn.count("coding_status") == 0:
        return Reply.call("coding_status", artifact_id=artifact_id)
    if turn.count("coding_run_check") == 0:
        return Reply.call("coding_run_check", artifact_id=artifact_id, check_id="test")
    if reads == 0:
        return Reply.call("coding_read_file", artifact_id=artifact_id, path="stats.py")
    if edits == 0:
        return _edit(artifact_id, _STALE_SHA)
    if reads == 1:
        return Reply.call("coding_read_file", artifact_id=artifact_id, path="stats.py")
    if edits == 1:
        read = turn.last("coding_read_file")
        sha = read.data.get("content_sha256") if read else None
        if not sha:
            raise ScriptError(f"no content_sha256 in read result: {read}")
        return _edit(artifact_id, str(sha))
    if turn.count("coding_run_check") == 1:
        return Reply.call("coding_run_check", artifact_id=artifact_id, check_id="test")
    if turn.count("coding_commit") == 0:
        return Reply.call(
            "coding_commit", artifact_id=artifact_id, message="Fix stats.mean divisor"
        )
    return Reply.text("Fixed stats.mean and the required test check passes.")
