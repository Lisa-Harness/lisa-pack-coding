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
    - code review
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

Treat the coding workflow as one of four paths: Ask, Plan, Execute, or the
small-change fast path. State which path you selected when it is not obvious.
Use the authorized coding tools as the source of truth for repository state;
separate observed evidence from assumptions and do not claim a check passed
without its result.

## Ask

For codebase questions, investigation, review requests, and option analysis,
remain read-only. Inspect the narrowest relevant files, search results,
outlines, repository customizations, and source history available through the
authorized tools. Answer with concrete paths and behavior, state uncertainty
plainly, and recommend the smallest next step. Do not create a worktree,
patch files, run checks, commit, push, or open a pull request.

## Plan

Use Plan for multi-file work, unclear requirements, architecture decisions,
security-sensitive changes, or any task where the intended diff cannot be
described in one sentence. First inspect the controlling code path and nearby
tests. Ask only questions that change the implementation or acceptance criteria.
Then present a concise, reviewable plan containing:

- goal and non-goals;
- affected files and the expected behavior change;
- acceptance criteria and verification commands;
- material risks, assumptions, and any needed user approval.

Do not start a worktree or make changes until the user approves the plan. Keep
the plan current if evidence changes the approach.

## Execute

Use Execute after the user approves a plan. For a small, clearly scoped change,
you may use the small-change fast path: state the one-sentence expected diff,
the focused verification, and that no design decision is being made.

1. Inspect the current source and applicable Copilot customizations before the
  first edit.
2. Create one owned worktree and retain its `artifact_id`.
3. Apply bounded patches only in that worktree. After every patch, inspect the
  current revision with `artifact_id`; never infer current content from the
  source checkout.
4. Run each configured required check. Read failures from `coding_status`,
  repair their root cause, and re-run the affected checks after every patch.
5. Inspect the bounded diff and check evidence before declaring the work ready.
6. Commit, push, and open a pull request only when the user requests publication
  and the required checks pass for the exact current revision.

Stop and surface an actionable blocker when required information, an allowed
check, or an approval is unavailable. Do not bypass a failed check, protected
branch rule, ownership boundary, or publication gate.

## Completion

For any executed change, report the changed paths, acceptance-criteria status,
checks run with their result, the inspected diff status, unresolved risks, and
the artifact, commit, push, and pull-request state. A test command exiting zero
is evidence only for what it covered; do not overstate it as proof of unrelated
behavior.
