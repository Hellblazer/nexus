---
title: "A Sandboxed Lua Runtime over the Nexus Surface: Client-Side, Read-Only First"
id: RDR-242
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-11-02
accepted_date:
related_issues: ["nexus-mdbds.1", "nexus-mdbds"]
related_rdrs: [RDR-239, RDR-240, RDR-110, RDR-111, RDR-205]
---

# RDR-242: A Sandboxed Lua Runtime over the Nexus Surface: Client-Side, Read-Only First

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Stage 3 of the scriptable tuple space (epic nexus-mdbds),
pulled ahead of stage 2 (declarative auras) on 2026-10-31 at Sam's direction,
because procedures move to Lua and RDR-240 needs a runtime to run them in.
Stage 1, the tuple-space dictionary (RDR-239), shipped and closed on
2026-11-02. RDR-240 cannot be accepted before this RDR is (its assumption
A1).

## Problem Statement

Nexus has no way to run a short program that someone other than a nexus
developer wrote. Every new behaviour (a query over transcripts, a check
before a push, a reaction to a tuple) costs a Python verb or an MCP tool: code
in the package, a schema, tests and upkeep. The models in the epic are
WeakAuras and Augur: a small, embedded language over an API the host chooses,
with the rules enforced by the host rather than by the script's author.

A runtime is a security boundary before it is a convenience. The script may
come from a repo, a plugin or an agent; whatever it can reach, a hostile or
careless script can reach too. So the first version is client-side, reads
only, and reaches only what the host names.

### Enumerated gaps to close

#### Gap 1: There is no sandboxed interpreter in nexus

Nothing in `src/nexus/` runs untrusted code under limits. RDR-240's
infrastructure audit lists "sandboxed interpreter: none". A Python `exec`
cannot be sandboxed in-process, so the interpreter has to be a separate
language with no ambient authority: no file system, no network, no process
spawning, no module loading beyond what the host hands it.

#### Gap 2: There is no named, closed surface for a script to call

The tuple-space dictionary (RDR-239) describes every subspace's meaning,
writers, readers and an example post, but nothing turns that description into
callable functions with a fixed set of verbs. The read verbs RDR-240 needs
(`transcripts.sessions`, `memory.get`/`list`/`search`, `git.diff_names`,
`git.diff_stat`, `tables.get`) exist only as Python internals or MCP tools,
not as a surface a script can be granted piece by piece.

#### Gap 3: There are no limits a script cannot escape

A script that loops forever, allocates without bound, or returns a result
larger than the caller can hold must fail with an error that names the limit,
not hang the MCP server or the CLI. Nothing today bounds instructions, memory,
wall time or result size for code the host runs on someone else's behalf.

#### Gap 4: There is no rule for granting a write

RDR-240's `gate_record` and `research_add` write one T2 record. Read-only
first means the runtime ships with no write verbs, but it must say how a write
is granted later (declared in the script's header, named per verb, refused
otherwise) so that the read-only version does not have to be redesigned when
the first write arrives.

## Relationship to Prior RDRs

- **RDR-239 (closed scope, stage 1):** the dictionary is the description of
  the tuple surface this runtime exposes. A tuple verb in the runtime is
  callable only for a subspace whose template the dictionary describes.
- **RDR-240 (draft):** the first consumer. Its Technical Design item 4 lists
  the host verbs it needs; its A1 depends on this RDR's write-grant rule.
- **RDR-110 (abandoned):** the semantic tuple space was scrapped for building
  for every consumer at once. This RDR builds for one consumer (RDR-240) and
  adds verbs only when a named consumer needs them.
- **RDR-111, RDR-205:** the tuple space and the mailbox, whose read verbs a
  later script may be granted.

## Context

### Background

Sam's direction (2026-10-31, bead nexus-mdbds.1): client-side first, using
`lupa` (Python bindings to Lua or LuaJIT); read-only first; expose only what
the dictionary describes plus named read verbs over memory, search, catalog
and transcripts; no shell, no file system, no network. Model: Augur (Lua over
a sanctioned API, with host-enforced rules like World of Warcraft's Secret
Values, where some returned values can be passed on but not inspected) and
WeakAuras custom code.

### Technical Environment

- Python 3.12+ client (`nx` CLI and the MCP server), shipped on PyPI as
  `conexus`, installed by `uv` on macOS (arm64), Linux (x86_64, arm64) and
  native Windows.
- The engine (Java) is out of scope for this RDR: scripts run in the client
  process, and reach the engine only through the client's existing HTTP
  clients.
- No Lua dependency today (`lupa` is absent from `pyproject.toml` and
  `uv.lock`, checked 2026-11-02).

## Research Findings

### Investigation

Open questions the research has to settle before a design is written:

1. **Interpreter and binding.** `lupa` ships wheels bundling Lua 5.1 to 5.4
   and LuaJIT. Which runtime, on which platforms (macOS arm64, Linux, native
   Windows, CPython 3.12 to 3.14), with wheels and no compiler at install?
2. **Sandbox construction.** What `lupa`'s `LuaRuntime` exposes by default
   (`register_eval`, `unpack_returned_tuples`, `attribute_filter`,
   `python` module access), and how to start from an empty environment so no
   `os`, `io`, `require`, `load` with bytecode, `debug` or `string.dump`
   reaches the script.
3. **Limits.** Instruction counting (`debug.sethook` count hook, or LuaJIT's
   lack of reliable hooks in compiled traces), memory caps (`lupa`'s
   `max_memory`, available only on some runtimes), wall time, and result size.
   Which of these hold under LuaJIT, and which only under PUC Lua?
4. **Crossing the boundary.** How Python objects are passed in (copied as
   tables, never as live proxies with attribute access) and how results come
   out as plain JSON values.
5. **Write grants.** The header-declared grant RDR-240 assumes: what the host
   checks at load and at call, and what a script sees for an ungranted verb.
6. **Prior art.** How WeakAuras, Augur, Redis (`EVAL`), Neovim and
   Roblox/Luau sandbox Lua; known escapes from Lua sandboxes (bytecode
   loading, `string.dump`, metatable tricks, `__gc`).

### Key Discoveries

Research survey of 2026-11-02 (T2 `nexus_rdr/242-research-survey-2026-11-02`;
probe scripts and their outputs in `scripts/spikes/rdr242-lua-sandbox/`).
Runtimes: `lupa` 2.8; PUC Lua 5.1 to 5.5 on macOS arm64 under CPython 3.12;
LuaJIT 2.1 in an aarch64 Linux container. "Verified" means the probe ran.

- **Verified: the empty environment holds.** A script loaded by the host
  into an empty environment table, with `_G` wiped and `lupa`'s
  `register_eval` and `register_builtins` off, sees nil for `io`, `os`,
  `require`, `load`, `loadstring`, `dofile`, `loadfile`, `debug`,
  `package`, `python`, `getmetatable`, `setmetatable`, `getfenv`,
  `setfenv`, `ffi`, `jit` and `_G`, on all five PUC versions and LuaJIT
  (30 probes each).
- **Verified: LuaJIT cannot be limited.** With the JIT on, the instruction
  count hook never fires inside compiled loops: a `while true`, a `for` loop
  and a hot call each ran to a 30 s timeout. They stop only with the JIT off
  for the whole runtime. `lupa` also cannot cap memory on 64-bit LuaJIT
  (`max_memory` fails at construction, and the docstring says so). LuaJIT is
  in no macOS wheel either.
- **Verified: Lua 5.4 can be limited.** The count hook stops all three
  loops at once; a 64 MB `max_memory` cap raises `LuaMemoryError` for
  `string.rep` and for table growth; unbounded recursion raises "stack
  overflow".
- **Verified: a script-created coroutine escapes the count hook on 5.4,
  and a host wrapper closes it.** A loop inside a new coroutine ran to the
  timeout with the hook set on the main thread. Cause: the debug library
  keeps the Lua hook function per thread. A new thread inherits the hook's
  count (`debug.gethook` inside it reports 100000) but has no function
  registered, so nothing runs when the hook fires. A host-provided
  `coroutine.create` that calls `debug.sethook(co, hook, "", n)` on each new
  coroutine stops the same loop at once (`co_probe.py`, cases `native`,
  `hookmask`, `wrapped`).
- **Verified: bytecode loads on 5.1 and LuaJIT.** Their `loadstring` path
  runs a binary chunk; 5.2 to 5.5 refuse one when the host compiles in mode
  `"t"`. `string.dump` stays reachable through the string metatable
  (`("").dump`) and returns bytecode; harmless while no loader is in reach.
- **Verified: host objects leak.** A Python object passed in is a live
  proxy. An `attribute_filter` that denies `_` names still let a plain
  attribute holding the `os` module through, and `getcwd()` and `system`
  ran. A closure created before the sandbox and passed in returns the full
  `_G`. Tables returned to Python are proxies too, not copies.
- **Verified: no result cap.** A 200 MB string came back in 0.32 s.
- **Verified (PyPI file lists, one bare-container install): wheels cover
  every supported platform.** `lupa` 2.8 has wheels for CPython 3.12 to 3.14
  on macOS arm64, Linux x86_64 and aarch64 (manylinux and musllinux) and
  Windows amd64, and an abi3 wheel for macOS x86_64; installing needs no
  compiler.
- **Documented: prior art agrees on the hazards.** Redis's 2015 `EVAL`
  escape was bytecode through `loadstring`; Luau removes `load`,
  `string.dump` and bytecode loading and keeps globals read-only per script;
  WeakAuras binds custom code to an empty environment with `setfenv` and
  blocks `loadstring`, `getfenv` and `setfenv`. No public source was found
  for Augur.
- **Verified (code read): of RDR-240's verbs, only the memory reads exist.**
  `memory.get` and `memory.search` map to the MCP functions in
  `src/nexus/mcp/core.py`, and `memory.list` to `db.list_entries`; the MCP
  functions return formatted text, so the host needs structured forms.
  `transcripts.sessions` has no function. `git.diff_names` and
  `git.diff_stat` have near neighbours (`_git_changed_since` in
  `src/nexus/indexer.py`, `diff_stat_block` in
  `src/nexus/commands/command_context.py`) that do not fit. `tables.get`
  needs a name-to-file mapping over `load_packaged_table`.

- **Verified: the dispatcher design holds** (`design_probe.py`, Lua 5.4).
  A single Python dispatcher taking and returning JSON strings, reached only
  through Lua functions built in a chunk with an empty environment, with an
  `attribute_filter` that refuses every attribute: the granted verb returns
  its JSON, an ungranted verb is refused by name, `("").dump` is nil after
  the string metatable's `__index` is replaced, none of 13 dangerous
  globals is visible, and a binary chunk is refused before compile.

What the probes force on any design: PUC Lua 5.4; host-side compile in mode
`"t"` and a refusal of any chunk that starts with byte 27 (ESC); plain
values in and a host-walked JSON conversion out, with depth and size caps;
no host object and no pre-built closure in the environment; `coroutine`
only through a host wrapper that arms the hook on each new thread, or not at
all in the first version; a result cap on the host.

### Critical Assumptions

- **A1:** A `lupa` wheel exists for every platform and CPython version
  conexus supports, so adding it needs no compiler. Verification: the wheel
  list on PyPI and an install on native Windows. Status: the wheel lists hold
  and a Linux aarch64 install ran; the native Windows install is not yet run.
- **A2:** A script can be held to an instruction or time limit and a memory
  limit that it cannot disable from inside. Verification: escape tests in the
  Test Plan, run against the chosen runtime. Status: holds for Lua 5.4, with
  coroutines only through the hook-arming wrapper; fails for LuaJIT; wall
  time not yet probed.
- **A3:** The read verbs RDR-240 needs can be served from the client without
  new engine routes. Verification: map each verb to an existing client call.
  Status: no engine route is needed, but four of the seven verbs need new
  client code (transcripts, both git verbs, tables by name).

## Proposed Solution

### Approach

One Python function runs one Lua 5.4 script in a fresh, empty interpreter
and returns plain JSON. The script reaches the outside world only through
verbs the host puts into its environment, each one a named read, and only
the verbs the script's caller grants. Every limit is enforced by the host,
from outside the script: instructions, memory, wall time and result size.
The first version has no write verb and runs only scripts from a library
on disk (RDR-240's procedure libraries), never source an agent typed.

The surface is built for one consumer. Version 1 carries exactly the read
verbs RDR-240's first four procedures need (its Technical Design item 4).
Tuple-space verbs, described by the dictionary (RDR-239), arrive with the
consumer that needs them, the aura stage of nexus-mdbds, through the same
grant rule.

### Technical Design

1. **Runtime.** `lupa.lua54` (PUC Lua 5.4) through `lupa>=2.8,<3`. LuaJIT is
   never loaded: its count hook skips compiled code and `lupa` cannot cap
   its memory (Key Discoveries). One `LuaRuntime` per run, discarded after
   it, so no state carries from one script to the next. Constructed with
   `register_eval=False`, `register_builtins=False`,
   `unpack_returned_tuples=False`, `max_memory` set, and an
   `attribute_filter` that refuses every attribute access.
2. **Environment.** Built by host Lua code that runs before the script and
   is then dropped. The script's environment is a new table holding copies
   of: the safe base functions (`assert`, `error`, `ipairs`, `next`,
   `pairs`, `pcall`, `select`, `tonumber`, `tostring`, `type`, `rawequal`,
   `rawlen`); `string` without `dump`; `table`; `math`; `utf8`; and the
   granted verbs. The string metatable's `__index` is replaced by the same
   reduced `string` table, so `("").dump` is nil. `coroutine` is absent in
   version 1. When a consumer needs it, it comes only as a host wrapper
   whose `create` arms the hook on each new thread (Key Discoveries). The
   real `_G` is wiped once the environment is built.
3. **Loading.** The host refuses source whose first byte is 27 (a binary
   chunk), then compiles it with `load(src, name, "t", env)`. The script's
   source comes from a file the runner read from a library root. The
   runtime takes no source from a tool argument.
4. **Verbs.** One Python dispatcher, `dispatch(verb, args_json) ->
   result_json`, is the only Python object that crosses into Lua. A host
   chunk loaded with an environment holding only that dispatcher builds one
   Lua function per granted verb, which encodes its arguments, calls the
   dispatcher and decodes the result. The script cannot read those
   functions' upvalues, since `debug` is absent. Every value that crosses is
   a JSON string, so no Python object, proxy or closure reaches the script.
   The dispatcher refuses a verb not granted for this run, by name. It
   never raises into Lua: it catches every exception and returns an error
   object as JSON, and the verb function raises a Lua error carrying that
   message as a string. Otherwise `pcall` hands the script the Python
   exception itself, a host object (`design_probe.py`, case `exc_attr`:
   the refuse-all filter held, and only `tostring` worked, but the rule
   must not rest on the filter alone).
5. **Version 1 verbs (read-only).** `memory.get(project, title)`,
   `memory.list(project)`, `memory.search(query, project, limit)`,
   `transcripts.sessions(since, until, project, bodies)`,
   `git.diff_names(base)`, `git.diff_stat(base)`, `tables.get(name)`. Each
   returns structured values, not the formatted text the MCP tools print:
   the memory verbs call the T2 store layer (`db.resolve_title`,
   `db.list_entries`, `db.search`); the other four are new client functions
   (Key Discoveries, A3). `transcripts.sessions` never returns message
   bodies unless `bodies` is true.
6. **Grants.** A script's caller passes the set of verbs to grant. RDR-240's
   runner takes that set from the procedure's header table. A requested
   verb that does not exist, or a write verb before one ships, fails the run
   before the script starts, naming the verb. The rule for a write verb
   later is the same: it is granted only when named, and it is a separate
   entry in the verb table, so read-only scripts never see it.
7. **Limits.** Defaults, each overridable downward by the caller and never
   upward:
   - instructions: 50 million, counted by a hook every 10,000 instructions;
   - memory: 64 MB (`max_memory`);
   - wall time: 30 s, checked by the same hook against a host clock captured
     in its closure;
   - result: 1 MB of JSON and 32 levels of nesting, checked while the host
     walks the returned value.
   A run that hits a limit fails with `ScriptLimitError(limit, value)`.
8. **Results.** The host walks the returned Lua value into JSON types itself
   (nil, boolean, number, string, array-like and string-keyed tables). A
   function, a userdata or a table with mixed keys fails the run with a named
   error. The caller never receives a `lupa` proxy.
9. **Entry point.** `nexus.lua_runtime.run_script(source, name, args,
   grants, limits=None) -> JSON value` is the module's whole public surface.
   RDR-240's `run_procedure` calls it. Neither the CLI nor MCP calls it
   directly in this RDR.

### Decision Rationale

Lua 5.4 is the only candidate the probes could hold to all four limits from
outside, and `lupa` ships it as a wheel for every platform conexus supports.
Passing only JSON strings across the boundary removes the three leaks the
probes found (host-object attributes, a pre-built closure, table proxies) by
construction rather than by filtering. Library-only source and no write
verb make version 1's worst case a read of something the user can already
read, so an interpreter bug in Lua is a denial of service, not a data
change.

## Alternatives Considered

### Alternative 1: LuaJIT

**Description:** the faster runtime `lupa` also ships.

**Pros:** much faster loops.

**Cons:** the count hook does not fire in compiled code, so a loop cannot be
stopped unless the JIT is off for the whole runtime; `lupa` cannot cap its
memory on 64-bit; absent from every macOS wheel.

**Reason for rejection:** two of the four limits fail, and the platform the
project runs on daily has no LuaJIT wheel.

### Alternative 2: A worker process per script

**Description:** run each script in a child process with OS limits
(`RLIMIT_AS`, `RLIMIT_CPU`, a kill on timeout) instead of in-process hooks.

**Pros:** survives interpreter bugs that corrupt memory; limits enforced by
the OS.

**Cons:** a process start per run; no `RLIMIT_AS` enforcement on macOS; the
verbs still run in the parent, so every verb call is an IPC round trip.

**Reason for rejection:** not needed while scripts come only from libraries
and nothing writes. It is the planned hardening if source from agents or
from other users is ever accepted.

### Alternative 3: Starlark, WebAssembly or embedded JavaScript

**Description:** other embeddable languages with sandboxing stories.

**Pros:** Starlark has no unbounded loops by design; WebAssembly has strong
isolation.

**Cons:** Sam chose Lua for the WeakAuras and Augur model; each needs a
native dependency with less wheel coverage than `lupa`.

**Reason for rejection:** the direction is Lua, and Lua 5.4 meets the
limits.

### Alternative 4: No runtime, a Python verb per procedure

RDR-240's Alternative 1; rejected there for the cost per procedure.

## Trade-offs

### Consequences

- A new runtime dependency, `lupa`, with a native extension in every
  install.
- Scripts are slower than LuaJIT would be; RDR-240's A2 (a 30-day transcript
  query under a minute) is measured against Lua 5.4.
- Every new verb is Python code with a test, reviewed like any other API.

### Risks and Mitigations

- **A Lua interpreter bug escapes the sandbox** (Redis shipped one in 2015,
  bytecode through `loadstring`). Mitigation: library-only source, no
  write verbs, a pinned `lupa` range raised deliberately, and Alternative 2
  before any untrusted source is accepted.
- **A `lupa` release changes defaults** (for example a builtin that becomes
  registered). Mitigation: the escape suite runs in the normal PR suite and
  fails on any new global.
- **The hook interval lets a costly C call run long** (one `string.rep`
  call is one instruction). Mitigation: the memory cap bounds it; the wall
  time check runs at the next hook.
- **Native Windows install fails.** Mitigation: Phase 1 Step 1 installs it
  on qwent's Windows side before anything else lands.

## Implementation Plan

### Phase 1: The sandbox

#### Step 1: Dependency

Add `lupa>=2.8,<3`; install on macOS arm64, Linux x86_64 (chas) and native
Windows (qwent) and run one script on each.

#### Step 2: Runtime, loading, results and limits

`src/nexus/lua_runtime/` with Technical Design items 1 to 4 and 7 to 9, and
the escape and limit tests, with no verbs yet.

### Phase 2: The read verbs

#### Step 1: Memory and tables

The three memory verbs over the T2 store layer, and `tables.get` with its
name-to-file map.

#### Step 2: Git and transcripts

`git.diff_names`, `git.diff_stat` and `transcripts.sessions`.

### Phase 3: Hand-off

RDR-240's runner calls `run_script`; this RDR closes when its first
procedure runs through it.

## Test Plan

- **Escape suite** (ported from `scripts/spikes/rdr242-lua-sandbox/`): every
  global listed in Key Discoveries is nil; `("").dump` is nil; a binary chunk
  is refused before compile; no attribute of the dispatcher is reachable; a
  verb not granted is refused by name; a verb error reaches the script as
  a string, never a Python exception object; a script cannot reach a verb
  function's upvalues; a returned function, userdata or mixed table fails
  with a named error.
- **Limit suite:** `while true`, a long `for` loop and a hot call stop on the
  instruction limit; `string.rep` and table growth stop on the memory limit;
  unbounded recursion fails with a stack overflow; a loop that sleeps
  through a verb stops on wall time; a 2 MB result and 40-deep nesting stop
  on the result cap. Each asserts the limit named in the error.
- **Isolation:** a global set by one run is absent in the next.
- **Verbs:** each verb against the real T2 store (engine substrate) and a
  real git repository in a temp directory; `transcripts.sessions` against
  fixture transcripts, with and without `bodies`.
- **Platforms:** the escape and limit suites run on macOS, Linux and native
  Windows (`scripts/windows_hand_test.py`).
- Each new test is shown to fail with the code it guards removed, and the
  red run is recorded on its bead.

## Finalization Gate

### Contradiction Check

### Assumption Verification

### Scope Verification

### Cross-Cutting Concerns

### Proportionality

## References

- Bead nexus-mdbds.1; epic nexus-mdbds.
- `docs/rdr/rdr-239-tuple-space-dictionary.md`
- `docs/rdr/rdr-240-procedures-as-sandboxed-lua-scripts.md`

## Revision History

- 2026-11-02: drafted (problem, context, research questions).
- 2026-11-02: research survey folded into Key Discoveries and the
  assumptions' status.
- 2026-11-02: Proposed Solution, Alternatives, Trade-offs, Implementation
  Plan and Test Plan drafted from the findings.
