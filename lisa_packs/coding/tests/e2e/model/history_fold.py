"""Scripted model for history_fold.scenario.yml (see e2etest.scripted).

Worktree, status, read, reread (supersedes the first read), edit with the
reread's digest (invalidates the reread and the status), verify, commit
(supersedes the verify). Eight coding_execute iterations, under the stage
cap. Ids and digests come from the real tool results.
"""

from __future__ import annotations

from e2etest.scripted import Reply, ScriptError, ScriptTurn

_BUG = "return total(values) / (len(values) - 1)"
_FIX = "return total(values) / len(values)"


def _artifact_id(turn: ScriptTurn) -> str:
    created = turn.last("coding_create_worktree")
    artifact_id = created.data.get("artifact_id") if created else None
    if not artifact_id:
        raise ScriptError(f"no artifact_id in create_worktree result: {created}")
    return str(artifact_id)


def _edit(turn: ScriptTurn, artifact_id: str) -> Reply:
    read = turn.last("coding_read_file")
    sha = read.data.get("content_sha256") if read else None
    if not sha:
        raise ScriptError(f"no content_sha256 in read result: {read}")
    return Reply.call(
        "coding_edit_files",
        artifact_id=artifact_id,
        edits=[
            {
                "kind": "update",
                "path": "stats.py",
                "expected_sha256": str(sha),
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
    steps = [
        (turn.count("coding_status") == 0, ("coding_status", {})),
        (reads == 0, ("coding_read_file", {"path": "stats.py"})),
        (reads == 1, ("coding_read_file", {"path": "stats.py"})),
    ]
    for due, (tool, args) in steps:
        if due:
            return Reply.call(tool, artifact_id=artifact_id, **args)
    if turn.count("coding_edit_files") == 0:
        return _edit(turn, artifact_id)
    if turn.count("coding_verify") == 0:
        return Reply.call("coding_verify", artifact_id=artifact_id)
    if turn.count("coding_commit") == 0:
        return Reply.call(
            "coding_commit", artifact_id=artifact_id, message="Fix stats.mean divisor"
        )
    return Reply.text("Fixed stats.mean; the reread, edit, and checks are recorded.")
