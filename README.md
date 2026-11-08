# lisa-pack-coding

Provider-neutral **local coding workflow** for Lisa. This pack owns the unit of
work: an isolated Git worktree plus a durable, identity-bound change artifact.
It is where the model *makes* a change — read the source, propose a patch,
check it, commit it — under a policy the user sets and the host enforces.

> Sandboxing (bubblewrap containment) comes from the sibling pack
> [`lisa-pack-bwrap`](../bwrap/README.md); an interactive shell and approvals
> come from [`lisa-pack-bash`](../bash/README.md). This pack composes through
> them; configured checks require an authorized bwrap execution floor.
> Publication (push and pull request) is opt-in and handled through this pack
> and [`lisa-pack-github`](../github/README.md), respectively.

**Review** here means auditing the security and ownership rules below against
[workflow.py](lisa_packs/coding/workflow.py) and the tool bodies. **Modify**
means changing a tool, a bound, or the artifact lifecycle — the README flags
the controlling module for each.

## What the pack provides

Eighteen tools, three modes, plus two host commands.

**Modes** (added 2026-10-04, replacing the executor/reviewer personas that used
to live in `lisa-pack-personas`):

| Mode | Tools | Role |
| --- | --- | --- |
| `coding_design` | read tools | Design a change: research the codebase like `researcher` reads the web, then forge a durable design doc. No worktrees, no edits. |
| `coding_review` | read tools | Read-only review of code/architecture; hand off findings. |
| `coding_execute` | full lifecycle | Implement: create worktree → edit/patch → run checks → checked commit (`task_completion: checked_commit`). |

`coding_design` is the entry point for "how should we build X" — it produces the
artifact `coding_execute` consumes, closing the design→review→execute pipeline
inside one pack.

**Tools** in two families:

### Git-workflow tools (the artifact lifecycle)

All take `artifact_id` (except `create_worktree`) and operate **only** on an
owned worktree, never the source checkout. The read-only Git inspection tools can
target the source workspace or an owned worktree.

| Tool | Effect | Approval | Role |
| --- | --- | --- | --- |
| `coding_create_worktree` | write | ✓ | Mint a task-bound artifact + `git worktree add` an isolated branch at the source `HEAD` (or at `import_commit`'s parent when given). Pass `name` (e.g. `auth-refactor`) for the branch `<prefix><name>` and worktree `<worktree-dir>/<name>`; without one a short random id is used. Name clashes and protected slugs fail fast. Worktree lives **outside** the source tree, enforced. |
| `coding_list_worktrees` | read | — | List the session's owned worktrees for this workspace (branch, path, `artifact_id`, status, revision, summary) so the model can name, find, and reuse them. |
| `coding_edit_files` | write | ✓ | Preferred editor: apply exact, hash-guarded replacements, creates, or deletes as one revision. Replacements match the original snapshot and must not overlap. The tool generates and prevalidates the Git patch, preserving supplied content and EOF newlines. |
| `coding_apply_patch` | write | ✓ | Fallback for existing unified diffs: prevalidate (`git apply --check`) and apply a bounded patch. Bumps `revision`, records `changed_paths`, **clears `checks`** (stale results are invalidated). |
| `coding_import_commits` | write | ✓ | Replay one already-merged upstream commit onto the owned worktree: the worktree must be created with `import_commit` (base = that commit's parent), the commit's diff applies as one patch revision, and `checks` are cleared. Rejects re-import before a commit/export, base drift, and empty commits. |
| `coding_prepare_workspace` | write | ✓ | Run the session-configured preparation command (an argument vector in `preparation_recipes`, exactly like a check's `argv`) in the owned sandbox, certify the prepared Python interpreter and/or Node packages, and record preparation evidence. Checks refuse to run until preparation is current. |
| `coding_run_check` | write | ✓ | Run one named check through the bwrap owner's direct-write sandbox floor. Result records the current revision and candidate-tree hash; no host fallback. A `verify` check that rewrites files fails and its edits are reverted; a `fix` check's edits become a new revision (like a patch) and stale the other checks. |
| `coding_commit` | write | ✓ | Stage commit candidates and commit only after required checks passed for this revision and tree, on the owned unprotected branch. The message may be any length and span multiple lines. Committing clears pending `imported_commits` state (the imported content is carried by the commit). |
| `coding_history` | write | ✓ | Slice and dice the owned branch's committed history: `tag`/`tag_delete` (owned commits only), `restore` (rewind to any existing tag's commit, including `<branch_prefix>ckpt/` checkpoints), `reword`, `squash` (fold owned commits into one; `target` keeps everything at or below it as a parent), or `rebase` (`--onto` a new base given as any Git revision — SHA or branch name — auto-abort on conflict; use it as soon as the source tip moves past the artifact base). Every rewrite first tags a checkpoint; a rewrite that leaves the tree byte-identical keeps recorded checks, any content change clears them. Refuses rewrites of already-pushed commits; protected branches and the source checkout are untouchable. |
| `coding_push` | external_write | ✓ | Push the checked feature branch upstream (`push_remote`). **No force option exists.** A successful push freezes the branch's history (rewrites and rewinds past the pushed revision are refused), so it prunes the artifact's now-dead `<branch_prefix>ckpt/` checkpoint tags and reports them as `pruned_checkpoint_tags`. |
| `coding_export_patches` | write | ✓ | Write `base_sha..commit_sha` as a `git format-patch --base` series to `<patch_export_dir or source-parent/.lisa-patches>/<id>/` (outside the source tree, enforced) and return the `git am -3` command. Re-export replaces the prior series; works after cleanup. |
| `coding_cleanup_worktree` | write | ✓ | Remove a **clean** owned worktree (`git worktree remove`, no force). Idempotent: a worktree that was already removed (e.g. deleted by hand after the model was told so) is recorded `cleaned`, not an error. Cleanup also ends the branch, so it prunes the artifact's `<branch_prefix>ckpt/` checkpoint tags from the source root (works after worktree loss) and reports them as `pruned_checkpoint_tags`. |
| `coding_status` | read | — | Report git status/diff + check state for an artifact without mutating it, plus the source checkout's current tip (`source_head_sha`, `source_head_branch`) beside the artifact's `base_sha` with a `source_ahead_of_base` flag — so base drift is visible and the worktree can be rebased onto the source tip promptly (`coding_history` `op=rebase`). |

### Read tools (source workspace or owned worktree)

Bounded, path-validated, non-following of symlinks, with an exclusion list
(`.git`, `.venv`, `node_modules`, `build`, `dist`, cache dirs). All are
`effect: read` with per-turn call caps. Task-bound reads default to the owned
worktree and reread mutable content; source reads remain available explicitly.
Complete valid UTF-8 files return `content_sha256` and `editable: true`, even
when only a line window is shown. Pass that whole-file hash as
`expected_sha256` to `coding_edit_files`. Truncated or invalid UTF-8 reads are
not edit-ready and return no hash.

For updates, every `old_text` must occur exactly once in the original file
snapshot, including whitespace. Replacements are independent and their order
does not matter: they cannot overlap or refer to text inserted by another
replacement. After a stale-hash rejection, reread the file and rebuild the
edits against the new snapshot. No fuzzy matching or newline normalization is
performed. All files are validated before application, and an inapplicable
hunk rejects the whole Git patch; this is not a crash-safe transaction across
filesystem writes and artifact persistence.

Calls that mutate the same artifact must be serialized by the caller. The
pack does not hold a shared lifecycle lock across edit, check, and commit
operations. Concurrent edits can overwrite artifact metadata, and a failed
artifact save can leave already-applied file changes unrecorded; inspect and
reconcile the worktree and artifact before retrying in either case.

| Tool | Effect | Cap | Role |
| --- | --- | --- | --- |
| `coding_list_directory` | read | 50/turn | Bounded directory listing. |
| `coding_find_files` | read | 50/turn | Enumerate non-generated source files. |
| `coding_read_file` | read | 50/turn | Byte- and line-bounded excerpt, one `<line>\t<text>` row per line (binary rejected). |
| `coding_git_diff` | read | 30/turn | Compare branches via merge-base, exact trees, a commit against its first parent, or staged/unstaged/combined tracked worktree changes. Working-tree comparisons separately list paginated untracked paths; patches are complete per file and byte-bounded. |
| `coding_git_log` | read | 30/turn | Paginated commit history at a revision or within a base/head range, optionally path-filtered. |
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
list_worktrees ─▶ create_worktree ─▶ edit_files ─▶ run_check ─▶ commit ─┬▶ push ─▶ (github pack opens PR)
 (reuse or name)   (`name` param)      (revision +1)   (at this    (all required │
                                                         revision)   checks green) └▶ export_patches ─▶ git am -3 (your checkout)
```

Create a worktree anchored at an upstream commit with the optional
`import_commit` parameter (base = that commit's parent); the companion
`coding_import_commits` then replays that one commit's diff as an ordinary
patch revision — the whole verify → commit → push → PR lifecycle carries the
replayed content like any fresh change (e.g. one PR per already-merged commit).

`coding_create_worktree` accepts an optional model-chosen `name` that becomes
the branch (`<prefix><name>`, e.g. `lisa/auth-refactor`) and the worktree
directory (`<worktree-dir>/<name>`), so related work can be found, reused,
and organized into cleanly-mergeable change sets. `coding_list_worktrees`
lists owned worktrees (branch, status, summary, `artifact_id`) so a session
can pick one back up instead of minting a new worktree per turn.

To integrate locally without GitHub, ask for `coding_export_patches` after the
commit, then in your own checkout run the returned command, e.g.
`git am -3 ~/Code/.lisa-patches/<id>/*.patch`. The series keeps authorship,
messages, and a `base-commit:` trailer, so `-3` can fall back to a three-way
merge if your branch has moved on.

### Rebasing onto the source tip

A worktree branches at the source `HEAD` frozen at creation; upstream commits
keep landing on the default branch and drift accumulates quietly. The loop
that keeps an owned branch mergeable is **check drift → rebase early**:
`coding_status` reports the source checkout's current tip beside the artifact
base, and when the tip has moved past the base the model is prompted to rebase
(`coding_history` with `op=rebase` and the tip as `target` — a SHA or a branch
name; both resolve to the canonical commit). Rebasing replays the owned
commits with `--empty=drop`, aborts cleanly on conflict, checkpoints the old
head first, and refuses to rewrite pushed commits — so rebasing early is safe
and cheap, exactly when reconciling with upstream is easiest.

Every artifact is a **`WorkProduct`** (`sdk.workproducts`) carrying a six-field
host identity — `workspace_id`, `user_id`, `user_slug`, `surface_type`,
`surface_id`, `session_plan_digest`, plus the `task_id` of the task currently
responsible for it. Every tool that advances an artifact (edit, patch, prepare,
check, commit, push — never a read) claims it for the calling task via
`WorkProduct.claim_for_task`, so a worktree started under one task and finished
under a later one counts toward the later task's completion. Load-time
ownership is checked via the shared canonical rule
[`artifact.belongs_to(ctx)`](lisa_packs/coding/workflow.py)
(`artifact_owner_matches`), so an artifact from
another user/workspace/session simply **won't load** — this is the seam that
lets the github pack reuse the exact same match without drift.
The host also checks the task ID, actual Git commit and changed paths, required
check-tree hashes, and a clean worktree before the executor can finish.

The gating rules, each backed by a guard in
[workflow.py](lisa_packs/coding/workflow.py):

- **Owned worktree, outside the source** — `artifact_worktree()` resolves the
  recorded `worktree_path`, requires `source_root` to match the configured
  root, and rejects any worktree *inside* the source tree.
- **Owned, unprotected branch** — `artifact_branch_is_allowed()` requires the
  branch to start with `branch_prefix` (default `lisa/`) and not be in
  `protected_branches` (default `main`, `master`).
- **Checks green on this tree** — `artifact_has_current_required_checks()`
  requires every required check to be `passed` at the current revision and
  candidate Git tree. Patching clears old checks; changing tracked or
  untracked candidates after a check invalidates it before commit.
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
the pack's `git_timeout_seconds`. Lifecycle mutations happen only in the **owned
worktree**; Git inspection is read-only and can target the configured source
workspace or an owned worktree. Review operations never fetch or check out
revisions.

### `run_check` requires bwrap

The check argv runs through the bwrap owner's delegated `bwrap_run` binding.
[run_check.py](lisa_packs/coding/tools/run_check/run_check.py) requires a floor
with `execution: sandbox`, `persistence: direct-write`, and an explicit `rw`
grant covering the owned worktree. Missing authorization, a degraded sandbox,
or a changed candidate tree fails the check without running it on the host.

### Sandbox floor contribution (`lisa.extensions.sandbox_floors`)

The worktree parent is a **runtime-derived** path
(`resolve_worktree_parent`: explicit `worktree_dir`, else
`<data_dir>/git-worktrees/<workspace>`) — the harness cannot know it when
hand-authoring the session floor. The **language-platform cache roots**
are the same class of need: the toolchain — not the harness — sets the
location (uv: `~/.cache/uv`; Go: `~/.cache/go-build` + `~/go/pkg/mod`;
npm: `~/.npm`; pnpm: `~/.local/share/pnpm/store` + `~/.cache/pnpm`; yarn:
`~/.yarn/berry/cache` + `~/.cache/yarn`; bun: `~/.bun/install/cache`;
the Go-era floor carried the Go pair by hand). [sandbox.py](lisa_packs/coding/sandbox.py)
declares both through the `lisa.extensions.sandbox_floors` entry point:

```python
def floor_contribution(pack_config, context):
    config = CodingPackConfig.model_validate(dict(pack_config))
    worktree_parent = config.resolve_worktree_parent(
        data_dir=str(context.get("data_dir") or ""),
        source_root=config.resolve_workspace_root(),
    ).resolve()
    return {
        "execution": "sandbox",
        "persistence": "direct-write",
        "paths": {
            str(worktree_parent): "rw",
            str(workspace): "rw",
            # + cache roots derived from the configured checks/recipes
        },
    }
```

The runtime merges this exclusively onto the harness-authored
`lisa-pack-bwrap` floor at session assembly (see the
[harness pack authoring guide](https://github.com/Lisa-Harness/lisa/blob/main/docs/PACK_AUTHORING_GUIDE.md)
and `kernel.sandbox_floor`), so the floor and the actual worktree location
can never drift apart — the failure mode behind the 2026-10-14
`coding_run_check` abort ("bwrap sandbox floor does not permit worktree
writes" after the data-dir switch). The contribution is derived from the
same `resolve_worktree_parent` the worktree tools use, so resolution changes
flow into the floor automatically. Cache roots are detected from the
*configured* check/recipe argv by whole-token match — a `uv` argv contributes
`~/.cache/uv`, a `pnpm` argv contributes `~/.local/share/pnpm/store` +
`~/.cache/pnpm` (so `pnpm` never matches an `npm` test, and
`env FOO=1 pnpm install` still counts) — and only when they exist on the
host: floor `rw` binds have no existence filter, so a missing cache
directory contributes nothing rather than aborting every run. The sandbox
child env passes `HOME` and no `*_CACHE_DIR` overrides, which is why these
are the right paths to bind: each tool derives that location from `HOME`
alone, so it resolves identically inside and outside the sandbox. A pnpm
session needs the store specifically — pnpm materializes every package from
its content store by hard link or copy, so an unwritable store fails the
install outright (`ERR_PNPM_STORE_DIR_OPEN_OPERATION_LOCK`, os error 30)
while a writable one that is *not* the host default silently relocates the
store into the worktree. The bwrap owner normalizes
`TMPDIR` to the namespace's writable `/tmp` (`--tmpfs /tmp`), so temp files
never resolve through a masked or read-only host path: a preparation recipe
needs no `env TMPDIR=` prefix. Network is deliberately *not* contributed:
whether a preparation recipe needs network is toolchain-dependent; a recipe
that needs it under a `deny` floor fails at recipe time with a precise cause.

## Configuration

Typed, frozen, `extra="forbid"` — [config.py](lisa_packs/coding/config.py)
`CodingPackConfig`. Read from the harness-owned session
`resources.packs.configuration` (`ctx.pack_config`); **model tool parameters
cannot forge it.** A `model_validator` enforces unique check ids, resolvable
`required_check_ids`, and a `branch_prefix` that isn't empty/protected.

Key keys (with defaults): `workspace_root` (**required**, must be a Git
worktree — resolved via the shared `sdk.workspace.resolve_workspace_root`,
strict `require_git=True`), `checks` (tuple of `{id, argv, timeout_seconds, kind}`,
where `kind` is `verify` (default) or `fix`),
`required_check_ids`, `branch_prefix` (`lisa/`), `protected_branches`
(`main`,`master`), `push_remote` (`origin`), `patch_export_dir` (empty →
`<source-parent>/.lisa-patches`; must be absolute and outside the source),
`worktree_dir` (empty → `<data-dir>/git-worktrees/<workspace-name>` from the
host-injected data dir; must be absolute; the resolved worktree directory must
stay outside the source), `preparation_recipes` (tuple of the recipe fields
documented above), and the byte/line/result bounds
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
      - lisa-pack-bwrap
    required:
      - lisa-pack-coding
    configuration:
      lisa-pack-coding:
        workspace_root: /home/you/Code/my-project
        branch_prefix: lisa/
        protected_branches: [main, master]
        push_remote: origin
        checks:
          - id: format
            argv: ["uv", "run", "--no-sync", "--no-cache", "python", "-m", "ruff", "format", "."]
            kind: fix  # rewrites files; its edits become a new revision
          - id: unit
            argv: ["uv", "run", "--no-sync", "--no-cache", "python", "-m", "pytest", "-q"]
            timeout_seconds: 300
          - id: type
            argv: ["uv", "run", "--no-sync", "--no-cache", "python", "-m", "mypy", "src"]
            timeout_seconds: 180
        required_check_ids: [unit, type]
      lisa-pack-bwrap:
        sandbox:
          floor:
            execution: sandbox
            persistence: direct-write
            network: deny
            paths:
              # Worktree parent (default <data-dir>/git-worktrees/<workspace-name>
              # unless worktree_dir is set) — grant rw so checks can write there.
              /home/you/.lisa/git-worktrees: rw
              /home/you/yourcode: rw
routing:
  # Optional: route natural-language shell intent elsewhere; this pack is
  # tool-only and has no start_mode.
---
```

With this plan, a change is gated: `coding_apply_patch` bumps the revision and
clears check results; both `unit` and `type` must re-pass on the candidate tree
or `coding_commit` returns an error. Address the interpreter with the
worktree-owned toolchain, e.g.
`uv run --no-sync --no-cache …` — the bounded executor does no shell PATH
lookups for Python and a bare `python` would run whatever the sandbox image
provides (or fail; `uv` itself is resolved on PATH). `--no-sync` keeps
checks pure consumers of the prepared venv (staleness is the pack's gate,
not uv's). `--no-cache` keeps the run independent of the uv cache's
existence and writability: the pack's floor contribution grants
`~/.cache/uv` when the configured toolchain names `uv` and the root exists
([sandbox.py](lisa_packs/coding/sandbox.py)), but checks must also survive
floors that grant none — test harnesses, hand-authored floors, cold
machines. For `uv sync` (preparation) keep the cache: installing is its job.

### With preparation (Python or Node)

`checks` alone assume the workspace is already installable. A
`preparation_recipes` entry makes `coding_prepare_workspace` run one finite,
sandboxed command and record evidence; `coding_run_check` and `coding_commit`
refuse to proceed until that evidence matches the current recipe and
manifests. The pack owns the mechanics (containment, provenance probes,
baseline checks); the session owns the toolchain.

| Field | Meaning |
| --- | --- |
| `prepare_argv` | The install command, a session-authored vector like a check's. |
| `manifest_paths` | Worktree-relative lockfiles whose digest makes preparation stale when edited (`package.json`, `pnpm-lock.yaml`, `uv.lock`, …). |
| `project_modules` | Python import names that must resolve from the worktree `.venv`. Enables the interpreter probe. |
| `node_packages` | Node package names that must resolve from the worktree `node_modules`. Enables the Node probe. |
| `ignored_paths` | Extra runtime dirs preparation creates that must be Git-ignored. `.venv/` and `node_modules/` are derived from the recipe's toolchain and need not be listed. |
| `baseline_check_ids` | Checks to run immediately after preparing, recorded as baseline evidence. |

`manifest_paths` and `ignored_paths` must be worktree-relative with no `..`.
Before running the command, preparation requires every derived or declared
runtime directory to be Git-ignored — otherwise the install would land in the
candidate tree hash and get committed. A Python recipe is therefore asked
for `.venv/` and a Node recipe for `node_modules/`, never both.

```yaml
      lisa-pack-coding:
        workspace_root: /home/you/Code/my-ts-app
        checks:
          # `fix` kind: formatter rewrites become a new artifact revision.
          - id: format
            argv: ["pnpm", "exec", "prettier", "--write", "."]
            kind: fix
          - id: unit
            argv: ["pnpm", "run", "test"]
            timeout_seconds: 300
          - id: type
            argv: ["pnpm", "exec", "tsc", "--noEmit"]
            timeout_seconds: 300
        required_check_ids: [unit, type]
        preparation_recipes:
          - id: node-pnpm
            prepare_argv: ["pnpm", "install", "--frozen-lockfile"]
            manifest_paths: ["package.json", "pnpm-lock.yaml"]
            node_packages: ["react"]      # certified to resolve from the worktree
            baseline_check_ids: [unit]
            timeout_seconds: 900
```

Two Node specifics worth knowing:

- `pnpm exec` resolves binaries from the worktree's own `node_modules/.bin`
  (the sandbox `cwd` is the worktree), so checks need no interpreter-path
  dance: unlike Python, there is no `uv run` equivalent to remember.
- The pnpm content store must be writable in the sandbox. The floor
  contribution binds `~/.local/share/pnpm/store` and `~/.cache/pnpm` whenever
  a pnpm argv is configured, so a networked session installs through the same
  store as the host.

## Language support

Two language platforms are first-class — **Python** (uv) and
**TypeScript/JavaScript** (pnpm, npm, yarn, bun). Both go through the same
session-owned `checks` + `preparation_recipes` contract, so nothing in the
toolchain path is Python-shaped any more. Support is per-tool, uniform across
the source workspace and owned worktrees:

- **Read / list / find / search — language-agnostic.**
  `coding_list_directory`, `coding_find_files`, `coding_read_file`,
  `coding_search_code`, `coding_git_diff`, and `coding_git_log` inspect the
  tree without language-specific parsing. `coding_read_file` accepts an
  optional `revision` to read a historical Git blob; historical reads are
  never edit-ready. `coding_git_diff` separates merge-base branch comparisons
  from exact tree comparisons and uses immutable SHA values in its result.
  Search runs a fixed
  `rg --json --no-heading --no-config` invocation, so any file ripgrep matches
  is searchable regardless of language. `read_file` returns any UTF-8 text
  (binary files are rejected). No per-language logic.

- **`coding_outline_source` — language-aware, four languages.** Dispatched on
  file extension in
  [outline_source.py](lisa_packs/coding/tools/outline_source/outline_source.py):
  - **Python** (`.py`) — *(active)* parsed with the real `ast` module: top-level classes,
    functions, and methods, with an `async` flag and exact line numbers.
  - **TypeScript / JavaScript** (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`)
    — regex-based: `class`/`function`/`interface`/`type` declarations and
    arrow-function `const/let/var` assignments (line-accurate, no full parse).
  - **Go** (`.go`) — *(active)* regex-based, exploiting gofmt's column-0 guarantee:
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
pip install -e ./coding          # editable; requires lisa-harness-kernel + lisa-harness-sdk, Python ≥ 3.11
```

```bash
cd coding
/home/adam/Code/lisa/.venv/bin/python -m pytest -p no:cacheprovider
```

Markers: `live` (real external signal/provider), `integration` (multi-module,
may need services), `eval` (deterministic tool-workflow regressions, no model
calls; local Git and sandbox I/O — see [tests/evals/](lisa_packs/coding/tests/evals/)). Entry points:
`lisa.extensions.content_roots` → `lisa_packs.coding:get_content_root`, and
`lisa.extensions.host_commands` →
`lisa_packs.coding.copilot_commands:get_host_commands`.

### Repeatable Go workflow tests

With the framework development environment, a sibling `lisa-packs/bwrap`
checkout, Go 1.23+, and bubblewrap available, run:

```bash
cd coding
../../lisa/.venv/bin/python -m pytest \
  lisa_packs/coding/tests/evals/test_worktree_trajectory.py::test_real_go_worktree_repair_trajectory \
  -q -rs -p no:cacheprovider
```

This offline packstest case dispatches the actual coding and bwrap tools:
create, read, structured edit, failing Go test, rejected commit, fresh read,
repair, passing test, commit, push to a disposable local bare remote, and
cleanup. Missing local prerequisites produce an explicit skip, not a mock.

The remembered dnsmasq-web hygiene task has a separate opt-in acceptance case:

```bash
LISA_CODING_GO_PROJECT=/path/to/dnsmasq-web \
  ../../lisa/.venv/bin/python -m pytest \
  lisa_packs/coding/tests/evals/test_worktree_trajectory.py::test_dnsmasq_web_hygiene_workflow \
  -q -s -p no:cacheprovider --basetemp="$(mktemp -d /tmp/dnsmasq-packstest.XXXXXX)"
```

It needs Go 1.23.12, a C compiler, and cached dependencies at `~/go/pkg/mod`
(override with `LISA_CODING_GO_MODULE_CACHE`). Modules are read-only; network
downloads are disabled. Go build and temporary files stay under the new
worktree's ignored `.toolchains/packstest` directory. It runs baseline tests,
structured hygiene edits, scoped formatting, `go vet ./...`, and fresh
`go test -count=1 ./...`, then reports a JSON trajectory and retained worktree.
It never commits, pushes, or takes over an existing Lisa artifact. The source
checkout, including pre-existing changes, is checked for preservation.

These cases certify tool behavior, not model task completion or host approval
replay. The real-project case leaves a new worktree on each run; inspect and
remove only those test-owned worktrees when no longer needed.

### Repeatable Python Preparation Test

With the same framework and bwrap checkouts, uv, and the framework's Python
version available locally, run from `coding`:

```bash
../../lisa/.venv/bin/python -m pytest \
  lisa_packs/coding/tests/evals/test_worktree_trajectory.py::test_real_python_preparation_and_repair_trajectory \
  -q -rs -p no:cacheprovider
```

This dependency-free offline fixture calls `coding_prepare_workspace` through
real bwrap, checks the owned interpreter and import origins (regular modules
through `__file__`; PEP 420 namespace modules through every `__path__` entry —
a workspace whose own source tree is a namespace, like the Lisa workspace's
`sdk`, must prepare), and records baseline evidence. It then exercises
hash-guarded edits, failing checks, blocked commit, repair, passing checks,
commit, and cleanup. A missing ignore rule for `.venv/` must fail before
creating runtime paths. The source checkout is preserved and the
successful run writes a JSON trajectory under pytest's temporary directory.
No model calls or downloads.

### Repeatable pnpm / TypeScript Preparation Test

Same framework and bwrap checkouts, plus `pnpm` (and Node ≥ 22.6 for
`node --test` with built-in TypeScript type stripping) on `PATH`:

```bash
../../lisa/.venv/bin/python -m pytest \
  lisa_packs/coding/tests/evals/test_worktree_trajectory.py -k pnpm \
  -q -rs -p no:cacheprovider
```

This offline fixture installs a vendored `file:` dependency with
`pnpm install --frozen-lockfile --offline`, so it needs no network and no warm
store. It certifies that the declared package resolves from the worktree's own
`node_modules` — including pnpm's symlinked `node_modules/.pnpm` virtual store,
whose real path the probe reports — records baseline `pnpm run test` evidence,
verifies `node_modules` is derived as a required ignore rule (and that an
unignored one fails before the install runs), makes the preparation stale by
editing `pnpm-lock.yaml`, re-prepares, and commits a TypeScript edit while the
installed tree stays out of the commit. The pnpm store and cache are pinned to
writable directories in the fixture, mirroring what the pack's floor
contribution derives for a pnpm argv; without those binds the install fails
with `ERR_PNPM_STORE_DIR_OPEN_OPERATION_LOCK` (os error 30).

The TypeScript e2e scenario (`coding/roadmap-chunk-implement-node`) uses the
same shape against [fixtures/nodesum](lisa_packs/coding/tests/e2e/fixtures/nodesum)
and is judged by `acceptance/nodesum_apply_discount.sh`.
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
- **Checks fail closed without authorized containment.** Missing bwrap,
  insufficient worktree `rw` grants, or degraded sandbox evidence rejects the
  check; no unchecked host execution is substituted.
- **The model can't self-approve.** Approval lives in the host engine keyed off
  a containment fact the *pack reports* but the *host owns*; this pack never
  decides its own gate.
