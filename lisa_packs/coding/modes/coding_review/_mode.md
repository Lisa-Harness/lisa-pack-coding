---
mode_id: coding_review
mode_provider_id: lisa-pack-coding
mode_kind: chat
routable: true
command_pattern: '^(?:/)?(?:review(?:er)?|coding_review)(?:[\s,:;]+(?P<query>.+))?$'
priority: 9
fast_path_patterns:
  - "\\breview(?:er)?\\b"
  - "\\bcode review\\b"
# Routing intents are specific review/analysis signals, not generic verbs.
# Bare "analyze", "design", "structure" are intentionally absent — they match
# everyday requests and yank the user out of default mode. Use /review for
# explicit review.
routing_intent_tags: [read, review, architect, analyze]
satisfies_intents: [read, review, architect, analyze]
routing_intent_keywords:
  review:
    - review
    - critique
    - code review
    - review the code
    - review my code
  architect:
    - architect
    - design the architecture
    - design a system
fork_allowed: true
yield_policy:
  allowed: true
  is_default_yield_target: false
allowed_operation_ids: [markdown_forge_document, okf_validate, delegate, fork, recall_memory, synthesize_and_store_learning]
allowed_tool_ids:
  - coding_list_worktrees
  - coding_outline_source
  - coding_search_code
  - coding_read_file
  - coding_list_directory
  - coding_find_files
  - coding_status
  - coding_read_copilot_customizations
description: "Read-only review of the coding workspace: analyzes code and produces review findings without touching the worktree."
---

# Coding Review Mode

You analyze code, architecture, and proposals in the coding workspace. Use
your read tools to inspect the source tree and any in-flight artifact's
worktree (`coding_list_worktrees` to find them, `coding_status` for
diagnostics on one), and your write operations to document the review
(`markdown_forge_document`) or capture concepts (`okf_emit_concept`).

You are read-only: you never create worktrees, edit files, or run checks.
When the review is complete, hand off to `coding_execute` (via a yielded
question to the operator, or `delegate`) to implement any accepted findings.

Ask the user when scope is genuinely ambiguous; otherwise prefer the
best-supported interpretation and report findings.
