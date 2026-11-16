# coding_edit_files validation recovery

Authored recovery text for malformed `coding_edit_files` arguments, rendered
by the kernel's validation-recovery hook when pre-execution parameter
validation rejects a call. Each heading below is a recovery key; the hook
selects one section, formats the named substitutions with missing-key-safe
`str.format_map`, and appends the rendered text to the original bounded
validation summary. No edit was applied by the rejected call.

Authoring rule: literal braces in examples are doubled (`{{`/`}}`) because
the section body is a `str.format_map` template; only `{field_path}` and
`{validation_detail}` are live substitutions.

Substitutions available in every section:

- `{field_path}` — the first failing field path (e.g. `edits.0.expected_sha256`)
- `{validation_detail}` — the bounded original validation summary

## edits_not_array

`edits` must be one JSON array of edit records, not an object and not a
string-encoded array. Send the native array directly:

```json
edits: [
  {{"kind": "update", "path": "relative/file.py", "expected_sha256": "<digest from the latest read>", "replacements": [{{"old_text": "...", "new_text": "..."}}]}}
]
```

Do not repeat the rejected payload. Validation detail: {validation_detail}

## record_shape

Every record carries its own `kind` and `path`, and each kind has exactly one
valid shape:

- `update`: `path` + `expected_sha256` (complete current-file SHA-256) + `replacements`; no `content`
- `create`: `path` + `content`; no `expected_sha256`, no `replacements`
- `delete`: `path` + `expected_sha256` only

First failing field: {field_path}. Remove fields that do not belong to the
record's `kind`; do not move `path` or `expected_sha256` to the arguments
level. Validation detail: {validation_detail}

## digest_format

`expected_sha256` must be the complete current-file SHA-256 digest — exactly
64 hexadecimal characters — obtained from the latest `coding_read_file` (or
`coding_status`) result for that path, not from memory. Never invent,
truncate, pad, or reuse a digest after any successful edit or external change:
re-read the file first, then copy its digest.

First failing field: {field_path}. Validation detail: {validation_detail}

## replacement_shape

`replacements` must be an array of exact replacement objects, each with
non-empty `old_text` and `new_text` string fields. `old_text` must match
exactly once in the original file snapshot.

First failing field: {field_path}. Validation detail: {validation_detail}

## missing_required

A required field is absent: `{field_path}`. Resend the complete arguments
with that field filled per the record shape above. Validation detail:
{validation_detail}

## unknown_or_other

The edit was rejected before any write because of invalid parameters at
`{field_path}`. Resend the complete corrected arguments; the previous call had
no effect. Validation detail: {validation_detail}
