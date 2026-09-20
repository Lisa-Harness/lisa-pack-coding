# lisa-pack-coding

Provider-neutral **local coding workflow** for Lisa. This pack owns the unit of
work: an isolated Git worktree plus a durable, identity-bound change artifact.
It is where the model *makes* a change — read the source, propose a patch,
check it, commit it — under a policy the user sets and the host enforces.

> Sandboxing (bubblewrap containment) comes from the sibling pack
> [`lisa-pack-bwrap`](../bwrap/README.md); an interactive shell and approvals
> come from [`lisa-pack-bash`](../bash/README.md). This pack composes through
> them but does not require them. Publication (push → pull request) is handed
> to [`lisa-pack-github`](../github/README.md) — by design, this pack stops at
> "push the checked branch."

**Review** here means auditing the security and ownership rules below against
[workflow.py](lisa_packs/coding/workflow.py) and the tool bodies. **Modify**
means changing a tool, a bound, or the artifact lifecycle — the README flags
the controlling module for each.

## What the pack provides

No modes, no operations — a deliberate **tool-only surface** plus two host
commands. (The legacy `coding`/`learn` modes were removed; [test_catalog.py](
lisa_packs/coding/tests/test_catalog.py) asserts this shape so it stays honest.)

Thirteen tools, in two families:

### Git-workflow tools (the artifact lifecycle)

All take `artifact_id` (except `create_worktree`) and operate **only** on an
owned worktree, never the source checkout.

| Tool | Effect | Approval | Role |
| --- | --- | --- | --- |
| `coding_create_worktree` | write | ✓ | Mint an artifact + `git worktree add` an isolated branch `<prefix><artifact_id>` at the source `HEAD`. Worktree lives at `<source>/.lisa-worktrees/<id>` — **outside** the source tree, enforced. |
| `coding_apply_patch` | write | ✓ | Prevalidate (`git apply --check`) and apply a bounded unified diff. Bumps `revision`, records `changed_paths`, **clears `checks`** (stale results are invalidated). |
| `coding_run_check` | write | ✓ | Run one named, session-configured check argv in the worktree. Result recorded at the **current revision** with `passed`/`failed`/`timed_out`. |
| `coding_commit` | write | ✓ | Stage changed paths and commit — **only** when every required check passed at the current revision and the branch is owned + unprotected. No newline in the message. |
| `coding_push` | external_write | ✓ | Push the checked feature branch upstream (`push_remote`). **No force option exists.** |
| `coding_cleanup_worktree` | write | ✓ | Remove a **clean** owned worktree (`git worktree remove`, no force). |
| `coding_status` | read | — | Report git status/diff + check state for an artifact without mutating it. |

### Read tools (source workspace or owned worktree)

Bounded, path-validated, non-following of symlinks, with an exclusion list
(`.git`, `.venv`, `node_modules`, `build`, `dist`, cache dirs). All are
`effect: read` with per-turn call caps and an `abbreviation_template` so a
repeat within one turn returns a one-line cache hit instead of re-reading.

| Tool | Effect | Cap | Role |
| --- | --- | --- | --- |
| `coding_list_directory` | read | 50/turn | Bounded directory listing. |
| `coding_find_files` | read | 50/turn | Enumerate non-generated source files. |
| `coding_read_file` | read | 50/turn | Byte- and line-bounded text excerpt (binary rejected). |
| `coding_search_code` | read | 40/turn | Bounded ripgrep JSON search. |
| `coding_outline_source` | read | 50/turn | Bounded structural outline of a source file. |
| `coding_read_copilot_customizations` | read | 10/turn | Parse `.github/{instructions,agents,prompts,skills}` / `.claude` files into structured front-matter + body. `default_available: false`. |

### Host commands (Copilot customizations)

Registered via the `lisa.extensions.host_commands` entry point
([copilot_commands.py](lisa_packs/coding/copilot_commands.py)):

- `/_copilot_prompt <name> [args]` — inject a discovered `.prompt.md` body as
  context, then hand off to its named target agent.
- `/_copilot_agent <name> [args]` — inject an `.agent.md`'s instructions as
  system context; the model self-regulates tools against the agent's list.

Discovery is **deferred to first invocation** (the runtime isn't available at
startup) and **degrades gracefully** to `(none)` on failure.

## The artifact lifecycle (and the rules that gate it)

The intended pipeline, each step enforced in code:

```text
create_worktree ─▶ apply_patch ─▶ run_check ─▶ commit ─▶ push ─▶ (github pack opens PR)
                  (revision +1)   (at this    (all required  ─┘
                                   revision)   checks green)
```

Every artifact is a **`WorkProduct`** (`sdk.workproducts`) carrying a six-field
host identity — `workspace_id`, `user_id`, `user_slug`, `surface_type`,
`surface_id`, `session_plan_digest`. Load-time ownership is checked via the
shared canonical rule [`artifact.belongs_to(ctx)`](
lisa_packs/coding/workflow.py) (`artifact_owner_matches`), so an artifact from
another user/workspace/session simply **won't load** — this is the seam that
lets the github pack reuse the exact same match without drift.

The gating rules, each backed by a guard in
[workflow.py](lisa_packs/coding/workflow.py):

- **Owned worktree, outside the source** — `artifact_worktree()` resolves the
  recorded `worktree_path`, requires `source_root` to match the configured
  root, and rejects any worktree *inside* the source tree.
- **Owned, unprotected branch** — `artifact_branch_is_allowed()` requires the
  branch to start with `branch_prefix` (default `lisa/`) and not be in
  `protected_branches` (default `main`, `master`).
- **Checks green at this revision** — `artifact_has_current_required_checks()`
  requires each `required_check_ids` entry to be `passed` **and** stamped at
  the artifact's current `revision`. Because `apply_patch` clears `checks`, a
  commit always reflects the *latest* code, not a stale pass.
- **Patch paths are validated** — `patch_paths()` walks every `---`/`+++`
  header of the diff through the same traversal/symlink/exclusion validator
  (`resolve_relative_path`) the read tools use; a patch naming a forbidden
  path is rejected before `git apply` runs.

## The bounded-execution model

No tool ever runs a shell or holds unbounded output.

[workflow.py](lisa_packs/coding/workflow.py) `run_process()` executes an
explicit **argv** via `asyncio.create_subprocess_exec` (no shell expansion),
drains stdout/stderr through `_drain_limited` (retains at most
`max_command_output_bytes`, flags `output_truncated`), and kills on
`timeout_seconds`. `run_git()` is the same with a fixed `git` executable and
the pack's `git_timeout_seconds`. All git work happens in the **owned
worktree**, never the source checkout.

### `run_check` composes through bwrap when present

The riskiest exec — an arbitrary configured check argv — is contained when the
bwrap pack is registered. [run_check.py](
lisa_packs/coding/tools/run_check/run_check.py) `_sandbox_registered()` probes
the tool registry for `bwrap_run`; if present it builds a child
`ToolExecutionContext` and runs the check through `bwrap_run` (read-only
filesystem, the worktree bound writable and set as `cwd`). On any degradation —
pack absent, binary missing, namespaced run failed — it **falls back to host
exec** and stamps the result `sandboxed: false`. Containment is best-effort
and *loud*, never silent.

## Configuration

Typed, frozen, `extra="forbid"` — [config.py](lisa_packs/coding/config.py)
`CodingPackConfig`. Read from the harness-owned session
`resources.packs.configuration` (`ctx.pack_config`); **model tool parameters
cannot forge it.** A `model_validator` enforces unique check ids, resolvable
`required_check_ids`, and a `branch_prefix` that isn't empty/protected.

Key keys (with defaults): `workspace_root` (**required**, must be a Git
worktree — resolved via the shared `sdk.workspace.resolve_workspace_root`,
strict `require_git=True`), `checks` (tuple of `{id, argv, timeout_seconds}`),
`required_check_ids`, `branch_prefix` (`lisa/`), `protected_branches`
(`main`,`master`), `push_remote` (`origin`), and the byte/line/result bounds
(`max_file_bytes` 512 000, `max_read_lines` 400, `max_patch_bytes` 200 000,
`max_command_output_bytes` 100 000, `git_timeout_seconds` 60, …).

The strictest part — **the workspace must be a Git worktree** — is the single
shared `sdk.workspace` rule, not a coding-private one. (The bash/bwrap packs
read the same `workspace_root` key but tolerate a non-git root; coding hard
fails. This asymmetry is deliberate: there's no Git workflow without a Git
workspace.)

## Session-context configuration

The pack is configured from the host session plan — a YAML **v2 session
context** (schema_version: 2). The model never supplies this; it comes from
`resources.packs.configuration`, keyed by pack id, and surfaces to every tool
as `ctx.pack_config` (validated into `CodingPackConfig`).

### Minimal

Include the pack and point it at a Git workspace; everything else uses
defaults.

```yaml
---
schema_version: 2
name: coding
surface:
  type: personal_dm
  id: host-console
  memory_scope: coding
resources:
  packs:
    include:
      - lisa-pack-coding
    configuration:
      lisa-pack-coding:
        workspace_root: /home/you/Code/my-project
---
```

### With checks and gating

Define the finite command vectors `coding_run_check` may run, mark which are
required before `coding_commit`/`coding_push`, and tune the branch policy and
bounds. These `checks` are the *only* argv the pack will execute — the model
cannot run an arbitrary command.

```yaml
---
schema_version: 2
name: coding
surface:
  type: personal_dm
  id: host-console
  memory_scope: coding
resources:
  packs:
    include:
      - lisa-pack-coding
    required:
      - lisa-pack-coding
    configuration:
      lisa-pack-coding:
        workspace_root: /home/you/Code/my-project
        branch_prefix: lisa/
        protected_branches: [main, master]
        push_remote: origin
        checks:
          - id: unit
            argv: ["/home/you/Code/lisa/.venv/bin/python", "-m", "pytest", "-q"]
            timeout_seconds: 300
          - id: type
            argv: ["/home/you/Code/lisa/.venv/bin/python", "-m", "mypy", "src"]
            timeout_seconds: 180
        required_check_ids: [unit, type]
        # Bounds (defaults shown); all are validated mins/maxes in config.py
        max_file_bytes: 512000
        max_read_lines: 400
        max_patch_bytes: 200000
        max_command_output_bytes: 100000
        git_timeout_seconds: 60
routing:
  # Optional: route natural-language shell intent elsewhere; this pack is
  # tool-only and has no start_mode.
---
```

With this plan, a change is gated: `coding_apply_patch` bumps the revision and
clears check results; both `unit` and `type` must re-pass at that revision or
`coding_commit` returns an error. Use absolute interpreter paths in `argv`
(the bounded executor does no shell PATH lookups).

## Language support

Support is per-tool, uniform across the source workspace and owned worktrees:

- **Read / list / find / search — language-agnostic.**
  `coding_list_directory`, `coding_find_files`, `coding_read_file`,
  `coding_search_code` treat the tree as text. Search runs a fixed
  `rg --json --no-heading --no-config` invocation, so any file ripgrep matches
  is searchable regardless of language. `read_file` returns any UTF-8 text
  (binary files are rejected). No per-language logic.

- **`coding_outline_source` — language-aware, four languages.** Dispatched on
  file extension in
  [outline_source.py](lisa_packs/coding/tools/outline_source/outline_source.py):
  - **Python** (`.py`) — parsed with the real `ast` module: top-level classes,
    functions, and methods, with an `async` flag and exact line numbers.
  - **TypeScript / JavaScript** (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`)
    — regex-based: `class`/`function`/`interface`/`type` declarations and
    arrow-function `const/let/var` assignments (line-accurate, no full parse).
  - **Go** (`.go`) — regex-based, exploiting gofmt's column-0 guarantee:
    top-level `func`s (with an optional type-parameter list), methods
    (`func (recv) Name`), and named `struct`/`interface` types. `var`/`const`
    and anonymous funcs are intentionally ignored.
  - **Rust** (`.rs`) — regex-based, keyword-led: `fn` (including
    `pub`/`async`/`unsafe`/`const`/`extern` forms) and `struct`/`enum`/
    `trait`/`union`/`impl`/`mod`/`type` items.
  - **Anything else** → `Unsupported file type for outline extraction.` The
    model should fall back to `coding_read_file`/`coding_search_code` to map an
    unsupported language's structure by hand.
- **`coding_read_copilot_customizations`** — parses Markdown customization
  files (`.prompt.md`, `.agent.md`, `.instructions.md`, skills) into
  structured front-matter + body JSON; it's markup-aware, not a
  programming-language parser.

To extend outline support for a new language, add its suffix to the dispatch
and a `_<lang>_outline()` extractor next to `_python_outline`/`_ts_outline`/
`_go_outline`/`_rust_outline` — no other tool needs to change. The unit tests in
[tests/test_outline_extractors.py](lisa_packs/coding/tests/test_outline_extractors.py)
exercise the extractors directly (no worktree/store plumbing); add a case there
for any new language.

## Where things live (for modification)

| Concern | Module |
| --- | --- |
| Artifact lifecycle, ownership, gating, bounded exec | [workflow.py](lisa_packs/coding/workflow.py) |
| Config schema + validation + workspace resolution | [config.py](lisa_packs/coding/config.py) |
| Path traversal/symlink/exclusion guards | [local_files.py](lisa_packs/coding/local_files.py) |
| One tool per dir (`X/X.py` + `X.tool.yml`) | [tools/](lisa_packs/coding/tools/) |
| Copilot prompt/agent host commands | [copilot_commands.py](lisa_packs/coding/copilot_commands.py) |
| Tool-inventory + shape assertions | [tests/test_catalog.py](lisa_packs/coding/tests/test_catalog.py) |

Each tool pairs a Pydantic `Parameters` schema (validated from
`ctx.parameters`) with an `execute()` returning `ToolResult`, and a
`.tool.yml` declaring `effect`, `approval_required`, and (for reads)
`max_calls_per_turn` + `abbreviation_template`.

## Install & test

```bash
pip install -e ./coding          # editable; requires lisa-kernel + lisa-sdk, Python ≥ 3.11
```

```bash
cd coding
/home/adam/Code/lisa/.venv/bin/python -m pytest -p no:cacheprovider
```

Markers: `live` (real external signal/provider), `integration` (multi-module,
may need services), `eval` (deterministic agent regression evals, no model
calls/I/O — see [tests/evals/](lisa_packs/coding/tests/evals/)). Entry points:
`lisa.extensions.content_roots` → `lisa_packs.coding:get_content_root`, and
`lisa.extensions.host_commands` →
`lisa_packs.coding.copilot_commands:get_host_commands`.

## How this pack fits the group

The four related packs form one pipeline with clean seams — and **no
cross-imports** (the sdk/packs rule); they hand off only through typed code
(artifact, tool registry, shared config key):

- **bwrap** = containment *primitive* (how/where to run safely).
- **bash** = interactive execution *session* + the user-facing *approval
  boundary*.
- **coding** (this) = Git *workflow* + durable *artifact ownership* (the unit
  of work).
- **github** = *publication* of that artifact (push → PR).

`coding_run_check` doesn't import bwrap — it dispatches through the tool
registry to `bwrap_run`. Ownership (`belongs_to`) and the artifact type live in
the SDK so the github pack matches identity identically. The single
`workspace_root` config key is the shared channel; only the strictness differs
per pack.

## Non-goals / sharp edges (deliberate)

- **Two parallel staging models don't interop.** bwrap's overlayfs layers
  (transient, fs-level capture) and this pack's worktrees+artifact (durable,
  Git-level) are separate. An overlay layer **cannot** become a `WorkProduct`;
  a patch verified under tmpfs scratch must be re-applied via
  `coding_apply_patch`. There's no bridge type in the kernel artifact — yet.
- **Checks are contained only when bwrap is installed.** Without it they run on
  the host (stamped `sandboxed: false`). If your threat model requires
  containment, require the bwrap pack rather than trusting every workspace to
  have it.
- **The model can't self-approve.** Approval lives in the host engine keyed off
  a containment fact the *pack reports* but the *host owns*; this pack never
  decides its own gate.
