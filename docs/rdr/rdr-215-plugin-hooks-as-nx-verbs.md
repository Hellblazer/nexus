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
that spawns a real executable with no shell at all. Sam's decision that
day: move the hook logic out of bash and behind `nx hook` verbs.

## Problem Statement

A Claude Code hook is a command the client runs at a lifecycle event
(session start, a tool call, a subagent stopping) and whose exit code and
JSON on stdout can block the event or add context to it. The conexus and
sn plugins declare 30 such hooks in their `hooks.json` files. Of those, 15
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
a Python that imports `nexus`. The `nx` console script already is that
Python: the generation's shim resolves `current` at spawn and execs into
it. A hook declared as `nx hook <verb>` inherits the resolution for free;
a hook declared as a bash script has to rebuild it.

#### Gap 3: Hook behaviour cannot run on a host without a POSIX shell

Claude Code's exec form runs `command` with `args` and no shell, on every
platform, provided `command` is a real executable. `nx` is one (a console
script on Linux and macOS, `nx.exe` on Windows). A hook layer built on
`nx hook` verbs in exec form therefore runs wherever `nx` installs. The
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
byte budgets, and calls into `nx` and `bd`. Eleven hooks were written in
Python from the start and are launched through `_run_python_hook.sh`;
four are already `nx` verbs (`nx hook session-start`, `nx upgrade --auto`,
`nx self gc`, `nx-session-end-launcher`). The direction is already set;
this RDR finishes it.

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
| `_run_python_hook.sh` | 49 | launcher for 11 Python hooks | n/a |
| `subagent-start-tuple-async.sh` | 38 | SubagentStart | no |
| `subagent-stop-tuple-async.sh` | 24 | SubagentStop | no |
| `sn/mcp-inject.sh` | 79 | SubagentStart | yes |
| `sn/auto-approve-sn-mcp.sh` | 10 | PreToolUse, PermissionRequest | yes |
| `sn/session-start.sh` | 9 | SessionStart | no |

Python hooks already present and launched through the bash launcher:
`preflight.py`, `session_start_hook.py`, `rdr_hook.py`,
`version_lockstep_hook.py`, `stop_failure_hook.py`, `mailbox_drain.py`,
`routing/subagent_git_write_requires_orchestrator.py`,
`routing/phase_review_close_requires_gate.py`, and their helper modules.
These need only a declaration change to exec form once a verb wraps them.

The full contract map, one row per script with stdin fields, stdout
shapes, exit codes, files touched outside the repo and every quoting
site, is T2 `nexus_rdr/215-hook-contract-map` (research-6).

### Key Discoveries

- **Verified** (source search, 2026-09-18): the Python hooks import
  `nexus` and must run under the generation's interpreter; a bare Homebrew
  `python3` cannot even log the import failure (`_run_python_hook.sh`
  header, measured 2026-09-08). `nx` resolves that interpreter by
  construction.
- **Verified** (docs, 2026-09-18): Claude Code's exec form needs a real
  executable and passes `args` verbatim, so `{"command": "nx", "args":
  ["hook", "subagent-start"]}` runs identically on every platform where
  `nx` is on PATH.
- **Verified** (source search): four hooks are already `nx` verbs and one
  already ships as a separate console script (`nx-session-end-launcher`),
  so the pattern exists in the repo.
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

- **Assumed**: the generation's console scripts are on the PATH Claude
  Code gives hooks on every supported install. The bash layer already
  assumes this (research-4), so a port cannot make it worse, but whether
  an app-launched Claude Code on macOS or a WSL2 distro inherits the login
  PATH is still to be measured in Phase 1. A hook that cannot find its
  generation must fail loud, not silently skip.
- **Verified, with a design consequence** (research-5): a hook that enters
  through the `nx` console script pays about 0.8 s for the CLI's eager
  imports. The verbs therefore get their own console script, `nx-hook`,
  whose module imports only what the invoked verb needs, so a hook costs
  what the Python hooks cost today (about 0.04 s) rather than what `nx`
  costs. The budget tests keep their thresholds and pin this.
- **Assumed**: the `bd` calls (29 sites) can stay as subprocess calls from
  Python; no bead-tool Python API is required.

## Proposed Solution

### Approach

1. Every hook in both plugins is declared in exec form as
   `nx-hook <verb>`, a new console script beside `nx-session-end-launcher`
   in `pyproject.toml`, whose entry module imports nothing from
   `nexus.cli` and resolves the verb to its module lazily. `nx hook <verb>`
   stays as an alias for a human at a terminal; the plugin never declares
   it, because `nx` pays the CLI's 0.8 s import on every call
   (research-5).
2. Each bash script becomes a module under `src/nexus/hooks/` with one
   entry function, registered in the `nx-hook` verb table. The module
   reads the hook payload from stdin and writes the decision JSON to
   stdout, exactly as the script did. Heavy imports (the catalog client,
   T3) happen inside the branch that needs them.
3. `expectations.sh` becomes `nexus.hooks.expectations`, one module, with
   the three ledger verbs exposed as `nx-hook expect`, `nx-hook census`
   and `nx-hook undeclared` keeping the documented exit codes. The e2e
   copy and its byte-identity test are deleted; `tests/e2e/lib` imports the
   module.
4. `_run_python_hook.sh` is deleted once no declaration names it.
5. Each port is a behaviour-preserving move: the existing subprocess test
   for the script is retargeted at the verb with the same stdin payload
   and the same expected stdout and exit code, and passes before the bash
   script is deleted.
6. Byte budgets and timing budgets that exist as tests today keep their
   thresholds.

### Technical Design

**Entry point.** `nx-hook = "nexus.hooks.entry:main"` in
`pyproject.toml`'s console-script table, beside `nx-session-end-launcher`
and built the same way: the module imports `os`, `sys` and `json` only,
reads `argv[1]` as the verb, and imports `nexus.hooks.<verb>` lazily.
The Click group `nx hook` keeps its six verbs for a human and gains
aliases to the new ones, but no `hooks.json` entry names `nx`.

**Package.** `src/nexus/hooks/` (the existing `nexus.hooks` module that
`session-start` calls becomes `nexus/hooks/__init__.py`). One module per
retired script, one `run(payload: dict | None, argv: list[str]) -> int`
per module. A shared `_io.py` holds the three things every script
re-implemented: read the payload from stdin (TTY-aware, empty or
malformed reads as `None`, as `_read_stdin_payload` does today), write a
decision envelope (`hookSpecificOutput` with `permissionDecision` or
`additionalContext`, or the top-level `decision` form the stop hooks
use), and the never-fail boundary (a verb that raises logs the traceback
to the hook log and exits 0 with no output, which is what the bash
scripts' missing `set -e` gives them today).

**Exit codes and stdout are contracts.** The map lists them per script;
the port reproduces each byte for byte, and the retargeted test asserts
them. Two are quoted outside the repo's tests: the ledger verbs' codes
(0 clean, 1 BLINDSPOT, 2 undeclared, 3 no ledger for `undeclared`; 0, 2,
4 for `reconcile`) in AGENTS.md and the orchestration skill, and the
close gate's deny text in 19 files.

**The ledger** (`nexus.hooks.expectations`). A TSV file under
`$XDG_STATE_HOME/nexus/orchestration/<session>.expectations` with
`mkdir` lock directories and `ln -s` credit slots; the module keeps the
file format, the paths and the atomicity primitives (`os.mkdir`,
`os.symlink`, both atomic on every platform nexus runs on), and exposes
`expect`, `start`, `census`, `undeclared`, `reconcile`, `archive`,
`sweep` as verbs. `tests/e2e/lib/expectations.sh` and the byte-identity
test go; the e2e scripts call `nx-hook <verb>`.

**Async wrappers.** The two `*-tuple-async.sh` scripts background a
Python projector and exit in about 18 ms. Their port is the launcher's
double-fork with the standard streams redirected to `/dev/null`, in the
entry module before any heavy import.

**Shared resolver.** `NX_ORCH_STOP_GUARD` is read inline by four scripts
today; `nexus.hooks._config` resolves it once.

**Defects fixed in the port, not carried.** The close gate's `bd create`
deny path omits `permissionDecisionReason`; the sn auto-approve wrapper
swallows a Python crash with an unconditional `exit 0`; the sn
session-start script has no error boundary at all. Each gets the shared
envelope and boundary.

**Tests.** Each retargeted test keeps its stdin fixture and expected
bytes and spawns `nx-hook <verb>` instead of `bash <script>`. Three
scripts have no test in `tests/hooks/` (`sn/session-start.sh`,
`sn/mcp-inject.sh`, and the sn auto-approve wrapper); their ports get
one. A lint test asserts no `hooks.json` command names `bash`, `sh` or a
`.sh` path, and that every `command` is `nx-hook` or an existing console
script.

### Decision Rationale

- **A generation console script over a second launcher.** The generation
  install already produces console scripts whose interpreter is the right
  one; `nx-hook` is one more entry in the same table, not a second copy of
  the bash launcher's resolution ladder.
- **A separate script over `nx hook`.** Measured, not preferred: `nx`
  imports 38 command modules before it dispatches (research-5). Making
  `nexus.cli` lazy would fix that for every `nx` call and is worth its own
  bead, but this RDR does not depend on it.
- **Exec form over `shell: powershell` variants.** One declaration per
  hook, no per-platform fork of the logic.
- **Move, do not rewrite.** The scripts encode months of measured
  behaviour (budgets, exit codes, refusal text that other files quote).
  Each port carries the existing test across first.

## Alternatives Considered

### Alternative 1: Keep bash, require Git for Windows on native Windows

Pros: zero work. Cons: the scripts assume GNU tools and `/tmp`; Git Bash
is MSYS, and the failure catalogue for that combination is long. It also
leaves Gap 2 and Gap 4 open on every platform.

### Alternative 2: Rewrite the hook logic in Node

Pros: Claude Code's own docs use `node` as the exec-form example. Cons:
the hooks import `nexus` and talk to T1, T2 and the catalog; a Node port
would reimplement that client or shell out to `nx` for every call, which
is the bash layer's current shape.

### Alternative 3: Port only the hooks that emit decisions

Pros: smaller. Cons: the ledger library and the launcher, which are the
two largest sources of shell dependency, emit no decision and would stay.

## Trade-offs

### Consequences

- The plugin's `hooks.json` files change shape (exec form). That is a
  plugin-surface change and ships through the drift ledger and a plugin
  cut or client release.
- Hook start-up moves from "bash then maybe nx" to "nx". For hooks that
  already called `nx`, that is one process fewer.
- 4,200 lines of bash leave; roughly the same amount of Python arrives,
  with unit tests per module.

### Risks and Mitigations

- **`nx` not on the hook PATH.** Mitigation: the first port measures it on
  macOS terminal, macOS app-launched Claude Code, Linux and WSL2, and the
  verb prints one line naming the fix when it cannot find its generation.
- **A port changes a refusal text or exit code that another file quotes.**
  Mitigation: the retargeted test asserts the exact bytes; a grep for the
  quoted strings runs before each script is deleted.
- **Timing.** Mitigation: the budget tests stay; a port that exceeds one
  is a finding, not a threshold change.

### Failure Modes

- A hook verb that imports a heavy module at start-up regresses every
  session start. Guard: imports deferred to the branch that needs them.
- The detach for async hooks leaks a child on one platform. Guard: the
  existing `test_subagent_tuple_async_wrappers.py` asserts no lingering
  process.

## Implementation Plan

No implementation starts before this RDR is accepted.

### Minimum Viable Validation

The first port, `auto-approve-nx-mcp.sh` to `nx-hook auto-approve`, with
its declaration in exec form, passing `tests/hooks/test_permission_request_hooks.py`
retargeted, on macOS and inside a WSL2 distro. That settles the module
shape and the PATH assumption before anything larger moves.

### Phase 1: Shape

1. `src/nexus/hooks/` package, the `nx-hook` console script and its lazy
   verb table, the payload reader and decision writer, one ported hook,
   its test, and a timing assertion that the ported hook starts in under
   0.1 s on the dev box.
2. PATH measurement on the four host shapes above; the result recorded in
   this RDR.

### Phase 2: The ledger

3. `expectations.sh` to `nexus.hooks.expectations`; the three verbs; the
   e2e copy and byte-identity test deleted; `agent-dispatch-expect.sh`,
   `subagent-start.sh`, `subagent-stop.sh` ported since they source it.

### Phase 3: The rest

4. The remaining conexus scripts, largest first
   (`pre_close_verification_hook.sh`, `stop_verification_hook.sh`,
   `subagent-start-stamp.sh`, `divergence-language-guard.sh`,
   `post_compact_hook.sh`, the two async wrappers).
5. The 11 existing Python hooks re-declared in exec form behind verbs;
   `_run_python_hook.sh` deleted.
6. The three sn hooks.

### Phase 4: Close

7. `hooks.json` carries no `bash` and no `.sh`; a lint test pins that.
8. AGENTS.md's `expectations_*` entry rewritten for the verbs.

## Test Plan

- Every ported hook keeps its subprocess test, retargeted at the verb with
  the same stdin and the same expected stdout and exit code.
- A lint test asserts no `hooks.json` command names `bash`, `sh` or a
  `.sh` path.
- The expectations module gets unit tests for the three verbs' exit codes
  0, 1, 2, 3 against fixture ledgers.
- The budget tests keep their thresholds.

## Finalization Gate

### Contradiction Check

- The Provenance line says Sam's decision was "behind `nx hook` verbs";
  the Approach says the plugin declares `nx-hook`, a separate console
  script, and keeps `nx hook` as a human alias. Both are true in
  sequence: the decision named the verb surface, research-5 measured the
  entry cost, and the entry point moved. The Revision History records the
  move.
- Gap 2 and Gap 3 speak of `nx` as the executable that inherits
  interpreter resolution. The design uses a sibling console script from
  the same generation table, which inherits it the same way. The gaps
  describe the property; the design names the script.
- The inventory (research-1) counted `timeout` 16, `uname` 1, `curl` 1
  as call sites; the contract map (research-6) found no script calls
  them. The Technical Environment carries the corrected list and names
  the correction; research-1's counts stand as what a word count gave.

### Assumption Verification

Three assumptions of record are in Critical Assumptions. Status at gate:

- **Console scripts on the hook PATH.** Assumed, inherited from the bash
  layer (research-4: eight `command -v nx` sites). Not yet measured on
  an app-launched macOS Claude Code or in WSL2. Phase 1 item 2 measures
  it and records the result here; a verb that cannot find its generation
  fails loud.
- **Start-up cost.** Verified by spike (research-5): the cost is
  `nexus.cli`'s eager import, avoided by a dedicated entry that imports
  only the verb's module. Phase 1 pins a sub-0.1 s start on the dev box.
- **`bd` stays a subprocess.** Assumed. The bash layer calls `bd list`
  and `bd set-state` today (research-6); a Python port calls the same
  binary with `subprocess.run`. No bead-tool Python API exists and none
  is needed.

#### API Verification

| Surface | Verification |
| --- | --- |
| Claude Code hook exec form (`command` plus `args`, real executable, no shell) | Docs only: code.claude.com/docs/en/hooks, read 2026-09-18 (research-2). Not yet exercised against a running Claude Code; the MVV does that. |
| Console-script entry with pre-fork minimal imports | Source search: `src/nexus/_session_end_launcher.py`, `pyproject.toml:233` (research-6). |
| Atomic claims via `os.mkdir` and `os.symlink` | Source search: `expectations.sh` uses `mkdir` and `ln -s` for the same guarantee (research-6); the POSIX and Windows semantics of both calls are documented as atomic-create-or-fail. |
| Hook payload and decision envelope shapes | Source search: per-script stdout shapes cited to lines in T2 `215-hook-contract-map`. |

### Scope Verification

In scope: the 16 scripts in the two plugins' `hooks/scripts/`, the
launcher, the e2e copy of the ledger library, the `hooks.json`
declarations, the tests that drive them, and the AGENTS.md entry that
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
- **Version lock-step.** A plugin whose `hooks.json` names `nx-hook`
  requires a conexus wheel that ships that console script; the existing
  lock-step hook (`version_lockstep_hook.py`) is the mechanism that
  reports the mismatch, and the drift ledger entry states the floor.
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
retires and adds no new mechanism beyond one console script and one
package.

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
