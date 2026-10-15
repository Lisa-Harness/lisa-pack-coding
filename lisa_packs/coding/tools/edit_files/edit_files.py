"""Apply bounded, exact text edits to an owned coding worktree."""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from itertools import islice
from pathlib import Path
from typing import Any, Literal, cast

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import CodingPackConfig, get_coding_config
from lisa_packs.coding.local_files import (
    CodingPathError,
    read_utf8_file_snapshot,
    resolve_relative_path_at_root,
)
from lisa_packs.coding.workflow import (
    CodingWorkflowError,
    artifact_worktree,
    load_owned_artifact,
    run_git,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

TOOL_ID = "coding_edit_files"


class _JsonTailState:
    """Final scan state of a structural walk over a truncated JSON payload."""

    __slots__ = ("dangling_escape", "stack", "string_open")

    def __init__(
        self, stack: list[str], string_open: bool, dangling_escape: bool
    ) -> None:
        self.stack = stack
        self.string_open = string_open
        self.dangling_escape = dangling_escape


def _scan_json_tail(text: str) -> _JsonTailState | None:
    """Walk ``text`` honoring JSON string/escape state and open brackets.

    Returns ``None`` when the payload is not a plausibly-truncated JSON
    document: a closer that pops an empty stack or mismatches its opening
    bracket means the model sent something other than an unterminated
    structure, and appending characters cannot fix it.
    """

    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack:
                return None
            expected = "]" if stack[-1] == "[" else "}"
            if char != expected:
                return None
            stack.pop()
    return _JsonTailState(stack, in_string, escaped)


def _complete_json_payload(text: str) -> str | None:
    """Close a truncated JSON payload's tail, or return ``None`` to refuse.

    Appends the missing closing quote (when the payload ends inside a string)
    and the missing bracket closers. A dangling trailing escape cannot be
    completed faithfully — ``\\"`` is itself ambiguous — so that shape is
    refused. The caller must still ``json.loads`` the result: appending
    closers can yield structurally closed but syntactically invalid JSON
    (a trailing comma before the end), which this function leaves for the
    parse gate to reject.
    """

    state = _scan_json_tail(text)
    if state is None:
        return None
    if not state.stack and not state.string_open:
        return None
    if state.dangling_escape:
        return None
    completed = text
    if state.string_open:
        completed += '"'
    completed += "".join(
        "]" if opener == "[" else "}" for opener in reversed(state.stack)
    )
    return completed


def _decode_string_edits(value: str) -> list[FileEdit] | None:
    """Decode a model that serialized ``edits`` into a JSON string.

    glm-5.3:cloud twice rendered ``edits`` as a JSON-escaped string inside the
    arguments object (2026-10-14 self-review run: 4 of 8 edit calls), once
    with the under-layer truncated by missing tail closers. The string must
    parse — directly or after faithful tail completion — as a JSON array
    of objects; anything else is refused unchanged.
    """

    candidate = value.strip()
    if not candidate.startswith("["):
        return None
    decoded: object
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError:
        completed = _complete_json_payload(candidate)
        if completed is None:
            return None
        try:
            decoded = json.loads(completed)
        except json.JSONDecodeError:
            return None
    if not isinstance(decoded, list) or not all(
        isinstance(item, dict) for item in decoded
    ):
        return None
    return cast("list[FileEdit]", decoded)


_KIND_KEYS = frozenset({"kind", "path", "replacements", "content", "expected_sha256"})


def _lift_flat_record(
    record: Mapping[str, Any], *, path: str, digest: object
) -> dict[str, Any]:
    """Rebuild one flat ``{old_text,new_text}`` record as a kind-shaped update."""

    return {
        "kind": "update",
        "path": path,
        "expected_sha256": digest,
        "replacements": [
            {"old_text": record["old_text"], "new_text": record["new_text"]}
        ],
    }


def _maybe_lift_flat_string_edits(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    """Transmute a legacy flat-shape batch into the current record contract.

    Two fault layers stack in the 2026-10-15 incident payloads: the ``edits``
    array serialized into a JSON string (the 2026-10-14 fault) *and* records
    shaped like the pre-Oct-1 ``{old_text,new_text}`` API. The lift source —
    ``path``/``expected_sha256`` — lives beside ``edits``, not inside each
    record, so this runs on the whole arguments object after the string
    decode pass. Shape-preserving by construction: kind-shaped record edits,
    flat records without a lift source, and unknown records pass through
    untouched so shape validation still names the real contract.
    """

    edits = arguments.get("edits")
    if isinstance(edits, str):
        edits = _decode_string_edits(edits)
    if not isinstance(edits, list) or not edits:
        return arguments
    lifted_path = arguments.get("path")
    lifted_digest = arguments.get("expected_sha256")
    if not isinstance(lifted_path, str) or not lifted_path:
        return arguments
    transmuted: list[Any] = []
    for edit in edits:
        if _is_flat_shape_edit(edit):
            transmuted.append(
                _lift_flat_record(edit, path=lifted_path, digest=lifted_digest)
            )
        else:
            transmuted.append(edit)
    lifted: dict[str, Any] = dict(arguments)
    lifted["edits"] = transmuted
    # The lift source is consumed by the transmutation; leaving ``path`` and
    # ``expected_sha256`` in the arguments would shadow the record contract's
    # own per-edit fields in future readers and mislead error messaging.
    lifted.pop("path", None)
    return lifted


def _is_flat_shape_edit(edit: Any) -> bool:
    """Report whether one edit value uses the pre-Oct-1 flat record shape.

    The 2026-10-15 run echoed the legacy API from stale recalled context:
    records carried only ``old_text``/``new_text`` — no ``kind``, ``path``,
    or ``replacements`` — with ``path`` and ``expected_sha256`` lifted to
    the arguments object.
    """

    return (
        isinstance(edit, Mapping)
        and not edit.keys() & _KIND_KEYS
        and "old_text" in edit
        and "new_text" in edit
    )


class Replacement(BaseModel):
    """One exact replacement against an original file snapshot."""

    old_text: str = Field(min_length=1, max_length=100_000)
    new_text: str = Field(max_length=100_000)


class FileEdit(BaseModel):
    """One bounded file action in a structured edit batch."""

    kind: Literal["update", "create", "delete"]
    path: str = Field(min_length=1, max_length=500)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    replacements: list[Replacement] = Field(default_factory=list[Replacement], max_length=50)
    content: str | None = Field(default=None, max_length=500_000)

    @model_validator(mode="after")
    def _validate_shape(self) -> FileEdit:
        if self.kind == "update":
            if self.expected_sha256 is None or not self.replacements or self.content is not None:
                raise ValueError("Updates require expected_sha256 and replacements only.")
        elif self.kind == "create":
            if self.expected_sha256 is not None or self.content is None or self.replacements:
                raise ValueError("Creates require content only.")
        elif self.expected_sha256 is None or self.replacements or self.content is not None:
            raise ValueError("Deletes require expected_sha256 only.")
        return self


class Parameters(BaseModel):
    """Structured edit request for one owned worktree."""

    model_config = ConfigDict(strict=False)

    artifact_id: str = Field(min_length=1, max_length=100)
    edits: list[FileEdit] = Field(min_length=1, max_length=20)

    @field_validator("edits", mode="before")
    @classmethod
    def _accept_string_edits(cls, value: object) -> object:
        """Decode a string-typed ``edits`` payload before list validation.

        Some providers serialize the array into a JSON string inside the
        arguments object (glm-5.3:cloud, 2026-10-14 incident: repeated
        ``edits → Input should be a valid list`` rejections burned a 20-call
        cap window). The string must decode to a JSON array of dict-shaped
        edits under the same length bound; anything else passes through so
        the pydantic error names the real shape.
        """

        if isinstance(value, str) and len(value) <= 2_000_000:
            decoded = _decode_string_edits(value)
            if decoded is not None:
                return decoded
        return value

    @model_validator(mode="before")
    @classmethod
    def _lift_flat_string_records(cls, data: object) -> object:
        """Transmute legacy flat-shape batches before record validation.

        The 2026-10-15 payloads stacked two faults: string-typed ``edits``
        (the field validator above decodes that) and pre-Oct-1 flat records
        ``{old_text,new_text}`` whose ``path``/``expected_sha256`` sit at the
        arguments level. Running after string decoding, this sees either
        shape for the edits value and rewrites only exact legacy signatures;
        everything else passes through untouched.
        """

        if isinstance(data, Mapping):
            return _maybe_lift_flat_string_edits(data)
        return data


@dataclass(frozen=True)
class PreparedEdit:
    """Validated desired file content used to generate a single Git patch."""

    path: str
    before: str | None
    after: str | None


_MAX_OCCURRENCE_LINES = 5
_MAX_EXCERPT_LINES = 8


def _occurrences(source: str, needle: str) -> list[int]:
    """Return every start offset of ``needle``, including overlapping matches."""

    found: list[int] = []
    start = source.find(needle)
    while start >= 0:
        found.append(start)
        start = source.find(needle, start + 1)
    return found


def _indentation(line: str) -> str:
    """Describe a line's leading whitespace in countable words."""

    lead = line[: len(line) - len(line.lstrip(" \t"))]
    tabs, spaces = lead.count("\t"), lead.count(" ")
    words = [f"{tabs} tab{'s' * (tabs != 1)}"] if tabs else []
    words += [f"{spaces} space{'s' * (spaces != 1)}"] if spaces else []
    return " + ".join(words) or "no indentation"


def _nearest_excerpt(source: str, old_text: str) -> str:
    """Show the actual lines closest to an unmatched ``old_text``.

    The anchor is the first non-blank old_text line compared without leading
    whitespace, so a wrong indentation guess still locates its target and the
    indentation difference is named instead of left for the model to spot.
    """

    old_lines = old_text.split("\n")
    anchor = next((line for line in old_lines if line.strip()), "")
    if not anchor:
        return ""
    lines = source.split("\n")
    stripped = [line.strip() for line in lines]
    candidates = [index for index, line in enumerate(stripped) if line == anchor.strip()]
    if not candidates:
        close = difflib.get_close_matches(anchor.strip(), stripped, n=1, cutoff=0.6)
        candidates = [stripped.index(close[0])] if close else []
    if not candidates:
        return ""
    offset = old_lines.index(anchor)
    wanted = [line.strip() for line in old_lines]

    def similarity(index: int) -> float:
        start = max(index - offset, 0)
        window = stripped[start : start + len(wanted)]
        return difflib.SequenceMatcher(a=wanted, b=window).ratio()

    index = max(candidates, key=similarity)
    first = max(index - offset, 0)
    last = min(first + len(old_lines), first + _MAX_EXCERPT_LINES, len(lines))
    width = len(str(last))
    excerpt = "\n".join(f"{number + 1:>{width}}\t{lines[number]}" for number in range(first, last))
    note = ""
    lead = _indentation(lines[index])
    if stripped[index] == anchor.strip() and lead != _indentation(anchor):
        note = (
            f" Indentation differs at line {index + 1}: the file has "
            f"{lead}, old_text has {_indentation(anchor)}."
        )
    return f"{note} Nearest actual lines:\n{excerpt}"


def _replacement_problem(source: str, index: int, replacement: Replacement) -> str:
    """Explain why one replacement does not match exactly once, or return ''."""

    found = _occurrences(source, replacement.old_text)
    if len(found) == 1:
        return ""
    if found:
        lines = ", ".join(
            str(source.count("\n", 0, start) + 1) for start in found[:_MAX_OCCURRENCE_LINES]
        )
        return (
            f"replacement {index} old_text occurs {len(found)} times (lines {lines}); "
            "include more surrounding text so it matches exactly once."
        )
    return (
        f"replacement {index} old_text was not found."
        f"{_nearest_excerpt(source, replacement.old_text)}"
    )


def _replace_snapshot(source: str, replacements: list[Replacement], *, path: str = "") -> str:
    """Apply unique, non-overlapping replacements against the original snapshot.

    Every failing replacement is reported at once so one correction round
    fixes the whole batch.
    """

    label = f"{path}: " if path else ""
    problems = [
        f"{label}{problem}"
        for index, replacement in enumerate(replacements)
        if (problem := _replacement_problem(source, index, replacement))
    ]
    if problems:
        raise CodingWorkflowError(
            "Each old_text must occur exactly once in the original file snapshot.\n"
            + "\n".join(problems)
        )
    spans = sorted(
        (start, start + len(replacement.old_text), replacement.new_text)
        for replacement in replacements
        for start in (source.find(replacement.old_text),)
    )

    parts: list[str] = []
    cursor = 0
    for start, end, new_text in spans:
        if start < cursor:
            raise CodingWorkflowError(
                f"{label}Replacement spans must not overlap in the original file snapshot."
            )
        parts.extend((source[cursor:start], new_text))
        cursor = end
    parts.append(source[cursor:])
    return "".join(parts)


def _diff_lines(content: str | None) -> list[str]:
    """Split at LF only, preserving other separators as literal file content."""

    if content is None:
        return []
    parts = content.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _unified_patch(edits: list[PreparedEdit]) -> str:
    """Render a Git-compatible patch from fully validated text snapshots."""

    lines: list[str] = []
    for edit in edits:
        before = _diff_lines(edit.before)
        after = _diff_lines(edit.after)
        lines.extend((f"diff --git a/{edit.path} b/{edit.path}\n",))
        if edit.before is None:
            lines.extend(("new file mode 100644\n", "--- /dev/null\n", f"+++ b/{edit.path}\n"))
        elif edit.after is None:
            lines.extend(
                (
                    "deleted file mode 100644\n",
                    f"--- a/{edit.path}\n",
                    "+++ /dev/null\n",
                )
            )
        else:
            lines.extend((f"--- a/{edit.path}\n", f"+++ b/{edit.path}\n"))
        diff = difflib.unified_diff(before, after, n=3)
        for line in islice(diff, 2, None):
            if line.endswith("\n"):
                lines.append(line)
            else:
                lines.extend((line + "\n", "\\ No newline at end of file\n"))
    return "".join(lines)


def _prepare_edit(edit: FileEdit, *, worktree: Path, config: CodingPackConfig) -> PreparedEdit:
    """Validate one file action and compute its desired snapshot without writing."""

    path = resolve_relative_path_at_root(worktree, edit.path)
    if edit.kind == "create":
        if path.exists():
            raise CodingWorkflowError(f"Cannot create existing file: {edit.path}")
        assert edit.content is not None
        if len(edit.content.encode("utf-8")) > config.max_file_bytes:
            raise CodingWorkflowError("Created content exceeds the configured file byte limit.")
        return PreparedEdit(path=edit.path, before=None, after=edit.content)
    if not path.is_file():
        raise CodingWorkflowError(f"Requested edit path is not a file: {edit.path}")
    before, digest = read_utf8_file_snapshot(path, max_bytes=config.max_file_bytes)
    if digest != edit.expected_sha256:
        raise CodingWorkflowError(f"File changed since it was read: {edit.path}")
    if edit.kind == "delete":
        return PreparedEdit(path=edit.path, before=before, after=None)
    after = _replace_snapshot(before, edit.replacements, path=edit.path)
    if len(after.encode("utf-8")) > config.max_file_bytes:
        raise CodingWorkflowError("Edited content exceeds the configured file byte limit.")
    return PreparedEdit(path=edit.path, before=before, after=after)


def _prepare_batch(
    params: Parameters, *, worktree: Path, config: CodingPackConfig
) -> list[PreparedEdit]:
    """Validate every file action, reporting all failing files together."""

    if len({edit.path for edit in params.edits}) != len(params.edits):
        raise CodingWorkflowError("Each structured edit path must be unique.")
    prepared: list[PreparedEdit] = []
    problems: list[str] = []
    for edit in params.edits:
        try:
            prepared.append(_prepare_edit(edit, worktree=worktree, config=config))
        except (CodingPathError, CodingWorkflowError) as exc:
            problems.append(str(exc))
    if problems:
        raise CodingWorkflowError(
            "\n".join(problems) + "\nNo edits were applied; the batch is atomic."
        )
    return prepared


async def _checked_patch(
    params: Parameters, *, worktree: Path, config: CodingPackConfig
) -> tuple[list[PreparedEdit], str]:
    """Return the changed edits and their patch once ``git apply --check`` accepts it."""

    prepared = _prepare_batch(params, worktree=worktree, config=config)
    changed = [edit for edit in prepared if edit.before != edit.after]
    if not changed:
        raise CodingWorkflowError("Structured edit batch makes no changes.")
    patch = _unified_patch(changed)
    if len(patch.encode("utf-8")) > config.max_patch_bytes:
        raise CodingWorkflowError("Generated patch exceeds the configured byte limit.")
    checked = await run_git(
        worktree,
        ("apply", "--check", "--whitespace=nowarn", "-"),
        config=config,
        stdin_text=patch,
    )
    if checked.return_code != 0:
        raise CodingWorkflowError(checked.stderr.strip() or "git apply --check failed")
    return changed, patch


def _validated_parameters(ctx: ToolExecutionContext) -> Parameters:
    """Validate tool parameters, surfacing the pydantic error as tool text.

    Pydantic ``ValidationError`` is a ``ValueError`` subclass; execute's
    ``except (… ValueError)`` clause would otherwise mask the shape problem
    behind ``tool_id``-empty failure text, so the translation happens here
    where both call sites share the exact message.
    """

    try:
        return Parameters.model_validate(ctx.parameters)
    except ValidationError as exc:
        raise CodingWorkflowError(
            f"Tool '{TOOL_ID}' received invalid parameters: {exc}"
        ) from exc


async def preflight(ctx: ToolExecutionContext) -> ToolResult | None:
    """Reject an edit batch that cannot apply before it waits for approval."""

    params = _validated_parameters(ctx)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx, artifact_id=params.artifact_id, config=config
        )
        await _checked_patch(
            params, worktree=artifact_worktree(artifact, config=config), config=config
        )
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))
    return None


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Validate and apply all structured edits as one owned-worktree revision."""

    try:
        params = _validated_parameters(ctx)
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx, artifact_id=params.artifact_id, config=config, claim=True
        )
        worktree = artifact_worktree(artifact, config=config)
        changed, patch = await _checked_patch(params, worktree=worktree, config=config)
        applied = await run_git(
            worktree,
            ("apply", "--whitespace=nowarn", "-"),
            config=config,
            stdin_text=patch,
        )
        if applied.return_code != 0:
            raise CodingWorkflowError(applied.stderr.strip() or "git apply failed")
        artifact.revision += 1
        artifact.changed_paths = list(
            dict.fromkeys([*artifact.changed_paths, *(edit.path for edit in changed)])
        )
        artifact.checks = {}
        artifact.status = "patched"
        await ctx.code_change_artifact_store.save(artifact)
    except (CodingPathError, CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=(
            f"Applied structured edits to {len(changed)} paths at revision {artifact.revision}."
        ),
        output_json={
            "artifact_id": artifact.artifact_id,
            "revision": artifact.revision,
            "changed_paths": artifact.changed_paths,
            "patch_sha256": sha256(patch.encode("utf-8")).hexdigest(),
        },
    )
