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
- External tools the bash layer calls, by count of call sites: `nx` 74,
  `python3` 70, `bd` 29, `awk` 22, `timeout` 16, `git` 16, `stat` 6,
  `mktemp` 5, `sed` 4, `date` 4, `uname` 1, `curl` 1.

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

### Critical Assumptions

- **Assumed**: `nx` is on the PATH Claude Code gives hooks, on every
  supported install. The generation install writes the shim to
  `~/.local/bin`; whether Claude Code's hook environment inherits the
  user's login PATH on macOS app launches and on Windows is to be
  measured, not assumed. A hook that cannot find `nx` must fail loud, not
  silently skip.
- **Assumed**: hook start-up latency under `nx` (a Python process import)
  is within the budgets the existing byte-and-time tests pin
  (`tests/hooks/test_session_start_combined_budget.py`,
  `test_subagent_start_byte_budget.py`). The bash hooks that call `nx`
  already pay that cost once; a verb pays it exactly once.
- **Assumed**: the `bd` calls (29 sites) can stay as subprocess calls from
  Python; no bead-tool Python API is required.

## Proposed Solution

### Approach

1. Every hook in both plugins is declared in exec form as `nx hook <verb>`
   (or `nx-<verb>` for a separate console script where a process must
   outlive the hook, as `nx-session-end-launcher` does today).
2. Each bash script becomes a module under `src/nexus/hooks/` with one
   entry function, registered as a subcommand of `nx hook`. The module
   reads the hook payload from stdin and writes the decision JSON to
   stdout, exactly as the script did.
3. `expectations.sh` becomes `nexus.hooks.expectations`, one module, with
   the three ledger verbs exposed as `nx hook expect`, `nx hook census`
   and `nx hook undeclared` keeping the documented exit codes. The e2e
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

To be written after the first port (the smallest decision-emitting hook,
`auto-approve-nx-mcp.sh`) settles the module shape: how a verb reads the
payload, how it logs, how it reports a decision, and how a test drives it
without a shell.

Open questions the design must settle:

- Hooks that must not block the client (the two `*-tuple-async.sh`
  wrappers spawn `nx` and return) need a portable detach. `subprocess.Popen`
  with the standard streams closed is portable; process groups are not.
- `pre_close_verification_hook.sh` reads T1 through the CLI and greps
  Bash commands for `bd close`; the port keeps its matcher and its
  refusal text verbatim, since three AGENTS.md entries quote it.
- The sn plugin's hooks are small and can move in one step.

### Decision Rationale

- **`nx` over a second launcher.** The generation shim already solves
  interpreter resolution; a Python launcher next to it would be a second
  copy of the same ladder.
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

The first port, `auto-approve-nx-mcp.sh` to `nx hook auto-approve`, with
its declaration in exec form, passing `tests/hooks/test_permission_request_hooks.py`
retargeted, on macOS and inside a WSL2 distro. That settles the module
shape and the PATH assumption before anything larger moves.

### Phase 1: Shape

1. `src/nexus/hooks/` package, `nx hook` subcommand registration pattern,
   the payload reader and decision writer, one ported hook, its test.
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

To be completed before the gate.

### Contradiction Check

### Assumption Verification

#### API Verification

### Scope Verification

### Cross-Cutting Concerns

### Proportionality

## References

- code.claude.com/docs/en/hooks (exec form, `shell`, Windows default
  shell), read 2026-09-18.
- T2 `nexus/windows-support-research-of-record-2026-09-18`.
- `conexus/hooks/hooks.json`, `sn/hooks/hooks.json`,
  `conexus/hooks/scripts/_run_python_hook.sh`.

## Revision History

- 2026-09-18: Created from the Windows-support research; inventory and
  plan drafted, Technical Design deferred to the first port.
