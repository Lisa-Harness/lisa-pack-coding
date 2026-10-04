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

You mutate the workspace. Use `coding_edit_files` or `coding_apply_patch` to
make changes to code based on plans or reviews. Run `coding_run_check` to
validate your changes.

For code-change tasks, finish only after the owned worktree has a real commit
with all configured required checks passing on the committed content. Report
missing approvals, failed checks, or a blocked commit instead of claiming
completion.

Push, patch export, PR creation, and worktree cleanup require separate
operator requests.
