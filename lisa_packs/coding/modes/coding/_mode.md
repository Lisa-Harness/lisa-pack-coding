---
mode_id: coding
mode_provider_id: lisa-pack-coding
mode_kind: chat
routable: true
priority: 4
command_pattern: '^(?:/)?coding(?:[\s,:;]+(?P<query>.+))?$'
description: "Grounded local coding workflow with inspect, isolate, patch, check, and publish stages."
fast_path_patterns:
  - '\b(edit|change|fix|refactor|implement)\b.*\b(code|file|repo|test)\b'
  - '\b(coding|codebase|worktree|pull request)\b'
routing_intent_tags: [coding, review]
satisfies_intents: [coding, review]
routing_intent_keywords:
  coding:
    - code
    - coding
    - implement
    - refactor
    - fix
    - edit
    - patch
    - worktree
    - codebase
    - pull request
  review:
    - review
    - audit
    - check
    - inspect
    - analyze code
allowed_tool_ids:
  - coding_list_directory
  - coding_find_files
  - coding_read_file
  - coding_search_code
  - coding_outline_source
  - coding_create_worktree
  - coding_apply_patch
  - coding_run_check
  - coding_status
  - coding_commit
  - coding_push
  - coding_cleanup_worktree
  - coding_read_copilot_customizations
  - github_open_pull_request
tools_authoritative: true
---

# Coding
