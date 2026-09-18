---
title: "Plugin Hooks as nx Verbs: Retire the Bash Hook Layer"
id: RDR-215
type: Technical Debt
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-18
accepted_date:
related_issues: []
related_rdrs: [RDR-184, RDR-205]
---

# RDR-215: Plugin Hooks as nx Verbs: Retire the Bash Hook Layer

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside the template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

**Provenance.** On 2026-09-18 a research pass on Windows support (T2
`nexus/windows-support-research-of-record-2026-09-18`, three T3 documents in
`nexus-rdr-research`) found that the plugin's hooks, not its MCP servers,
are what tie nexus to a POSIX shell on the client side. Claude Code runs a
hook's `command` through bash, or through PowerShell on Windows when Git
Bash is absent, and it also offers an exec form (`command` plus `args`)
that spawns a real executable with no shell at all, and an `mcp_tool`
form that calls a tool on an already-connected MCP server with no
process at all. Sam's decision that day: move the hook logic out of bash
and into the `nexus` package.

## Problem Statement

A Claude Code hook is a command the client runs at a lifecycle event
(session start, a tool call, a subagent stopping) and whose exit code and
JSON on stdout can block the event or add context to it. The conexus and
sn plugins declare 28 such hooks in their `hooks.json` files. Of those, 15
are bash scripts totalling about 4,200 lines, and every Python hook is
launched through one more bash script, `_run_python_hook.sh`, whose job is
to find an interpreter that can import `nexus`.

That layer is the plugin's only hard dependency on a POSIX shell. The MCP
servers are Python console scripts; the CLI is a Python console script;
the engine is a native binary. Only the hooks need bash, `awk`, `timeout`,
`stat` and `mktemp` to exist on the host. On Linux and macOS they do. On
native Windows they exist only under Git for Windows, and even there the
scripts assume Unix paths, GNU flag spellings and a `/tmp`.

### Enumerated gaps to close

#### Gap 1: The hook layer is the plugin's only shell dependency

Fifteen bash scripts and one bash launcher stand between Claude Code and
the Python that does the work. The largest, `expectations.sh` at 1,827
lines and `pre_close_verification_hook.sh` at 796, carry real logic:
the RDR-184 dispatch ledger and the review-marker close gate. That logic
has no unit tests of its own beyond what drives the script end to end,
and `expectations.sh` is kept byte-identical in two places by a test
because two consumers source it.

#### Gap 2: Interpreter resolution is re-derived by a shell script

`_run_python_hook.sh` walks a four-rung ladder (`NX_HOOK_PYTHON`, a
checkout venv whose `nexus` is this tree, the installed generation's
`current/bin/python`, then `python3.13`, `python3.12`, `python3`) to find
a Python that imports `nexus`. The installed generation's console
scripts, and the `nx-mcp` server Claude Code already runs from one of
them, are that Python by construction. A hook served from either
inherits the resolution for free; a hook declared as a bash script has
to rebuild it.

#### Gap 3: Hook behaviour cannot run on a host without a POSIX shell

Claude Code's `mcp_tool` form runs a hook as a tool call on a connected
server, and its exec form runs `command` with `args` and no shell, on
every platform, provided `command` is a real executable. A hook layer
built on those two forms runs wherever the conexus wheel installs. The
bash layer runs only where bash and the GNU userland do. Windows support
via WSL2 does not need this gap closed, since WSL2 is Linux; a native
Windows client would, and so would any future host that ships without
bash.

#### Gap 4: Two copies of one shell library

`conexus/hooks/scripts/expectations.sh` and `tests/e2e/lib/expectations.sh`
are held identical by `tests/hooks/test_subagent_stop_hook.py`. A Python
module has one home, one import path and one test file.

## Relationship to Prior RDRs

- **RDR-184** (background-teammate expectations ledger): the ledger lives
  in `expectations.sh`. This RDR moves it to Python and keeps its contract
  (`expectations_expect`, `expectations_census`, `expectations_undeclared`
  and their exit codes 0, 1, 2, 3 as documented in AGENTS.md).
- **RDR-205** (tuple space): the subagent start and stop hooks write
  tuples through `nx` already; the async wrappers around them are bash
  and go with this change.
- **RDR-157 / RDR-161** (distribution, native-only install): unchanged.
  This RDR does not add a Windows build; it removes one of the three
  reasons a native Windows client is not viable (the other two, the PG
  bundle and the bash install scripts, are outside its scope).

## Context

### Background

The hook layer grew one script at a time between 2026-05 and 2026-09.
Early hooks were a few lines of bash. Later ones (`subagent-start.sh` at
399 lines, `subagent-stop.sh` at 290) accumulated JSON assembly in shell,
byte budgets, and calls into `nx` and `bd`. Eight hooks were written in
Python from the start and are launched through `_run_python_hook.sh`;
four already name an `nx` entry point (`nx hook session-start`,
`nx upgrade --auto`, `nx self gc`, `nx-session-end-launcher`), but all
four are declared in shell form, and two of them carry shell logic (`||`
and redirects) in the command string. No entry in either plugin has an
`args` key today (research-7). The direction is already set; this RDR
finishes it.

### Technical Environment

- Claude Code hooks: `command` runs under bash, or PowerShell on Windows
  without Git Bash; `shell` can be set per hook; exec form `command` plus
  `args` spawns without a shell and requires a real executable
  (code.claude.com/docs/en/hooks, read 2026-09-18).
- `nx` is a console script produced by the generation install
  (`src/nexus/_install/shims.sh`); on Windows a `uv tool install` produces
  `nx.exe`.
- Existing `nx hook` verbs: `session-start`, `session-end`,
  `session-end-detach`, `session-end-flush`, `mailbox-arm`,
  `routing-stats` (`src/nexus/commands/hook.py`).
- Hook tests: 36 files under `tests/hooks/`, most driving the scripts as
  subprocesses with a fixture `hooks.json` payload on stdin.
- External commands the bash layer actually calls (research-6, a read of
  every script, not a word count): `python3` for every JSON parse and
  build, `awk` for the ledger, `mkdir`, `rmdir`, `find`, `ln -s` for
  locks and claims, `nx` (`scratch`, `catalog sync`, `catalog
  links-for-file`, `tuple`, `hook session-start`), `bd` (`list`,
  `set-state`), `git` (`status --porcelain`, `rev-parse`), `date -u`,
  `stat` in both BSD and GNU spellings, `mktemp`, `shasum`. No script
  calls `jq`, `timeout`, `uname` or `curl`.

## Research Findings

### Investigation

Inventory at develop tip 62b5fcb90, 2026-09-18:

| Script | Lines | Event | Emits a decision |
| --- | ---: | --- | --- |
| `expectations.sh` (sourced library) | 1,827 | SubagentStart, SubagentStop, PreToolUse Agent | no |
| `pre_close_verification_hook.sh` | 796 | PreToolUse Bash | yes |
| `subagent-start.sh` | 399 | SubagentStart | yes |
| `subagent-stop.sh` | 290 | SubagentStop | yes |
| `agent-dispatch-expect.sh` | 208 | PreToolUse Agent | no |
| `stop_verification_hook.sh` | 111 | Stop | yes |
| `auto-approve-nx-mcp.sh` | 99 | PreToolUse, PermissionRequest | yes |
| `subagent-start-stamp.sh` | 96 | SubagentStart | no |
| `divergence-language-guard.sh` | 80 | PostToolUse Write, Edit | yes |
| `post_compact_hook.sh` | 57 | PostCompact | no |
| `_run_python_hook.sh` | 49 | launcher for 8 Python hooks | n/a |
| `subagent-start-tuple-async.sh` | 38 | SubagentStart | no |
| `subagent-stop-tuple-async.sh` | 24 | SubagentStop | no |
| `sn/mcp-inject.sh` | 79 | SubagentStart | yes |
| `sn/auto-approve-sn-mcp.sh` | 10 | PreToolUse, PermissionRequest | yes |
| `sn/session-start.sh` | 9 | SessionStart | no |

Python hooks already present and launched through the bash launcher:
`preflight.py`, `session_start_hook.py`, `rdr_hook.py`,
`version_lockstep_hook.py`, `stop_failure_hook.py`, `mailbox_drain.py`,
`routing/subagent_git_write_requires_orchestrator.py`,
`routing/phase_review_close_requires_gate.py`, with their helper modules.
Seven of the eight need only a declaration change to exec form once a
verb wraps them. The eighth, `version_lockstep_hook.py`, is the permanent
exception of Approach item 3: it is never wrapped, and it also names the
launcher in its own code (lines 101, 218 and 521) to dispatch its repair
action, so its port changes those three lines and its declaration.

The full contract map, one row per script with stdin fields, stdout
shapes, exit codes, files touched outside the repo and every quoting
site, is T2 `nexus_rdr/215-hook-contract-map` (research-6).

### Key Discoveries

- **Verified** (docs, 2026-09-18): Claude Code's `mcp_tool` hook type
  calls a tool on an already-connected server, for a plugin server under
  the scoped name `plugin:conexus:nexus`, passing `input` values built by
  `${path}` substitution from the hook payload, and reads the tool's text
  output exactly as command-hook stdout. A server that is not connected,
  or a tool that returns an error, is a non-blocking error and the event
  continues. The type is skipped on `SessionStart` at launch and on
  `Setup`, because those fire before the servers exist; a `SessionStart`
  after `/clear` or a compaction runs it. *Source: T2
  `nexus_rdr/215-research-9`.*
- **Verified** (source search, 2026-09-18): the Python hooks import
  `nexus` and must run under the generation's interpreter; a bare Homebrew
  `python3` cannot even log the import failure (`_run_python_hook.sh`
  header, measured 2026-09-08). `nx` resolves that interpreter by
  construction.
- **Verified** (docs, 2026-09-18): Claude Code selects exec form by the
  presence of the `args` key, needs a real executable, and passes `args`
  verbatim, so `{"command": "nx-hook", "args": ["subagent-start"]}` runs
  identically on every platform where the console script is on PATH.
- **Verified** (source search): four hook entries already name an `nx`
  entry point, though all in shell form, and one of them is a separate
  console script (`nx-session-end-launcher`), so the pattern exists in
  the repo.
- **Verified** (source search, 2026-09-18): the bash layer already depends
  on `nx` being on the hook PATH. Eight call sites locate it with
  `command -v nx` and degrade when it is absent; the close gate warns when
  `bd` is missing. A verb-based layer inherits that assumption rather than
  adding one. *Source: T2 `nexus_rdr/215-research-4`.*
- **Verified** (source search, 2026-09-18): every hook exits 0 on every
  path and encodes a block in its JSON body; the one exception is the sn
  session-start script, which inherits `cat`'s exit. `set -e` and
  `pipefail` are absent by design ("must never fail"), so a port needs an
  explicit try-and-swallow boundary per verb to keep that property. The
  ledger's atomic claims are `mkdir` and `ln -s`, which `os.mkdir` and
  `os.symlink` reproduce exactly. *Source: T2
  `nexus_rdr/215-hook-contract-map`.*
- **Verified** (source search, 2026-09-18): `nx-session-end-launcher`
  (`src/nexus/_session_end_launcher.py`) already exists as a console
  script that imports only `os` and `sys` before it forks, built because
  the Click import raced Claude Code's SIGTERM. It is the template for
  `nx-hook` and for the two async tuple wrappers. *Source: research-6.*
- **Verified** (spike, 2026-09-18, dev Mac, mean of five runs): the cost of
  an `nx` invocation is the CLI's import tree, not Python. The generation
  interpreter starts in 0.010 s and imports `nexus` in 0.010 s;
  `nexus.commands.hook` alone imports in 0.080 s; `nexus.cli`, which
  registers 38 command modules eagerly, imports in 0.788 s, and
  `nx --version` takes 0.808 s. A Python hook run through the bash launcher
  costs 0.042 s. The bash auto-approve hook costs 0.096 s. Routing hooks
  through `nx` would therefore add about 0.8 s to every PreToolUse and
  PermissionRequest event. *Source: T2 `nexus_rdr/215-research-5`.*

### Critical Assumptions

- **Assumed**: the `nx-mcp` server is connected by the time the first
  post-session-start hook fires, on every supported install. The doc
  says only that `SessionStart` at launch precedes the servers; whether
  the first `PreToolUse` can race the server's connection is measured in
  Phase 1. A hook that runs before its server is connected is a
  non-blocking error, which for the close gate means fail-open.
- **Assumed**: the installed generation's console scripts are on the
  PATH Claude Code gives `SessionStart` hooks. The bash layer already
  assumes this (research-4). Phase 1 measures it on an app-launched macOS
  Claude Code and in WSL2. A hook that cannot find its generation fails
  loud.
- **Verified, with a design consequence** (research-5): an entry through
  the `nx` console script pays about 0.8 s for the CLI's eager imports.
  The one command-tier script this design keeps, `nx-hook`, imports only
  what the invoked verb needs; the tool tier pays no process at all.
- **Assumed**: the `bd` calls (research-6: `bd list`, `bd set-state`)
  stay subprocess calls from Python. No bead-tool Python API is needed.

## Proposed Solution

Two tiers, chosen by when the event fires. Every hook event that fires
after the session's MCP servers are connected becomes a tool on the
plugin's own `nx-mcp` server, declared as an `mcp_tool` hook: no process,
no shell, no PATH, no interpreter to find, and the hook runs in the
process that already imports `nexus`. The `SessionStart` group, which
fires before the servers exist, and the version-lockstep hook, which must
work on a wheel older than the plugin, are command hooks in exec form.
All logic lives in `src/nexus/hooks/`; both tiers call the same
functions.

### Approach

1. **The tool tier.** Every entry on `PreToolUse`, `PostToolUse`,
   `PermissionRequest`, `UserPromptSubmit`, `SubagentStart`,
   `SubagentStop`, `Stop`, `StopFailure` and `PostCompact` in the conexus
   plugin becomes `{"type": "mcp_tool", "server": "plugin:conexus:nexus",
   "tool": "hook_<name>", "input": {...}}`. The `input` map names the
   payload fields the hook reads (the contract map lists them per script)
   as `${session_id}`, `${tool_input.command}` and so on. The tool returns
   the same decision JSON the script wrote to stdout. That is 16 of the 24
   conexus entries.
2. **The command tier.** Six of the seven `SessionStart` entries (the
   seventh is item 3) become command hooks in exec form on `nx-hook`, a
   new console script beside
   `nx-session-end-launcher` in `pyproject.toml`, whose entry module
   imports `os`, `sys` and `json` and resolves the verb to its module
   lazily. `SessionEnd` keeps `nx-session-end-launcher`, reshaped to exec
   form. No entry names `nx`, because `nx` pays the CLI's 0.8 s import on
   every call (research-5).
3. **The lockstep exception.** `version_lockstep_hook.py` repairs a wheel
   that is behind the plugin, so it can depend on neither tier. It stays a
   stdlib-only script declared as `{"command": "python3", "args":
   ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/version_lockstep_hook.py"]}`, its
   two dispatch lines (218 and 521) run `python3 version_lockstep_action.py`
   directly, which is sound because the action imports only the standard
   library (research-8), and the `_LAUNCHER` assignment at line 101 is
   deleted with them.
4. **One implementation, two entries.** Each bash script becomes a module
   under `src/nexus/hooks/` with one function
   `run(payload: dict | None) -> HookResult`. The tool tier registers it
   on `nx-mcp` under `hook_<name>`; the command tier registers it in
   `nx-hook`'s verb table. Neither entry holds logic.
5. **The ledger.** `expectations.sh` becomes `nexus.hooks.expectations`,
   one module, keeping the file format, the paths, the `mkdir` and
   `ln -s` atomicity primitives, and the documented exit codes. The e2e
   copy and its byte-identity test are deleted. Its consumers are two
   classes: the bash e2e scripts that `source` it call `nx-hook <verb>`
   instead, and the Python test files that shell out to `bash -c
   "source ..."` import the module directly.
6. **The four shell-form `nx` entries.** `nx hook session-start` becomes
   `{"command": "nx-hook", "args": ["session-start"]}`,
   `nx-session-end-launcher` becomes
   `{"command": "nx-session-end-launcher", "args": []}`, and
   `nx upgrade --auto` and `nx self gc`, whose shell logic lives in the
   command string, become `nx-hook upgrade-auto` and `nx-hook self-gc`.
7. **The launcher.** `_run_python_hook.sh` is deleted once no
   `hooks.json` entry, no executable line under `conexus/hooks/scripts/`,
   and no test names it. The tests that do today are retired or inverted
   in the same change: `tests/test_plugin_structure.py`'s
   `test_python_hook_runner_helper_present_and_executable` and
   `test_python_hooks_use_runner_helper` (the latter inspects `command`
   and would pass vacuously under exec form),
   `tests/hooks/test_run_python_hook_runner.py`, and
   `tests/e2e/plugin-lockstep-gate.sh` lines 146 and 235. Docstring and
   prose mentions are updated, not counted.
8. **The sn plugin.** sn ships no Python package and no server of its
   own, and its hook logic already lives in two bundled stdlib scripts
   (`auto_approve_sn_mcp.py`, `worktree_guard.py`) that the bash wrappers
   call with `python3`. Its four entries become exec-form `python3` on
   those scripts, with the session-start section emitted by a third small
   script. sn keeps no dependency on the conexus wheel or server.
9. **Move, then delete.** Each port retargets the script's existing test
   at the new entry with the same payload and the same expected output,
   passes, and only then deletes the script. Byte and timing budgets keep
   their thresholds.

### Technical Design

**The tool tier on `nx-mcp`.** `src/nexus/mcp/hooks.py` registers one
tool per hook module, named `hook_<name>`, with an input schema that
mirrors the module's payload fields. The tool calls `run()` and returns
the decision JSON as its text content; a raised exception is caught at
the tool boundary, logged to the hook log, and returned as empty text
with `isError` false, so the event continues exactly as a bash script
without `set -e` lets it continue today. The two async wrappers become a
daemon thread started inside the server, which replaces the double-fork,
and the Stop hook's `nx catalog sync`, synchronous in the script today
(line 94), moves to a daemon thread as well so a git push never holds
the server; that is a deliberate change, listed under Failure Modes.
The hook tools are visible in the model's tool list, since MCP has no
way to hide a tool; the `hook_` prefix and a one-line description saying
so are the mitigation, and the auto-approve matcher covers them.

**The command tier.** `nx-hook = "nexus.hooks.entry:main"` in
`pyproject.toml`, built like `nx-session-end-launcher`: `os`, `sys` and
`json` before dispatch, the verb's module after. It reads the payload
from stdin (TTY-aware, empty or malformed reads as `None`), calls the
same `run()`, writes the decision JSON to stdout, and exits 0. `nx hook`
keeps its Click verbs for a human at a terminal; no `hooks.json` entry
names it.

**Package.** `src/nexus/hooks/` (the existing `nexus.hooks` module that
`session-start` calls becomes `nexus/hooks/__init__.py`). One module per
retired script. A shared `_io.py` holds the payload reader, the decision
envelope writers (`hookSpecificOutput` with `permissionDecision` or
`additionalContext`, and the top-level `decision` form the stop hooks
use), and the never-fail boundary. `_config.py` resolves
`NX_ORCH_STOP_GUARD` once for the four hooks that read it inline today.

**Contracts.** The contract map (T2 `215-hook-contract-map`) lists each
script's stdin fields, stdout shapes and exit codes; the port reproduces
each byte for byte and the retargeted test asserts them. Two are quoted
outside the tests: the ledger verbs' codes (0 clean, 1 BLINDSPOT, 2
undeclared, 3 no ledger for `undeclared`; 0, 2, 4 for `reconcile`) in
AGENTS.md and the orchestration skill, and the close gate's deny text in
19 files.

**The ledger** (`nexus.hooks.expectations`). A TSV file under
`$XDG_STATE_HOME/nexus/orchestration/<session>.expectations` with
`mkdir` lock directories and `ln -s` credit slots, kept as they are
(`os.mkdir` and `os.symlink` are atomic create-or-fail on Linux, macOS
and WSL2). Verbs: `expect`, `start`, `census`, `undeclared`, `reconcile`,
`archive`, `sweep`.

**Defects fixed in the port.** The close gate's `bd create` deny path
omits `permissionDecisionReason`; the sn auto-approve wrapper swallows a
Python crash with an unconditional `exit 0`; the sn session-start script
has no error boundary. Each gets the shared envelope and boundary.

**Lint.** A test asserts, for every entry in the conexus `hooks.json`,
one of two shapes. Tool tier: `type` is `mcp_tool`, `server` is
`plugin:conexus:nexus`, `tool` starts with `hook_` and names a registered
tool, and the event is not `SessionStart`. Command tier: the entry has an
`args` key, `command` is exactly one of `nx-hook`,
`nx-session-end-launcher`, or `python3` whose sole `args` element ends in
`version_lockstep_hook.py`, and no `command` or `args` element equals
`bash`, `sh` or `nx` or ends in `.sh`; matching is whole-string, so
`nx-hook` is not `nx`. A `SessionStart` entry must be command tier. For
the sn `hooks.json`: every entry has `args`, `command` is exactly
`python3`, and the sole `args` element is a `.py` path under
`${CLAUDE_PLUGIN_ROOT}/hooks/scripts/`.

**Tests.** Each retargeted test keeps its payload fixture and expected
bytes. Tool-tier tests call the registered tool through the server's
in-process dispatch; one integration test drives a real `nx-mcp` over
stdio for one tool. Command-tier tests spawn `nx-hook <verb>`. Two
scripts have no test today (`sn/session-start.sh`, `sn/mcp-inject.sh`);
their ports get one.

### Decision Rationale

- **A tool over a process.** The server already runs, already imports
  `nexus`, and is the installed wheel by construction. A hook served
  there has no spawn cost, no PATH, no interpreter ladder and no
  lock-step failure mode. The command tier exists only where the doc
  says the tool tier cannot run.
- **`nx-hook` over `nx` for the command tier.** `nx` imports 38 command
  modules before it dispatches (research-5). Making `nexus.cli` lazy is
  worth its own bead; this RDR does not depend on it.
- **Fail-open is the existing posture.** A bash hook whose `nx` is
  missing already returns nothing; a tool hook whose server is missing
  returns a non-blocking error. The close gate is fail-open in both
  worlds. Making it fail-closed is a separate decision this RDR does not
  take.
- **Move, do not rewrite.** The scripts encode months of measured
  behaviour; each port carries the existing test across first.

## Alternatives Considered

### Alternative 1: Keep bash, require Git for Windows on native Windows

Pros: zero work. Cons: the scripts assume GNU tools and `/tmp`; Git Bash
is MSYS, and the failure catalogue for that combination is long. It also
leaves Gap 2 and Gap 4 open on every platform.

### Alternative 2: One tier, everything through `nx-hook`

Pros: one declaration shape, no MCP dependency. Cons: a process per hook
event, about 0.05 s each, on every tool call; the lock-step failure mode
for every hook rather than none; the PATH assumption on every event
rather than session start only.

### Alternative 3: The `http` hook type against the engine

Pros: no process either. Cons: the hook logic is Python and the engine is
Java; the engine would proxy to the client or the logic would move
languages. The engine also does not run on a cloud-mode box.

### Alternative 4: Rewrite the hook logic in Node

Pros: Claude Code's own docs use `node` as the exec-form example. Cons:
the hooks talk to T1, T2 and the catalog; a Node port would reimplement
that client or shell out to `nx` for every call.

## Trade-offs

### Consequences

- Both `hooks.json` files change shape. That is a plugin-surface change
  and ships through the drift ledger and a plugin cut or client release;
  the tool tier also requires a wheel whose `nx-mcp` registers the hook
  tools, so the drift ledger entry states the wheel floor.
- 16 conexus hooks stop spawning a process at all; six `SessionStart`
  hooks spawn one `nx-hook` each instead of bash, the lockstep hook
  spawns `python3`, and the four sn hooks spawn `python3` instead of bash.
- The hook tools appear in the model's tool list.
- 4,200 lines of bash leave; roughly the same amount of Python arrives,
  with unit tests per module.

### Risks and Mitigations

- **A hook fires before `nx-mcp` is connected.** Mitigation: Phase 1
  measures the first `PreToolUse` after launch on macOS and WSL2; if it
  can race the connection, that event's entry gets a command-tier twin
  until the server is up.
- **`nx-hook` not on the `SessionStart` PATH.** Mitigation: measured in
  Phase 1 on an app-launched macOS Claude Code and in WSL2; the verb
  prints one line naming the fix when it cannot find its generation.
- **A port changes a refusal text or exit code another file quotes.**
  Mitigation: the retargeted test asserts the exact bytes; a grep for the
  quoted strings runs before each script is deleted.
- **A hook tool blocks the server.** Mitigation: hook tools do no more
  than the script did, and the one long call, the Stop hook's
  `nx catalog sync`, moves off the synchronous path into a daemon thread.

### Failure Modes

- A hook module imports a heavy module at start-up and regresses every
  `SessionStart`. Guard: imports deferred to the branch that needs them.
- A daemon thread (the two async projectors, the catalog sync) outlives
  its work, leaks, or fails silently where the synchronous call would
  have logged. Guard: `test_subagent_tuple_async_wrappers.py` retargeted
  to assert completion; the thread logs its outcome to the hook log.
- The model calls a `hook_` tool by itself. Guard: the tools do what the
  hook did, reads and ledger appends the model can already reach through
  `nx`; a stray append surfaces in the census rather than hiding; the
  description says the tool is a hook entry.

## Implementation Plan

No implementation starts before this RDR is accepted.

### Minimum Viable Validation

Two first ports: `auto-approve-nx-mcp.sh` as `hook_auto_approve` on
`nx-mcp` declared as an `mcp_tool` hook, and `nx hook session-start` as
`nx-hook session-start` in exec form. Both pass their retargeted tests,
and both fire in a real Claude Code session on macOS and inside a WSL2
distro, with the timing of the first `PreToolUse` after launch recorded.

### Phase 1: Shape

1. `src/nexus/hooks/` package, `_io.py`, `_config.py`, the `nx-mcp`
   registration module, the `nx-hook` console script, the two MVV ports
   and their tests.
2. The connection-race and PATH measurements on the host shapes above,
   recorded in this RDR.

### Phase 2: The ledger

3. `expectations.sh` to `nexus.hooks.expectations`; `agent-dispatch-expect.sh`,
   `subagent-start.sh` and `subagent-stop.sh` ported as tools since they
   source it; the e2e copy and its byte-identity test deleted.

### Phase 3: The rest

4. The remaining conexus scripts ported as tools, largest first
   (`pre_close_verification_hook.sh`, `stop_verification_hook.sh`,
   `subagent-start-stamp.sh`, `divergence-language-guard.sh`,
   `post_compact_hook.sh`, the two async wrappers).
5. The eight Python hooks: `mailbox_drain.py`, `stop_failure_hook.py`
   and the two routing hooks re-declared as tools; `preflight.py`,
   `session_start_hook.py` and `rdr_hook.py` re-declared on `nx-hook`;
   `version_lockstep_hook.py` re-declared as exec-form `python3` with its
   dispatch lines rewritten (Approach item 3). The four shell-form `nx`
   entries per Approach item 6. The launcher and its tests per Approach
   item 7.
6. The four sn entries per Approach item 8.

### Phase 4: Close

7. The lint of Technical Design passes on both `hooks.json` files.
8. AGENTS.md's `expectations_*` entry rewritten for the verbs.

## Test Plan

- Every ported hook keeps its test, retargeted at the tool or the verb
  with the same payload and the same expected output.
- One stdio integration test drives a real `nx-mcp` for one hook tool.
- The lint test of Technical Design, both shapes, whole-string matching.
- The expectations module gets unit tests for the verbs' exit codes
  0, 1, 2, 3 and 0, 2, 4 against fixture ledgers.
- The budget tests keep their thresholds.

## Finalization Gate

### Contradiction Check

- Gap 3 names two hook forms that need no shell; the Approach uses both,
  by event. The tool tier is unavailable on `SessionStart` at launch
  (research-9); the command tier covers that set plus `SessionEnd`, which
  stays on `nx-session-end-launcher` by choice.
- The inventory (research-1) counted `timeout`, `uname` and `curl` as
  call sites; the contract map (research-6) found no script calls them.
  The Technical Environment carries the corrected list.
- Approach item 2 says no entry names `nx`; the lint's whole-string
  clause enforces it; item 6 gives each reshape target. The three agree.

### Assumption Verification

Four assumptions of record are in Critical Assumptions. Status at gate:

- **Server connected before the first post-launch hook.** Assumed from
  the doc's wording; measured in Phase 1 with the mitigation named in
  Risks.
- **Console scripts on the `SessionStart` PATH.** Assumed, inherited from
  the bash layer (research-4); measured in Phase 1.
- **Start-up cost.** Verified (research-5); the command tier avoids it by
  construction and the tool tier has none.
- **`bd` stays a subprocess.** Assumed; no API is needed.

#### API Verification

| Surface | Verification |
| --- | --- |
| Claude Code `mcp_tool` hook (`server`, `tool`, `input` with `${path}` substitution; text output read as stdout; skipped on `SessionStart` at launch and `Setup`) | Docs only: code.claude.com/docs/en/hooks, read 2026-09-18 (research-9). Exercised by the MVV. |
| Claude Code exec form (`command` plus `args`, real executable, no shell) | Docs only: same page (research-2). Exercised by the MVV. |
| Plugin server scoped name `plugin:conexus:nexus` | Docs only: the hooks page's `server` field; the plugin's `.mcp.json` key is `nexus`. |
| Console-script entry with pre-dispatch minimal imports | Source search: `src/nexus/_session_end_launcher.py`, `pyproject.toml:233` (research-6). |
| Atomic claims via `os.mkdir` and `os.symlink` | Source search: `expectations.sh` uses `mkdir` and `ln -s` (research-6); both atomic create-or-fail on Linux, macOS and WSL2; native Windows is out of scope and `os.symlink` there also needs a privilege. |
| Hook payload and decision envelope shapes | Source search: per-script stdout shapes cited to lines in T2 `215-hook-contract-map`. |

### Scope Verification

In scope: the 16 scripts in the two plugins' `hooks/scripts/`, the
launcher and the four tests that name it, the e2e copy of the ledger
library, the `hooks.json` declarations, the `nx-mcp` registration of the
hook tools, the tests that drive the hooks, and the AGENTS.md entry that
documents the ledger verbs. Out of scope: the bash generation-install
scripts under `src/nexus/_install/` (a different surface with its own
tests, and not a hook), the PG bundle, any Windows build, and making
`nexus.cli` lazy (named as its own bead in Decision Rationale). The three
defects listed under Technical Design are fixed because the port
replaces the lines that carry them; no other behaviour changes.

### Cross-Cutting Concerns

- **Plugin release surface.** `hooks.json` and every file under
  `conexus/hooks/scripts/` are plugin content; the change ships through
  the drift ledger (`conexus/PENDING_RELEASE.md`) and a client release or
  a plugin cut. Until then the installed plugin keeps running the bash.
- **Version lock-step.** A plugin whose `hooks.json` names `nx-hook` or
  the `hook_` tools requires a conexus wheel that ships them. On a box
  whose wheel predates it, those entries fail to spawn or return a
  non-blocking error, so the hook that repairs that state,
  `version_lockstep_hook.py`, is on neither tier (Approach item 3), and
  the drift ledger entry states the wheel floor.
- **Hook tools in the model's tool list.** MCP cannot hide a tool; the
  `hook_` prefix, the description, and the auto-approve matcher are the
  mitigation.
- **Plugin independence.** sn's hooks depend on `python3` and its own
  bundled scripts only; nothing in this RDR makes sn require conexus.
- **Logging.** Hooks log to the hook log through `_hook_logging.py`
  today; the shared boundary keeps that path so a swallowed exception is
  still recorded.
- **Worktree agents.** The subagent hooks read the worktree guard; the
  port keeps the same file reads.

### Proportionality

About 4,200 lines of bash become roughly the same amount of Python, with
per-module tests where the bash had end-to-end tests only. The port is
phased so the smallest decision-emitting hook proves the shape before
the ledger moves, and each phase leaves the plugin working. The
alternative of keeping bash costs nothing today and blocks any host
without a POSIX shell; the alternative of a Node rewrite would
re-implement the `nexus` client. The work is sized to the surface it
retires and adds no new mechanism beyond one console script, one
registration module on the existing server, and one package.

## References

- code.claude.com/docs/en/hooks (exec form, `shell`, Windows default
  shell), read 2026-09-18.
- T2 `nexus/windows-support-research-of-record-2026-09-18`.
- `conexus/hooks/hooks.json`, `sn/hooks/hooks.json`,
  `conexus/hooks/scripts/_run_python_hook.sh`.

## Revision History

- 2026-09-18: Created from the Windows-support research; inventory and
  plan drafted, Technical Design deferred to the first port.
- 2026-09-18: research-4 and research-5 recorded; the entry point moves
  from `nx hook` to a dedicated `nx-hook` console script after measuring
  the CLI's 0.8 s eager import.
- 2026-09-18: research-6, the contract map; Technical Design written from
  it; the external-command list corrected (no `timeout`, `uname`, `curl`
  or `jq`).
- 2026-09-18: Gate round 1 — BLOCKED (2 Critical, 3 Significant, 2 ship-blocker(s)); commit `766e3be42`; critique `nexus_rdr/215-gate-critique-2026-09-18-r1`.
- 2026-09-18: Gate round 2 — PASSED (0 Critical, 0 Significant, 0 ship-blocker(s)); commit `c77ae4e49`; critique `nexus_rdr/215-gate-critique-2026-09-18-r2`.
- 2026-09-18: Design amended to two tiers (research-9): `mcp_tool` hooks on `nx-mcp` for every event after session start, `nx-hook` command hooks for `SessionStart`, the lockstep hook on stdlib `python3`.
- 2026-09-18: Gate round 3 — PASSED (0 Critical, 4 Significant, 0 ship-blocker(s)); commit `dd95743fa`; critique `nexus_rdr/215-gate-critique-2026-09-18-r3`.
