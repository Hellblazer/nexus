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

To be written after the research above.

### Technical Design

To be written after the research above.

### Decision Rationale

To be written.

## Alternatives Considered

To be written. Candidates to weigh: Starlark (Python-like, no loops without
bounds); a WebAssembly sandbox; JavaScript through an embedded engine; no
runtime (a Python verb per procedure, RDR-240 Alternative 1).

## Trade-offs

### Consequences

To be written.

### Risks and Mitigations

To be written.

## Implementation Plan

To be written.

## Test Plan

To be written. It must include escape tests (file, network, process, module
loading, bytecode, debug library, metatable tricks) and limit tests (an
infinite loop, unbounded allocation, a huge result), each failing with an
error that names what it hit.

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
