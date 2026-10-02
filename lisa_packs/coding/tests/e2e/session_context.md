---
schema_version: 2
name: e2e-coding
surface:
  type: personal_dm
  id: host-console
  memory_scope: personal
resources:
  packs:
    configuration:
      lisa-pack-bwrap:
        sandbox:
          floor:
            execution: sandbox
            persistence: direct-write
            network: allow
            device: dev
            ipc: shared
            paths:
              ${WORKSPACE_PARENT}: rw
              /tmp: rw
            tmpfs_budget_bytes: 67108864
      lisa-pack-coding:
        workspace_root: ${WORKSPACE}
        branch_prefix: lisa/
        protected_branches: [main]
        push_remote: origin
        checks:
          - id: test
            argv: ["python3", "-m", "unittest"]
            timeout_seconds: 90
        required_check_ids: [test]
      lisa-pack-core:
        plan_max_tokens: 32768
      lisa-pack-memory:
        memory_distill_max_tokens: 16384
    required:
      - lisa-pack-bash
      - lisa-pack-bwrap
      - lisa-pack-coding
      - lisa-pack-markdown
      - lisa-pack-personas
providers:
  capabilities:
    memory: lisa-pack-memory
  modes:
    default: lisa-pack-memory
  tools:
    web_search: lisa-pack-serper
routing: { from: { pack: lisa-pack-core, session: default } }
execution:
  max_session_tokens: 3000000
  max_completion_tokens: 500000
  max_capability_calls: 300
---
# E2E coding session

You are a running instance of the Lisa host under an automated end-to-end test.
The coding workspace is a small Python project.

## You (Lisa)

You are direct and helpful.
You avoid making assumptions.
You never falsely claim success.
