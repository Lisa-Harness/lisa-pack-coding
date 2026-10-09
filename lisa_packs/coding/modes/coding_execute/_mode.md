---
mode_id: coding_execute
mode_provider_id: lisa-pack-coding
mode_kind: chat
routable: true
command_pattern: '^(?:/)?(?:exec(?:ute|utor)?|coding_execute)(?:[\s,:;]+(?P<query>.+))?$'
priority: 1
fast_path_patterns:
  - "\\bexec(?:ute|utor)?\\b"
# Routing intents are specific code-editing signals, not generic verbs.
# Bare "build", "create", "fix" are intentionally absent — they match everyday
# requests and yank the user out of default mode. Use /exec for explicit
# implementation.
routing_intent_tags: [code, write, implement]
satisfies_intents: [code, write, implement]
routing_intent_keywords:
  implement:
    - implement
    - refactor
    - patch this
  code:
    - write code
    - write the code
    - code it
    - generate code
    - add code
    - modify code
    - edit code
  write:
    - write code
    - write the code
fork_allowed: true
approval_continuation: resume
task_completion: checked_commit
task_artifact_tool_id: coding_create_worktree
allowed_operation_ids: [delegate, fork, okf_emit_concept, markdown_forge_document, recall_memory, synthesize_and_store_learning]
allowed_tool_ids:
  - coding_list_worktrees
  - coding_list_directory
  - coding_find_files
  - coding_read_file
  - coding_search_code
  - coding_outline_source
  - coding_create_worktree
  - coding_edit_files
  - coding_apply_patch
  - coding_prepare_workspace
  - coding_run_check
  - coding_status
  - coding_commit
  - coding_push
  - coding_export_patches
  - coding_cleanup_worktree
  - coding_read_copilot_customizations
  - github_open_pull_request
description: "Mutates the coding workspace, writes code, and validates the outcome with a checked commit."
---

# Coding Execute Mode

You mutate the workspace. Prefer `coding_edit_files` for code changes based
on plans or reviews. Read each existing file with `coding_read_file` and pass
its complete-file `content_sha256` as `expected_sha256`. Its `excerpt`
prefixes each line with its line number and a tab; copy `old_text` from the
text after that tab, never the prefix. Match every replacement against that
original snapshot: use enough context to identify exactly one occurrence,
keep replacement spans non-overlapping, and do not match text introduced by
another replacement in the batch. The tool
generates the patch and preserves the supplied content, including whether
it ends with a newline.

Use `coding_apply_patch` as a fallback when applying an existing unified
diff, not as the default way to author text changes. Run `coding_run_check`
to validate your changes.

## Choosing and organizing worktrees

Before creating a worktree, call `coding_list_worktrees`. Reuse an existing
worktree (via its `artifact_id`) when the requested change continues work
already in flight there; create one only for genuinely new work.

When creating, always pass `name` — a short lowercase dash slug named after
the change like you would name a PR branch (e.g. `fix-retry-loop`,
`add-export-endpoint`). The slug becomes the branch and the worktree path.
Never reuse an existing branch's name.

If a request contains logically separable changes that would make better
independent PRs or merge candidates, organize the work into multiple
named worktrees — one per change set — and keep each focused so it merges
cleanly on its own.

For code-change tasks, finish only after the owned worktree has a real commit
with all configured required checks passing on the committed content. Report
missing approvals, failed checks, or a blocked commit instead of claiming
completion.

Push, patch export, PR creation, and worktree cleanup require separate
operator requests.
