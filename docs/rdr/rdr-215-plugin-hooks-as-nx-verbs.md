---
title: "Plugin Hooks as nx Verbs: Retire the Bash Hook Layer"
id: RDR-215
type: Technical Debt
status: closed
closed_date: 2026-09-19
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-18
accepted_date: 2026-09-18
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
process at all. Sam's decision that day: move the hook logic out of bash,
into the `nexus` package for conexus and onto sn's own bundled Python
scripts for sn.

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

- **Verified** (bead `nexus-q02nx.6`, 2026-09-19, macOS and WSL2, CLI
  2.1.278): `${path}` substitution into an `mcp_tool` `input` map
  delivers non-scalar payload fields AS STRUCTURES. `${tool_input}`
  arrives as a `dict`, `${background_tasks}` on `Stop` as a `list` — not
  JSON-encoded strings and not unsubstituted literals. An absent key
  renders as an empty string, so absent is distinguishable from an empty
  list (`''` against `[]`). The tool tier therefore carries
  `stop_verification_hook.sh`'s `background_tasks` cross-check intact,
  and beads `nexus-q02nx.10`, `.12` and `.13` stay on the tool tier.
  The determining case was measured, not assumed: a populated MIXED
  population survives intact. A `Stop` event carrying one `type: shell`
  task (keys `id/type/status/description/command`) and one
  `type: subagent` task (`agent_type` in place of `command`) arrived as
  a `list` of two `dict`s with both key sets and every value intact,
  including a `command` containing `$i`, quotes and semicolons. So
  `expectations.sh`'s documented "mixed population, field names not yet
  stable" concern is answered for transport.
  Two limits: `''` is unambiguous only where the field can never
  legitimately be an empty STRING; and the empty-list case (`[]`) alone
  would NOT have shown this, so do not cite it as the evidence.
  *Source: T2 `nexus_rdr/215-phase1-measurements`.*
- **Verified, and it is a landmine for the ports** (same bead): Claude
  Code delivers those structures, but the tool tier's own registration
  schema cannot currently accept them. `_make_tool_function`
  (`src/nexus/mcp/hooks.py`) types EVERY hook-tool field
  `Annotated[str | None, ...]`, and pydantic 2.12.5 rejects both a
  `dict` and a `list` against that with `string_type`. So the transport
  was proven and the implementation was not: a port declaring a field
  that carries `${tool_input}` or `${background_tasks}` would have failed
  validation. FIXED (bead `nexus-9ifls`): `HookToolSpec` now takes
  `structured_fields`, and only a field listed there is typed `Any` --
  every other field keeps `str | None`, because the shapes are a closed
  set the contract map enumerates and the model reads this schema. A
  blanket widening was tried first and was a pure regression on the one
  live tool; the wire snapshot is byte-identical to before it.
  What a rejected argument actually does, measured (2026-09-19, CLI
  2.1.278) rather than assumed: pydantic rejects it during FastMCP's
  argument binding, BEFORE the tool body runs, so `never_fail` never sees
  it and this is not the `isError=False` path. It surfaces as a tool
  error -- and at the HOOK level the event still is not blocked. A hook
  tool that raised was logged `Hook PreToolUse:Bash (PreToolUse) error:
  ...` and the Bash command ran anyway. So a mistyped field is loud at
  the tool boundary and fail-open at the event, which is the third
  fail-open path this phase has found.
- **Verified** (same bead): a hook tool's own invocation is exempt from
  the hook chain, but a MODEL-initiated call to one is not. With a
  `PreToolUse` matcher covering the server's own tools, the hook fired
  once on the model's call and its own tool call did not re-trigger it —
  one dispatch, terminating. So `hook_auto_approve` matching
  `mcp__plugin_conexus_.*` is benign and needs no recursion guard.
  Scoped to the topology tested: ONE hook tool whose own matcher covers
  it. Phases 2 and 3 register roughly fourteen `hook_*` tools against
  that same matcher; if the exemption proves per-invocation rather than
  blanket, re-check with two registered at once.

### Critical Assumptions

- **Verified, and the assumption was stronger than it needed to be**
  (bead `nexus-q02nx.6`, 2026-09-19, CLI 2.1.278, macOS and WSL2): there
  is no race. The first model turn does not begin until the session's
  MCP connection attempt RESOLVES. Measured by delaying the probe
  server's start rather than racing it: a 15 s delay moved the first
  `PreToolUse` hook by 15.3 s while the connect-to-hook gap stayed about
  2 s, and `[engine] turn 1 start` landed 21 ms after
  `Successfully connected`. On WSL2 the same ordering held at 73 ms.
  Since every tool-tier event is reached only through a model-initiated
  tool call, none of them can fire before the servers are available.
  **Measured under headless `claude -p` only** (`cc_entrypoint=sdk-cli`,
  `nonInteractive=true`), on both host shapes. Interactive ordering is
  NOT established here, and interactive is what users actually run — a
  human can submit input the instant a prompt appears, which a scripted
  invocation does not exercise. Before Phase 2 relies on "no command-tier
  twin is needed for any event", run the same delay ladder interactively.
  **THIS WAS NEVER RUN, and Phases 2 through 4 relied on the conclusion
  anyway** -- the shipped manifest carries 13 `mcp_tool` entries with no
  command-tier fallback for any of them. Found by the isolated close
  critique, not during implementation, and tracked as bead `nexus-veh77`
  (P1) against the plugin cut or client release that activates this
  manifest for real users, NOT against a develop push: fail-open is the
  posture here, so a race silently skips the hook, and
  `conexus/PENDING_RELEASE.md` already gates the release on the wheel
  floor. The hooks that would silently skip include the bd-close gate and
  the RDR-184 EXPECT writer.
  The barrier is more than a timing correlation: at the 35 s point the
  server was still sleeping when the 30 s timeout fired, and turn 1
  started 33 ms after the TIMEOUT resolved rather than after the server
  became ready, so turn 1 waits on the connection ATTEMPT's resolution
  and not merely on a freed event loop. That discriminates a real
  barrier from scheduling starvation — for this launch mode.
  *Source: T2 `nexus_rdr/215-phase1-measurements`.*
- **Verified, and it relocates the risk**: the real failure mode is a
  server that never connects, not one that connects late. At a 35 s
  start delay the connection hit its 30 s `CONNECT_TIMEOUT`, `turn 1
  start` followed 33 ms later, and the hook was skipped — fail-open,
  with the tool call proceeding and nothing user-visible. The only trace
  is the debug log: `[WARN] Hooks: mcp_tool hook skipped — MCP server
  '<name>' not connected`. `nx-mcp`'s own stdio `initialize` round-trip
  is 0.61-0.73 s warm, about 40x under that ceiling; the cold-boot case
  is not yet measured.
- **Verified on two host shapes, and on a third by inference** (bead
  `nexus-q02nx.6`,
  2026-09-19): Claude Code spawns a command hook directly, with no
  intervening login shell, and hands it its own PATH unmodified.
  Terminal-launched macOS: `nx-hook` resolves from `~/.local/bin`.
  WSL2 (Ubuntu 26.04, same CLI 2.1.278): an exec-form `nx-hook` named
  bare on the PATH resolved and ran, argv intact — with a STAND-IN
  executable of that name, not the real console script. A full conexus
  install fails on that distro (`uv tool install conexus` needs
  `x86_64-linux-gnu-g++` to build `fasttext-predict` via `mineru`), so
  WSL2 proves PATH resolution of an exec-form hook, not the real verb
  end to end. App-launched macOS:
  the desktop app REPAIRS the PATH for the children that run user
  tooling. Measured on a Finder-launched `/Applications/Claude.app`:
  the app's own process and its generic node helper carry the bare
  launchd GUI PATH (`/usr/bin:/bin:/usr/sbin:/sbin`, 4 entries, no
  `~/.local/bin`), while the MCP-server spawn path and the plugin node
  helper both carry a 25-entry login-shell PATH in which `nx` and
  `nx-hook` both resolve. So the command tier is reachable on that
  shape.
  One inferential step remains and is stated rather than hidden: an
  actual Claude Code child of the desktop app was not observed, because
  starting one needs a Code session opened in the UI. The two spawn
  paths that WERE measured are the ones that run user tooling, and they
  agree. If a future defect points here, measure a live Code child
  directly before trusting this bullet.
  Bearing the other way: Claude Code itself does NOT repair a minimal
  PATH. Launched with `/usr/bin:/bin:/usr/sbin:/sbin`, the `SessionStart`
  hook received exactly that and `nx-hook` was not found. The command
  tier depends entirely on whoever spawns Claude Code getting this
  right.
- **Refuted, and it changes where the fail-loud line can live**: a
  command hook that cannot be found does NOT fail loud. Claude Code
  reports `[ERROR] Hook command failed to spawn (SessionStart:startup):
  Executable not found in $PATH: "nx-hook"` to the debug log only; the
  session proceeds normally and the user sees nothing. The RDR's
  "a hook that cannot find its generation fails loud" cannot be
  implemented inside the verb, because the verb never runs. It has to
  live somewhere that runs regardless — `nx doctor`, the install, or a
  declaration that does not depend on PATH resolution.
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
The hook logic lives in `src/nexus/hooks/`; the shared dispatch
plumbing it is called through — `_io.py`, `_config.py` and the command
tier's `entry.py` — lives in `src/nexus/_hook_runtime/`, a package whose
`__init__` is a docstring and nothing else. The split is not cosmetic:
Python runs a package's `__init__` before any module inside it, and
`nexus/hooks/__init__.py` imports `nexus.session` and structlog, so a
stdlib-only verb reached through `nexus.hooks` paid 0.06 s before doing
anything (bead `nexus-br31l`; see § Technical Design). Both tiers still
call the same functions.

### Approach

1. **The tool tier.** Every entry on `PreToolUse`, `PostToolUse`,
   `PermissionRequest`, `UserPromptSubmit`, `SubagentStart`,
   `SubagentStop`, `Stop`, `StopFailure` and `PostCompact` in the conexus
   plugin becomes `{"type": "mcp_tool", "server": "plugin:conexus:nexus",
   "tool": "hook_<name>", "input": {...}}`. The `input` map names the
   payload fields the hook reads (the contract map lists them per script)
   as `${session_id}`, `${tool_input.command}` and so on. The tool returns
   the same decision JSON the script wrote to stdout. That is 13 of the 25
   conexus entries as shipped -- this sentence said "15 of the 24" when
   written and both halves moved: `behaviour_census.py` arrived from
   nexus-4lnn1 and took the count to 25, and two more entries stayed on
   the command tier than this item anticipated. See the 2026-09-19
   Revision History entry; the excluded set is three, not one.
   THE FIRST EXCLUSION, known when this was written:
   `phase_review_close_requires_gate` is the routing framework's only
   `fail_closed: true` rule (`routing/registry.yaml`), and its contract is
   that a crash still emits a deny envelope (`routing/_lib.py`'s
   `run_hook`). On this tier it could not: the tool boundary returns a
   raised exception as empty text with `isError` false, and Claude Code
   treats a disconnected server as non-blocking, so both a crash and a
   server that is down would read as allow and a phase could close without
   its gate. It takes the command tier instead, where the process can still
   write the deny envelope before it exits.
   THE OTHER TWO, discovered during the port: `mailbox_drain.py`
   (`UserPromptSubmit`) and `routing/subagent_git_write_requires_orchestrator.py`
   (`PreToolUse`) both reach `_endpoint_resolve.py` -- the first directly,
   the second through `routing/_lib.py` -- and that module cannot leave
   `conexus/hooks/scripts/` while `t2_prefix_scan.py` and
   `tuple_ledger_project.py` import it and neither is ported by this
   epic. Moving them would mean a second copy of a 449-line resolver
   beside `nexus.db.service_endpoint`, which Approach item 9 forbids as a
   rewrite. Unlike the first exclusion this one is not about fail-closed
   semantics; it is a dependency the tier split cannot cross.
   CORRECTED 2026-09-23 (nexus-t9klx): THAT REASON WAS WRONG. It assumed a
   moved script had to carry its mirror with it. A wheel module can call
   the client's own primitives instead, which is what
   `tuple_ledger_project` had already done when this epic ported it. All
   three hooks, and the other two bare-`python3` hooks, are now `nx-hook`
   verbs with no mirror. The tier outcome stands for other reasons.
   Both routing guards return a deny, and an `mcp_tool` hook cannot return
   a permission decision (nexus-17i1n measured the close gate inert on the
   tool tier); `phase_review_close_requires_gate` must also deny when it
   crashes. `mailbox_drain` must write its stdout before it returns. The
   git-write guard's fail-OPEN posture is not a tier reason: a crash on
   the tool tier already reads as allow. See Revision History, 2026-09-23.
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
   call with `python3`. Its four entries become exec-form `python3` [since 2026-09-23 exec-form `uv run`, nexus-j4iy0]: the
   `PreToolUse` and `PermissionRequest` entries on `auto_approve_sn_mcp.py`,
   `SubagentStart` on a new `subagent_start.py` that carries
   `mcp-inject.sh`'s body (the section files, the envelope, and the
   worktree decision imported from `worktree_guard.py`), and
   `SessionStart` on a new `session_start.py` that emits the session-start
   section. sn keeps no dependency on the conexus wheel or server.
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
**CORRECTED 2026-09-19: it was DELETED, not threaded.** The command has
raised `ClickException` unconditionally since conexus 7.0.0 and the
substrate it synced was retired at RDR-158 P4, so threading it would have
moved dead code off the synchronous path. `src/nexus/hooks/stop_verification.py`
carries the deviation in its own docstring under "DEVIATION, STATED".
The hook tools are visible in the model's tool list, since MCP has no
way to hide a tool; the `hook_` prefix and a one-line description saying
so are the mitigation, and the auto-approve matcher covers them.

**The command tier.** `nx-hook = "nexus._hook_runtime.entry:main"` in
`pyproject.toml`, built like `nx-session-end-launcher`: `os`, `sys` and
`json` before dispatch, the verb's module after. The entry point and the
shared payload/decision plumbing live in `nexus._hook_runtime`, a package
whose `__init__` is a docstring and nothing else, and NOT in
`nexus.hooks` — Python runs a package's `__init__` before any module
inside it, and `nexus/hooks/__init__.py` imports `structlog` and
`nexus.session`, so reaching `_io` from there cost 0.06 s against 0.01 s
for a bare `import nexus`. Nothing on the dispatch path configures
logging either; a verb that logs through an ambient logger calls
`configure_hook_logging()` itself, and `main()` routes stray stdout to
stderr so the decision channel is safe whether it does or not. A
stdlib-only verb dispatches end to end in 0.02 s, against 0.03 s for the
bash close gate measured on the same box the same day (bead .2's harness
recorded 0.04 s for it; the margin is real either way, but it is one
hundredth of a second, not two). That 0.02 s is the dispatch FLOOR,
measured with a synthetic stdlib-only verb through the real entry point,
and it is what the close gate's COMMON path will pay -- the path that
runs on every Bash call and exits early via `_lib.allow()`. The narrow
phase-review branch additionally imports `nexus.session` and shells out
to `bd show`, which is its own cost and is not measured until the port
lands (nexus-br31l). It reads the payload
from stdin (TTY-aware, empty or malformed reads as `None`), calls the
same `run()`, writes the decision JSON to stdout, and exits 0 for every
hook verb. Every ledger verb propagates the code `run()` returns instead,
so a caller that branches on it keeps working; Contracts, below, carries
the codes.

**A THIRD CASE: a ledger verb that CRASHED exits a reserved 70**
(sysexits `EX_SOFTWARE`; Sam's ruling 2026-09-19, bead
`nexus-q02nx.9`). A ledger verb's exit code IS its contract, so a crash
must not wear a vocabulary value. Measured before the fix: a ledger verb
that raised exited 0, indistinguishable from a clean `reconcile`, which
bead `.13` reads as "nothing stranded" — a silent miss in the subsystem
built to catch silent misses. `never_fail` now marks the swallow
(`HookResult.crashed`) and the entry point maps it. Reserved rather than
folded into `undeclared`'s 3 ("no ledger file, nothing checkable"),
because "I could not tell you" is not "there was nothing to tell", and
folding them loses the distinction exactly when someone is diagnosing a
flapping audit. Non-ledger verbs are unchanged and still exit 0: for
them a crash IS the hook choosing to say nothing, which is failing open. `nx hook` keeps its Click verbs for a human at a terminal; no
`hooks.json` entry names it.

**Package.** `src/nexus/hooks/` (the existing `nexus.hooks` module that
`session-start` calls becomes `nexus/hooks/__init__.py`). One module per
retired script. A shared `_io.py` holds the payload reader, the decision
envelope writers (`hookSpecificOutput` with `permissionDecision` or
`additionalContext`, and the top-level `decision` form the stop hooks
use), and the never-fail boundary. `_config.py` resolves
`NX_ORCH_STOP_GUARD` once for the four hooks that read it inline today.

**Contracts.** The contract map (T2 `215-hook-contract-map`) lists each
script's stdin fields, stdout shapes and exit codes; the port reproduces
each byte for byte and the retargeted test asserts them. ONE is quoted
outside the tests: the ledger verbs' codes (0 clean, 1 BLINDSPOT, 2
undeclared, 3 no ledger for `undeclared`; 0, 2, 4 for `reconcile`) in
AGENTS.md and the orchestration skill. The close gate's deny text is the
opposite case and an earlier draft of this section had it backwards:
before the port exactly ONE file on disk carried the literal remedy block
-- the script itself. Scarcity is the hazard, not ubiquity. A text living
in twenty places cannot be quietly reworded; one living in a single place
can, and then the ten-odd documents describing the gate drift from what
it says with nothing to disagree with them. That is why it is pinned. `census` returns 1 on the same blind-spot shape, quoted in the
orchestration skill and asserted by value in
`tests/e2e/lib/expectations_test.sh` and
`tests/hooks/test_subagent_stop_hook.py`. `expect` and `start` return 2
on invalid input, a code no file outside the tests quotes: the e2e file
exercises `expect`'s path by success or failure, and never drives `start`
with invalid input at all. The port keeps all three codes and the Test
Plan adds the assertions that are missing.

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
`nx-session-end-launcher`, or `python3` whose sole `args` element is one
of FIVE plugin-resident scripts -- `version_lockstep_hook.py`,
`mailbox_drain.py`, `behaviour_census.py`,
`routing/phase_review_close_requires_gate.py` and
`routing/subagent_git_write_requires_orchestrator.py`, pinned by name as
`PLUGIN_RESIDENT_SCRIPTS` in `tests/test_hooks_json_shape_lint.py`
(CORRECTED 2026-09-19: this paragraph named only the lockstep, which was
true when written and became false when bead .21's tier resolution
settled the other four) -- and no `command` or `args` element equals
`bash`, `sh` or `nx` or ends in `.sh`; matching is whole-string, so
`nx-hook` is not `nx`. A `SessionStart` entry must be command tier. For
the sn `hooks.json`: every entry has `args`, `command` is exactly
`python3`, and the sole `args` element is a `.py` path under
`${CLAUDE_PLUGIN_ROOT}/hooks/scripts/`. (Launcher changed to `uv run` at nexus-j4iy0, 2026-09-23; see Revision History.)

**Tests.** Each retargeted test keeps its payload fixture and expected
bytes. Tool-tier tests call the registered tool through the server's
in-process dispatch; one integration test drives a real `nx-mcp` over
stdio for one tool. Command-tier tests spawn `nx-hook <verb>`. One
script has no test today (`sn/session-start.sh`); its port gets one.

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
- 15 conexus hooks stop spawning a process at all; six `SessionStart`
  hooks and the close gate spawn one `nx-hook` each instead of bash, the
  lockstep hook spawns `python3`, and the four sn hooks spawn `python3` [`uv`
  since nexus-j4iy0]
  instead of bash.
- The hook tools appear in the model's tool list.
- 4,200 lines of bash leave; roughly the same amount of Python arrives,
  with unit tests per module.

### Risks and Mitigations

- **A hook fires before `nx-mcp` is connected.** Mitigation: Phase 1
  measures the first `PreToolUse` after launch on macOS and WSL2; if it
  can race the connection, that event's entry gets a command-tier twin
  until the server is up.
- **`nx-hook` not on the `SessionStart` PATH.** Measured in Phase 1 on
  all three host shapes, and the PATH itself is fine (see § Critical
  Assumptions). The mitigation as written here is REFUTED and no
  replacement has been built: a command hook that cannot be found does
  not fail loud, so "the verb prints one line naming the fix" is
  structurally impossible — the verb never runs. Claude Code logs
  `Executable not found in $PATH` to the debug log only and the session
  proceeds silently. OPEN: the fail-loud line has to live somewhere that
  runs regardless — `nx doctor`, the installer, or a declaration that
  does not depend on PATH resolution. Bead `nexus-3z8vb` closed the
  related case where the shim exists but its target does not.
- **A port changes a refusal text or exit code another file quotes.**
  Mitigation: the retargeted test asserts the exact bytes; a grep for the
  quoted strings runs before each script is deleted.
- **A hook tool blocks the server.** Mitigation: hook tools do no more
  than the script did, and the one long call, the Stop hook's
  `nx catalog sync`, moves off the synchronous path into a daemon thread.
  CORRECTED 2026-09-19: deleted rather than threaded -- see Technical
  Design. The mitigation holds more strongly than planned, since the call
  is gone rather than relocated.

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
and both fired in a real Claude Code session on macOS, with the timing of
the first `PreToolUse` after launch recorded.

**What that does and does not mean.** Neither port is wired into the live
`conexus/hooks/hooks.json`, and nothing in Phase 1 touches that file — the
re-declaration is Approach item 6, deliberately Phase 3 (beads
`nexus-q02nx.21`/`.22`). So what fired live was the real port reached
through a purpose-built `hooks.json` in an isolated HOME, which proves the
DISPATCH MECHANISM and the tier's contracts; it is not the same as a
hooks.json-triggered event in a normal session reaching the ported module,
which stays open until Phase 3. On WSL2 the mechanism was proven with a
probe server and a stand-in executable rather than with nexus's own
package, which does not install on that distro (see § Critical
Assumptions).

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
5. The eight Python hooks: `stop_failure_hook.py` re-declared as a tool.
   `mailbox_drain.py` and the two routing hooks were named here as tools
   too and did NOT move -- all three reach `_endpoint_resolve.py`
   (directly, or via `routing/_lib.py`), which cannot leave
   `conexus/hooks/scripts/` while `t2_prefix_scan.py` and
   `tuple_ledger_project.py` import it and neither is ported by this
   epic. Making them wheel-resident would mean a second copy of a
   449-line resolver beside `nexus.db.service_endpoint`, which Approach
   item 9 forbids as a rewrite. They stay exec-form `python3`; full
   reasoning in T2 `nexus_rdr/215-tier-resolution-bead-21`. (That reason
   was wrong, and all three are now `nx-hook` verbs: see Approach item 1's
   2026-09-23 correction.) Then
   `preflight.py`,
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
  MET IN SHAPE ONLY UNTIL 2026-09-19: the test drove a synthetic
  `hook_stdio_probe` whose own summary read "never a real hook",
  authored correctly at bead .3 before any hook was ported and never
  revisited after bead .4 ported the first. Twelve real `hook_*` tools
  shipped and the number ever proven reachable over the wire was ZERO,
  for the whole epic, while this line read as satisfied. Closed by bead
  .31 / commit `4842e0c12`, which registers the real `HOOK_TOOLS` tuple.
- The lint test of Technical Design, both shapes, whole-string matching.
- The expectations module gets unit tests for every verb's exit codes
  against fixture ledgers: `undeclared`'s 0, 1, 2 and 3, `reconcile`'s
  0, 2 and 4, `census`'s 0 and 1, and `expect` and `start` returning 2 on
  invalid input.
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
  bundled scripts only; nothing in this RDR makes sn require conexus. (Launcher changed to `uv run` at nexus-j4iy0, 2026-09-23; see Revision History.)
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
- 2026-09-18: Accept dispositions of the round-3 residuals: the four carried from round 2 closed by `dd95743fa` (fix check `nexus_rdr/215-fix-check-dd95743fa`); the four from round 3 fixed in `faa779251`.
- 2026-09-18: `phase_review_close_requires_gate` carved out of the tool tier to the command tier; the tool tier is 15 of 24 conexus entries, not 16. SUPERSEDED 2026-09-19, see the bead `nexus-q02nx.25` entry below: the measured tool tier is 13. Raised during bead `nexus-q02nx.1` review; critique `nexus_rdr/critique-impl-nexus-q02nx.1-hooks-package`.
- 2026-09-19: Phase 1 measurements landed (bead `nexus-q02nx.6`): the
  MCP connection race is refuted — turn 1 waits for connection
  resolution — and the risk relocates to a server that never connects,
  which fails open with only a debug-log warning. `${path}` substitution
  carries non-scalars as structures, so no bead is re-tiered. A
  not-found command hook does not fail loud, which moves that
  requirement out of the verb. The SessionStart PATH is verified on all
  three host shapes: the macOS desktop app repairs the PATH for the
  children that run user tooling, so the command tier is reachable
  there. Record: T2 `nexus_rdr/215-phase1-measurements`.
- 2026-09-18: The command-tier entry point moves from `nexus.hooks.entry` to `nexus._hook_runtime.entry`, taking `_io` and `_config` with it, and the eager logging bridge in `main()` is replaced by a per-verb call plus a structural stdout guard. Measured: a stdlib-only dispatch falls from 0.06 s to 0.02 s, below the bash close gate re-measured on the same box at 0.03 s (bead .2 recorded 0.04 s in its own harness). Bead `nexus-br31l`.
- 2026-09-19: Contracts amended with a third exit case (Sam's ruling): a
  ledger verb that crashed exits a reserved 70 rather than 0, so a caller
  branching on the 0/1/2/3/4 vocabularies can tell "the audit could not
  run" from a real verdict. Found and measured during bead
  `nexus-q02nx.9`; non-ledger verbs unchanged.
- 2026-09-19: Bead `nexus-q02nx.12` found the first port had dropped both
  of `expectations_owes_report`'s operator-facing stderr diagnostics --
  the lock-exhaustion line and the credit-slot-orphan line. The cause
  still rode the ledger's 4th field, so an auditor could recover it, but
  the person watching an agent get blocked had nothing telling a
  precautionary block from a verified one. Restored through `_emit`,
  which never touches stdout. Caught by the retargeted SubagentStop
  suite, whose five pinned substrings are the only reason it was
  visible; a port checked against the decision table alone would have
  passed. This is the measured answer to the Phase 2 question of whether
  "move, do not rewrite" needs the old tests carried across before the
  script goes: it does.
- 2026-09-19: The two SubagentStop scans become `subagent_stop_scans`
  functions rather than sibling scripts. The `nexus-2gcqk` heredoc-pipe
  constraint that forced them out of the script does not apply to an
  import, and removing their two `python3` spawns is the concrete form
  the timing mitigation takes for this hook. `test_subagent_stop_hook.py`
  still drives the port through a CHILD process, because three of its
  test families set per-call environment and run six racers concurrently
  and `os.environ` is process-global; the no-spawn claim is therefore
  asserted separately and in-process, in
  `tests/hooks/test_subagent_stop_module.py`.
- 2026-09-19: Sequencing correction found starting bead
  `nexus-q02nx.14`. Deleting `conexus/hooks/scripts/expectations.sh` moves
  from `.14` (Phase 2) to `.21` (Phase 3), to land in the same change that
  re-declares the `hooks.json` entries. All four live entries still run
  bash scripts that source it, so the deletion would have preceded the
  re-declaration and removed the library from the production
  implementation, and the guard would have gone quiet with almost nothing
  to see. The exact breakdown, re-verified at the bead .16 critique after
  this entry first got it wrong: FOUR wired entries source it --
  SubagentStart/`subagent-start-stamp.sh` and
  SubagentStop/`subagent-stop.sh` as bare `|| exit 0`,
  PreToolUse/`agent-dispatch-expect.sh` with one stderr line then exit 0,
  and Stop/`stop_verification_hook.sh` unguarded, degrading silently via
  its own `command -v expectations_reconcile` check. So two of the four
  are wholly silent, one says one line, one drops a feature without
  saying anything. (`subagent-start.sh` does NOT source it and never has,
  despite this RDR's Approach naming it at line 719 as a script to port
  "since they source it" -- that line is wrong and no
  `subagent_start.py` was ever written.) `.14` already forbade this in
  its own words -- "nothing is deleted while a consumer still sources
  it" -- but its three enumerated consumer classes are test scripts,
  Python tests and prose, and the production bash hooks are a fourth the
  enumeration missed. `.14` DELETES the `tests/e2e/lib/` copy and keeps
  the PLUGIN copy; an earlier version of this entry stated that
  backwards.
- 2026-09-19: Two claims in this document were measured false during the
  Phase 3 review and corrected in place (bead `nexus-q02nx.25`;
  critique `nexus_rdr/215-bead25-critique-findings-2026-09-19`). (a) The
  TOOL TIER IS 13, not the 15 the 2026-09-18 entry above records:
  `mailbox_drain.py` and `routing/subagent_git_write_requires_orchestrator.py`
  were both named for the tool tier and both stayed command tier, for the
  `_endpoint_resolve.py` reason now written into phase item 5 (a reason
  later shown wrong; see 2026-09-23). Counted
  from the shipped manifest: 13 `mcp_tool` and 12 `command` entries, 25
  total (24 of them this epic's; `behaviour_census.py` arrived from
  nexus-4lnn1). The resolution had existed in T2 since 2026-09-19 and
  never reached this document. THE SWEEP FOR THIS NUMBER FOUND THREE OF
  ITS FOUR SITES: Contracts, phase item 5 and this history were corrected
  in `d4e744e81`, and Approach item 1 was missed and corrected separately
  after the Phase 4 critique found it (bead `nexus-q02nx.30`). Recorded
  because it is the datum, not the embarrassment: the fix pass for a
  wrong number did not begin by grepping for the number. (b) The
  Contracts section said the close
  gate's deny text was quoted in 19 files. It was quoted in ONE -- the
  script -- measured by `git grep` at `213f4515d`, and the scarcity is
  the argument for pinning it, so the claim was not merely wrong but
  backwards. Both were found by implementation and fixed elsewhere
  first; the standing lesson for this epic's close is to sweep the
  document for "verified false, fixed in code or T2, RDR text unchanged".
- 2026-09-19: Phase 3 code review (bead `nexus-q02nx.24`) returned one
  Critical and two Significant, all three fixed with tests that failed
  first. The close gate's override path had collapsed the bash's two id
  sets into one, stamping covered beads `overridden` and naming them in
  the escape log and allow text; `_bead_ids` carried two flag tables and
  the port expanded only the shlex one, so `--reason-file` and `-r`
  values leaked into the harvest on the malformed-quoting fallback (both
  tables are now derived from one constant); and the bead `.20` daemon
  thread logged only failure, so a clean projection and a thread that
  never started were the same absence in the hook log.
- 2026-09-19: Closed. All 31 beads and the epic closed; four gates green
  (unit 20787/0, lint 1273, local-service-gate 597, plugin-lockstep
  PASSED). The isolated close critique returned `partial` on one finding
  worth recording here rather than only in T2: the Critical Assumptions
  precondition above -- run the delay ladder INTERACTIVELY before Phase 2
  relies on "no command-tier twin is needed" -- was never met, and unlike
  every other open item in this epic it was absent from both closure
  records. Now bead `nexus-veh77`. The instructive part is WHERE it hid:
  the closure records tracked everything DISCOVERED during implementation
  and were blind to a constraint WRITTEN DOWN before it started, and both
  were assembled by the session that did the work. The critique that
  found it is the one the rdr-close skill deliberately gives no session
  context, reading only this document and the repo.
- 2026-09-19: Round-2 close critique found THREE more instances of this
  document's own standing lesson -- verified false, fixed in code, RDR
  text unchanged -- and the point is that the sweep had already been run
  twice (beads .25 and .30) and was not run a third time. All three are
  corrected in place above: the Lint paragraph named one plugin-resident
  python3 script where five shipped; Technical Design and Risks both said
  `nx catalog sync` moves to a daemon thread where it was deleted; and the
  Test Plan's stdio line read as satisfied for the epic's whole life while
  the test drove a placeholder. None required a code change -- the code
  was already correct in every case, which is exactly what makes this
  class survive: nothing fails, so nothing asks.
- 2026-09-23: The `_endpoint_resolve.py` reason given for keeping
  `mailbox_drain.py` and both routing guards plugin-resident was WRONG,
  corrected in place at Approach item 1, Phase 3 item 5, the 2026-09-19
  entry above, and the post-mortem. It assumed moving a script meant
  moving its stdlib mirror, and so a second copy of the resolver. The
  option nobody re-examined was to drop the mirror and call the client's
  own primitives, which `tuple_ledger_project` had already done in this
  same epic. nexus-t9klx did that for all five bare-`python3` hooks; the
  shipped `hooks.json` names no interpreter and no plugin script. The TIER
  rulings are unaffected, because each has a reason of its own recorded
  beside the correction. The reason this entry is written as a correction
  rather than an update: a constraint's shape outlives its rationale, and
  a reader who finds a reason recorded next to the decision does not
  re-derive it.
- 2026-09-23 (nexus-j4iy0): sn's four hooks launch through exec-form
  `uv run --no-project --no-config --quiet <script>` instead of `python3`,
  which stock Windows lacks. The independence ruling stands: the scripts
  stay stdlib-only and never import the conexus wheel. The dependency moved
  from `python3` to `uv`, which Serena's `uvx` launch already required; a
  user who runs sn for Context7 alone now needs uv for the hooks. The shape
  lint in `tests/test_hooks_json_shape_lint.py` pins the argv whole.
- 2026-09-23 (nexus-veh77): the interactive delay ladder that Critical
  Assumptions required before Phase 2 was run, and it REFUTES "no
  command-tier twin is needed" for interactive sessions. CLI 2.1.280, macOS
  (this dev Mac, 67 runs) and WSL2 (qwentescence, `nexus` user, 78 runs).
  Harness: `tests/cc-validation/connection-race-ladder/` (a probe MCP server
  with a start delay stands in for `nx-mcp`; each event carries a
  command-tier twin as ground truth; verdicts come from the twin log, the
  probe's own log and `--debug-file`, never the pane). Raw results: T2
  `nexus/veh77-interactive-ladder-results-2026-09-23`.
  (a) Interactive has NO connection barrier. Headless `claude -p` holds
  turn 1 until the connection attempt resolves; interactive starts turn 1
  about 50 ms after submit whatever the server's state. With the server
  connecting 8 s after launch, every submit rung from 0 to 2000 ms missed
  every tool-tier event: macOS 24/24 runs, WSL2 20/20 (its 0 ms rung lost the
  Enter, below). Controls held on both hosts: server connected long before
  submit, 2/2 fired; server exits before serving, 2/2 missed.
  (b) The skip is decided per MODEL REQUEST, not per event. A `PreToolUse`
  or `PostToolUse` from a request that began before the server connected is
  skipped even when the connection finished seconds earlier: on macOS, with
  submit at 0 ms and the probe connecting 0.15 to 2.1 s after the input box
  appeared, the first `PreToolUse` (at +1.8 to +2.7 s) missed 15/15. With four Bash calls spread over 12 s and the server
  connecting at +3.2 s, the first call's `PreToolUse` and `PostToolUse`
  missed and every later call's fired (macOS 3/3, WSL2 2/2). `Stop` fires
  when the turn's last request began after the connection. `SubagentStart`
  fired whenever the server was connected when the subagent started (3/3);
  `SubagentStop` for a subagent dispatched by a pre-connection request
  missed 3/3 even so.
  (c) The window is short but real. With a probe that needs no start delay,
  macOS missed first-request hooks at a 0 ms submit (3/3) and fired at
  250 ms and 500 ms (6/6); WSL2 missed at 100 ms and 250 ms (5/5) and fired
  at 500 ms (3/3). The input box appears before any server connects.
  (d) What closes the window today is incidental: a submitted prompt waits
  for the SessionStart command hooks. A 12 s SessionStart hook against an
  8 s server fired everything (macOS 3/3, WSL2 3/3); a 4 s hook missed
  everything (2/2 each). In the one real conexus session with a debug log on
  this Mac (2026-09-20, CLI 2.1.278) `nx-mcp` connected 2.1 s after launch,
  its connection queued about 0.9 s behind other plugin servers, and the
  last SessionStart hook finished at 8.4 s, so that session was covered.
  The cover is the SessionStart verbs being slow, which this RDR's own
  timing work pushes the other way.
  Tool-tier entries exposed: `PreToolUse Agent|Task` (the RDR-184 EXPECT
  writer), the `SubagentStop` tuple projector, `Stop` verification,
  `PostToolUse Write|Edit`, and the three `SubagentStart` entries when the
  server is not up. `PostCompact` and `StopFailure` were not measured. The
  Risks mitigation is now a design question: twin these entries on the
  command tier, or make a SessionStart verb wait for `nx-mcp` to connect.
  Not decided here.
  Two harness facts: text typed before the input box appears is kept but
  its Enter is dropped (6/6), and on WSL2 an Enter sent 0 ms after the
  status bar appeared was dropped 18/18 (at 100 ms, 5 of 31).
- 2026-09-23 (nexus-veh77, decision + close-out): Sam's ruling on the design
  question the entry above left open: a `SessionStart` command-tier verb
  waits, bounded and fail-open, for `nx-mcp` to connect, rather than
  twinning every exposed tool-tier entry on the command tier. Shipped as
  `nx-hook mcp-connect-wait` (`nexus.hooks.mcp_connect_wait`), wired in
  `conexus/hooks/hooks.json` under the `startup` matcher only.
  **Readiness signal.** `nexus.mcp.core._t1_lifespan` Branch 0 publishes
  this session's `t1_session_lease.<session_id>` file (`nexus.db.t1.
  publish_t1_session_lease`) inside its mint-or-borrow critical section,
  before the lifespan's own `yield` -- and an MCP server built on the `mcp`
  SDK's lifespan contract cannot answer `initialize` until that `yield`
  returns and the transport's request loop starts. So the lease file is
  written on the causal path to "connected", not sampled after the fact,
  and its `session_id` is byte-identical to the SessionStart payload's own
  field (both resolve through `CLAUDE_CODE_SESSION_ID`, harness-set at
  spawn). The verb polls `read_t1_session_lease` for THIS session's id
  every 0.2 s.
  **Which sources wait.** `startup` only. JDR-001 and its own
  `nexus-ggvi0` falsification establish that the MCP process usually
  PERSISTS across `/clear`, `/resume`, `/compact` and a fork, so the
  connection this verb waits for already exists on every other
  `SessionStart` source; waiting there would only add latency.
  **The bound.** 15 s, from the ladder's own measurements: the one real
  `nx-mcp` connect recorded from a live session's debug log was 2.1 s
  (queued ~0.9 s behind other plugin servers); every `ladder_s8` probe
  rung connected by design at 8.15 s (macOS) / 8.5 s (WSL2) and is the
  widest delay this RDR measured. 15 s is a little under 2x the widest
  measured connect and about 7x the one live-session connect, and stays
  under `upgrade-auto`'s own 30 s ceiling in the same matcher group.
  **Fail-open.** Not a ledger verb -- `nx-hook`'s dispatcher forces exit 0
  regardless -- and a timeout logs one line
  (`mcp_connect_wait_timed_out`) to the hook log, never stdout, then
  returns the identical silent result a successful wait returns.
  **Proof.** Unit tests (`tests/hooks/test_mcp_connect_wait_verb.py`,
  11 cases): the polling primitive against REAL lease files (ready
  immediately, ready after N polls via an injected fake clock, never
  ready and times out, a lease for a DIFFERENT session id never read as
  ready, an expired lease reads as absent) and the verb's own wiring
  (only `source=startup` waits, a missing session id is a fast no-op, a
  timeout still returns a silent exit-0 `HookResult`, the test-only
  bound/poll env overrides are honoured end to end through `run()`).
  Then the interactive ladder itself, re-run on macOS with `--barrier`
  (`tests/cc-validation/connection-race-ladder/run_ladder.py`, the probe
  now publishing the same lease-file signal at the point in its own
  timeline that stands in for "connected", the barrier invoking the REAL
  `nx-hook mcp-connect-wait` verb in-process, both via the worktree's own
  `.venv` python).
  **A harness confound, found and fixed before the numbers below are
  trustworthy.** The barrier's `SessionStart` command entry chains three
  `;`-separated commands sharing ONE stdin pipe (Claude Code writes the
  payload once); the first, a command-tier "twin" logger that also does
  `json.load(sys.stdin)` for its own bookkeeping, drained the payload
  before the real verb's own `read_payload()` ran second, which then saw
  an already-EOF stdin, read `None`, and took the `source is None` fast
  no-op path -- the barrier measured ~50ms elapsed instead of waiting, and
  the first re-run reproduced round 1's misses unchanged (a false
  negative on the fix, not evidence against it). Fixed by redirecting
  `< /dev/null` onto both twin-logger calls in the chain, leaving the
  shared pipe untouched for the verb's own read. Recorded because it is
  exactly this RDR's own recurring lesson in miniature: two consumers of
  one shared resource, one silently starving the other, discovered only
  by reading the per-run timestamps rather than trusting the summary line.
  **After (macOS, `barrier.plan`, 2 reps per rung, the SAME `ladder_s8`
  submit rungs that missed 4/4 in round 1):**

  | label | S | submit | barrier | runs | UPS | PreToolUse | PostToolUse | Stop | SubagentStop |
  |---|---|---|---|---|---|---|---|---|---|
  | pos_control | 0 | 8000 | on | 2 | 2/0/0 | 2/0/0 | 2/0/0 | 2/0/0 | 2/0/0 |
  | neg_control (broken) | 0 | 3000 | on | 2 | 0/2/0 | 0/2/0 | 0/2/0 | 0/2/0 | 0/2/0 |
  | ladder_s8 | 8 | 0 | on | 2 | **2/0/0** | **2/0/0** | **2/0/0** | **2/0/0** | 2/0/0 |
  | ladder_s8 | 8 | 1000 | on | 2 | **2/0/0** | **2/0/0** | **2/0/0** | **2/0/0** | 1/0/1 |
  | ladder_s8 | 8 | 2000 | on | 2 | **2/0/0** | **2/0/0** | **2/0/0** | **2/0/0** | 2/0/0 |
  | thresh | 0 | 0 | on | 2 | 0/2/0 | 0/2/0 | 0/2/0 | 2/0/0 | 2/0/0 |

  (fired/missed/not\_reached; bold = flipped from round 1's 0/4/0 or 0/3/1
  to fired.) The controls are unchanged from round 1 (`pos_control` still
  fires clean, the broken-server `neg_control` still fails open with a
  normal miss, never a hang), and `thresh` at a genuinely fast, zero-delay
  connect is unchanged too -- the barrier costs nothing when there is
  nothing to wait for. Per-run timestamps on three `ladder_s8` reps show
  the mechanism directly: the verb's own `BarrierBegin`-to-`BarrierEnd`
  span was 8.29-8.30s (matching the probe's 8s start delay), and each
  returned 0.22-0.24s after the probe's lease-publish event -- one poll
  interval, well inside the 15s bound, on the exact rungs round 1 measured
  missing every tool-tier event 4 times out of 4.
  WSL2 and `PostCompact`/`StopFailure` remain unmeasured, as the round-1
  entry above already recorded.
