---
title: "Registered, Approved Lua Scripts in a Sandboxed Runtime: Client-Side, Read-Only First"
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

# RDR-242: Registered, Approved Lua Scripts in a Sandboxed Runtime: Client-Side, Read-Only First

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

Provenance. Stage 3 of the scriptable tuple space (epic nexus-mdbds),
pulled ahead of stage 2 (declarative auras) on 2026-10-31 at Sam's direction,
because procedures move to Lua and RDR-240 needs a runtime to run them in.
Stage 1, the tuple-space dictionary (RDR-239), shipped and closed on
2026-11-02. RDR-240 cannot be accepted before this RDR is (its assumption
A1). Rewritten on 2026-11-03 at Sam's direction (bead nexus-mdbds.1): no
arbitrary Lua at run time; a script runs only after it is registered,
checked, trial-run and approved by Sam.

## Problem Statement

Nexus has no way to run a short program that someone other than a nexus
developer wrote. Every new behaviour (a query over transcripts, a check
before a push, a reaction to a tuple) costs a Python verb or an MCP tool: code
in the package, a schema, tests and upkeep. The models in the epic are
WeakAuras and Augur: a small, embedded language over an API the host chooses,
with the rules enforced by the host rather than by the script's author.

A runtime is a security boundary before it is a convenience, and it has two
questions to answer. The first is what a running script can reach; a sandbox
answers it. The second is whether a given script should run at all; a
sandbox cannot answer that, because it treats every script the same. The
scripts will often be written by agents, and an agent can be steered by text
it read. So the first version is client-side, reads only, reaches only what
the host names, and runs only scripts Sam has read and approved.

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
is granted later (declared when the script is registered, named per verb,
covered by the approval, refused otherwise) so that the read-only version
does not have to be redesigned when the first write arrives.

#### Gap 5: Nothing decides which scripts may run

A sandbox that accepts source at run time runs whatever source it is handed.
The source could come from an agent whose context held a prompt injection,
from an MCP client that is not what it claims, or from a registrant who wants
the script to do something its description hides. The sandbox is then the
only defence, and an interpreter bug or a limit that can be dodged (Key
Discoveries lists three found by the probes) is a defence that failed. There
is no record of which scripts exist, who wrote them, what they may call,
whether anyone has read them, or how to stop one. Sam's direction (2026-11-03):
no arbitrary Lua; scripts are registered, evaluated, approved by Sam only, and
then run.

## Relationship to Prior RDRs

- RDR-239 (closed scope, stage 1): the dictionary is the description of
  the tuple surface this runtime exposes. A tuple verb in the runtime is
  callable only for a subspace whose template the dictionary describes.
- RDR-240 (draft): the first consumer. Its Technical Design item 4 lists
  the host verbs it needs; its A1 depends on this RDR's write-grant rule.
  This rewrite changes one of its premises: a procedure file in a library
  root is what a registration reads, not what a run reads, and
  `run_procedure(name, args)` resolves the name to its approved version
  (Technical Design 8) before it calls `run_script`. RDR-240 needs that edit
  before its gate; this RDR does not make it.
- RDR-110 (abandoned): the semantic tuple space was scrapped for building
  for every consumer at once. This RDR builds for one consumer (RDR-240) and
  adds verbs only when a named consumer needs them.
- RDR-111, RDR-205: the tuple space and the mailbox, whose read verbs a
  later script may be granted. The tuple space is also the rejected home for
  the registry (Alternative 7).

## Context

### Background

Sam's direction (2026-10-31, bead nexus-mdbds.1): client-side first, using
`lupa` (Python bindings to Lua or LuaJIT); read-only first; expose only what
the dictionary describes plus named read verbs over memory, search, catalog
and transcripts; no shell, no file system, no network. Model: Augur (Lua over
a sanctioned API, with host-enforced rules like World of Warcraft's Secret
Values, where some returned values can be passed on but not inspected) and
WeakAuras custom code.

Sam's direction (2026-11-03, same bead): no raw source at run time.
`run_script` takes a registered name, a pinned version (the content's
sha256) and arguments. Registry entries carry the source, its hash, declared
grants, the registrant and a state (pending, approved, revoked). Evaluation
comes before approval: static checks and a sandboxed trial run on fixture
inputs with an output-shape check. Grants are enforced at run time, and the
sandbox stays as the second layer. Approval is by Sam only, through a CLI
verb whose credential no other principal holds, at least while the runtime
is read-only.

### Technical Environment

- Python 3.12+ client (`nx` CLI and the MCP server), shipped on PyPI as
  `conexus`, installed by `uv` on macOS (arm64), Linux (x86_64, arm64) and
  native Windows.
- Scripts run in the client process. The registry lives in the engine
  (Java, PG 17 through Liquibase), as every new persistent state does under
  the NO-SQLITE rule; Technical Design 2 says why it is an engine table and
  not T2 memory or a tuple subspace.
- The engine already distinguishes principals: `RequestContext.Principal`
  carries the tenant, the session and a server-assigned scope (`root`,
  `tenant`, `mint`, `data`; `service/src/main/java/dev/nexus/service/http/RequestContext.java`,
  read 2026-11-03). The registry records the registrant from it, never from
  a value the client asserts.
- No Lua dependency today (`lupa` is absent from `pyproject.toml` and
  `uv.lock`, checked 2026-11-02). `cryptography` 50.0.2 is in `uv.lock` as a
  transitive dependency (checked 2026-11-03); this RDR makes it direct.
- OpenSSH's `ssh-keygen` (10.3p1 on the laptop, 2026-11-03) signs and
  verifies SSHSIG signatures. It is needed only where Sam approves; runs
  verify in-process.

## Research Findings

### Investigation

Open questions the research had to settle before a design is written:

1. Interpreter and binding. `lupa` ships wheels bundling Lua 5.1 to 5.4
   and LuaJIT. Which runtime, on which platforms (macOS arm64, Linux, native
   Windows, CPython 3.12 to 3.14), with wheels and no compiler at install?
2. Sandbox construction. What `lupa`'s `LuaRuntime` exposes by default
   (`register_eval`, `unpack_returned_tuples`, `attribute_filter`,
   `python` module access), and how to start from an empty environment so no
   `os`, `io`, `require`, `load` with bytecode, `debug` or `string.dump`
   reaches the script.
3. Limits. Instruction counting (`debug.sethook` count hook, or LuaJIT's
   lack of reliable hooks in compiled traces), memory caps (`lupa`'s
   `max_memory`, available only on some runtimes), wall time, and result size.
   Which of these hold under LuaJIT, and which only under PUC Lua?
4. Crossing the boundary. How Python objects are passed in (copied as
   tables, never as live proxies with attribute access) and how results come
   out as plain JSON values.
5. Write grants. The declared grant RDR-240 assumes: what the host checks
   at registration, at approval and at call, and what a script sees for an
   ungranted verb.
6. Prior art. How WeakAuras, Augur, Redis (`EVAL`), Neovim and
   Roblox/Luau sandbox Lua; known escapes from Lua sandboxes (bytecode
   loading, `string.dump`, metatable tricks, `__gc`).
7. Static checks (added 2026-11-03). Can a checker refuse every escape
   class before a script runs, without a Lua parser of its own that could
   disagree with Lua's?
8. Registry and approval (added 2026-11-03). Where the registry lives, what
   makes an approval unforgeable, and what a revocation, an edit or a
   swapped source does to a run.

### Key Discoveries

Research survey of 2026-11-02 (T2 `nexus_rdr/242-research-survey-2026-11-02`;
probe scripts and their outputs in `scripts/spikes/rdr242-lua-sandbox/`).
Runtimes: `lupa` 2.8; PUC Lua 5.1 to 5.5 on macOS arm64 under CPython 3.12;
LuaJIT 2.1 in an aarch64 Linux container. "Verified" means the probe ran.

- Verified: the empty environment holds. A script loaded by the host
  into an empty environment table, with `_G` wiped and `lupa`'s
  `register_eval` and `register_builtins` off, sees nil for `io`, `os`,
  `require`, `load`, `loadstring`, `dofile`, `loadfile`, `debug`,
  `package`, `python`, `getmetatable`, `setmetatable`, `getfenv`,
  `setfenv`, `ffi`, `jit` and `_G`, on all five PUC versions and LuaJIT
  (30 probes each).
- Verified: LuaJIT cannot be limited. With the JIT on, the instruction
  count hook never fires inside compiled loops: a `while true`, a `for` loop
  and a hot call each ran to a 30 s timeout. They stop only with the JIT off
  for the whole runtime. `lupa` also cannot cap memory on 64-bit LuaJIT
  (`max_memory` fails at construction, and the docstring says so). LuaJIT is
  in no macOS wheel either.
- Verified: Lua 5.4 can be limited. The count hook stops all three
  loops at once; a 64 MB `max_memory` cap raises `LuaMemoryError` for
  `string.rep` and for table growth; unbounded recursion raises "stack
  overflow".
- Verified: a script-created coroutine escapes the count hook on 5.4,
  and a host wrapper closes it. A loop inside a new coroutine ran to the
  timeout with the hook set on the main thread. Cause: the debug library
  keeps the Lua hook function per thread. A new thread inherits the hook's
  count (`debug.gethook` inside it reports 100000) but has no function
  registered, so nothing runs when the hook fires. A host-provided
  `coroutine.create` that calls `debug.sethook(co, hook, "", n)` on each new
  coroutine stops the same loop at once (`co_probe.py`, cases `native`,
  `hookmask`, `wrapped`).
- Verified: bytecode loads on 5.1 and LuaJIT. Their `loadstring` path
  runs a binary chunk; 5.2 to 5.5 refuse one when the host compiles in mode
  `"t"`. `string.dump` stays reachable through the string metatable
  (`("").dump`) and returns bytecode; harmless while no loader is in reach.
- Verified: host objects leak. A Python object passed in is a live
  proxy. An `attribute_filter` that denies `_` names still let a plain
  attribute holding the `os` module through, and `getcwd()` and `system`
  ran. A closure created before the sandbox and passed in returns the full
  `_G`. Tables returned to Python are proxies too, not copies.
- Verified: no result cap. A 200 MB string came back in 0.32 s.
- Verified (PyPI file lists, one bare-container install): wheels cover
  every supported platform. `lupa` 2.8 has wheels for CPython 3.12 to 3.14
  on macOS arm64, Linux x86_64 and aarch64 (manylinux and musllinux) and
  Windows amd64, and an abi3 wheel for macOS x86_64; installing needs no
  compiler.
- Documented: prior art agrees on the hazards. Redis's 2015 `EVAL`
  escape was bytecode through `loadstring`; Luau removes `load`,
  `string.dump` and bytecode loading and keeps globals read-only per script;
  WeakAuras binds custom code to an empty environment with `setfenv` and
  blocks `loadstring`, `getfenv` and `setfenv`. No public source was found
  for Augur.
- Verified (code read): of RDR-240's verbs, only the memory reads exist.
  `memory.get` and `memory.search` map to the MCP functions in
  `src/nexus/mcp/core.py`, and `memory.list` to `db.list_entries`; the MCP
  functions return formatted text, so the host needs structured forms.
  `transcripts.sessions` has no function. `git.diff_names` and
  `git.diff_stat` have near neighbours (`_git_changed_since` in
  `src/nexus/indexer.py`, `diff_stat_block` in
  `src/nexus/commands/command_context.py`) that do not fit. `tables.get`
  needs a name-to-file mapping over `load_packaged_table`.
- Verified: the dispatcher design holds (`design_probe.py`, Lua 5.4).
  A single Python dispatcher taking and returning JSON strings, reached only
  through Lua functions built in a chunk with an empty environment, with an
  `attribute_filter` that refuses every attribute: the granted verb returns
  its JSON, an ungranted verb is refused by name, `("").dump` is nil after
  the string metatable's `__index` is replaced, none of 13 dangerous
  globals is visible, and a binary chunk is refused before compile.

Registry and static-check spikes of 2026-11-03 (`static_check.py`,
`registry_probe.py`, `cstack_probe.py`, `cstack_hook_probe.py`, the corpus in
`corpus/`, and an `out_*.txt` file for each; `lupa` 2.8, Lua 5.4, macOS arm64, CPython from
`uv run --no-project --with lupa==2.8`):

- Verified: a static check can read Lua's own compiled output instead of
  parsing Lua. The checker compiles the source with Lua 5.4's
  `load(src, name, "t", {})` in a throwaway runtime (compiling runs nothing),
  dumps it with `string.dump`, reads the 5.4 binary chunk in Python (about
  100 lines), and applies its rules to every function prototype. A
  calibration step pins the opcode numbering against Lua's own output. Over a
  44-case corpus (6 allowed scripts, 38 that must be refused), every allowed
  script passed and every other one was refused under the rule the corpus
  expects: `load`,
  `loadstring`, `dofile`, `require`, `debug.sethook`, `debug.getupvalue`,
  `string.dump`, `("").dump`, `s:dump()`, `setmetatable` on `string`,
  `getmetatable("")`, `rawset(_ENV, ...)`, `rawget`, `_ENV` as a value,
  indexed, nested or declared local, `_G`, a global write, a global
  `function f()`, overwriting a verb, `coroutine.create`, `coroutine.wrap`,
  `collectgarbage`, an ungranted verb, a write verb, a dynamic verb name, an
  aliased verb namespace, `pairs(string)`, a dynamic library member, an
  unknown library member, an unused grant, an unknown grant, a syntax error,
  a non-ASCII byte, a control byte, and a binary chunk.
- Verified: the checker fails closed where Lua's code generation changes.
  Over 255 constants in one function, Lua stops reading globals with
  `GETTABUP` and loads `_ENV` into a register; the checker then refuses the
  script under the `_ENV` rule (case `inline_many_constants`). A constant key
  in brackets (`string["rep"]`) compiles to the same static field access as
  `string.rep` and passes.
- Verified: a key built at run time on a value the checker cannot type
  passes the static check, by design: `local s, k = "", "du" .. "mp" return
  s[k]` is a dynamic index on a local. The sandbox returns nil for it,
  because the string metatable's `__index` is the reduced `string` table.
  The second layer is needed for this class.
- Verified: native `coroutine.wrap` escapes the count hook even when
  `coroutine.create` is the hook-arming wrapper: `wrap` makes its thread in
  C, so the loop inside ran to the 8 s timeout. A host `wrap` built on the
  wrapped `create` stopped it at the instruction limit.
- Verified: a script can swallow a tripped limit with `pcall`.
  `while true do pcall(function() while true do end end) end` ran to the
  timeout, because each hook error was caught and the loop went on. A hook
  that, once tripped, rearms itself to fire on every instruction stopped it,
  since the next instruction outside the `pcall` raised again.
- Verified: at the C-stack limit the hook stops running. With
  `local function g() while true do pcall(g) end end g()`, the script sits
  at a depth of about 198 nested `pcall` calls (measured: 99,803 of 100,000
  iterations ended in "C stack overflow"), and the loop ran to the timeout
  even with the rearmed hook; an instrumented run that logged every 50th hook
  call logged none in 4 s. The likely cause, inferred and not traced in Lua's
  source: calling the Lua hook function needs a C-stack level the script has
  used up, so the hook call itself fails with "C stack overflow" and the
  script's `pcall` catches it. A host `pcall` that re-raises
  whenever a limit has tripped, and whenever the caught error is a stack
  overflow, stopped it with `limit:stack`; the same guarded `pcall` still
  returned `false, "boom"` for an ordinary error. `lupa` 2.8 has no C-level
  hook (its `LuaRuntime` API lists none), so the Lua hook and this guard are
  the only in-process instruction limit.
- Verified: a rearmed hook also fires on the host's own next instruction
  after the script returns, so a tripped limit reaches Python as a
  `LuaError` from the host wrapper, not as a `false` from its `pcall`. The
  host maps it; nothing escapes.
- Verified: the registry shape works end to end against an in-memory stand-in
  for the engine tables. A registered script is pending and its run is
  refused; an approval signed by a key that is not the enrolled one leaves it
  pending; an approval signed by the enrolled key makes it run; an edit is a
  new sha256 and a new pending version, while the approved version still
  runs; a source swapped under the approved sha256 is refused by the pin;
  grants widened after approval no longer match the signed payload, so the
  version reads as pending; a revocation stops it, and replaying the old
  approval after it does not bring it back; a trial with a verb call that has
  no fixture, and a trial whose output has the wrong shape, are recorded and
  the approval refuses them; a static failure is recorded and the trial is
  not run.
- Verified: an SSHSIG signature made by `ssh-keygen -Y sign` with an
  Ed25519 key, in namespace `nexus-script-approval`, verifies in-process with `cryptography`
  in about 40 lines, and agrees with `ssh-keygen -Y verify`. Not verified: a
  signature from a hardware-held key. A FIDO2 (`sk-`) signature adds flags and
  a counter to the signed data (OpenSSH `PROTOCOL.u2f`, not read for this
  RDR), so the verifier needs that variant written and tested.
- Verified: grants hold at run time with the static check bypassed. In a
  sandbox built for `memory.get` only, `memory.list(...)` fails with "attempt
  to call a nil value (field 'list')" and `git.diff_names(...)` with "attempt
  to index a nil value (global 'git')"; a backend exception reaches the script
  as a string.

What the probes force on any design: PUC Lua 5.4; host-side compile in mode
`"t"` and a refusal of any chunk that starts with byte 27 (ESC); plain
values in and a host-walked JSON conversion out, with depth and size caps;
no host object and no pre-built closure in the environment; `coroutine`
only through host wrappers for both `create` and `wrap`, or not at all in the
first version; a `pcall` that cannot swallow a limit or a stack overflow; a
hook that rearms on every instruction once tripped; a result cap on the host;
and a static check that reads Lua's own bytecode.

### Critical Assumptions

- A1: A `lupa` wheel exists for every platform and CPython version
  conexus supports, so adding it needs no compiler. Verification: the wheel
  list on PyPI and an install on native Windows. Status: the wheel lists hold
  and a Linux aarch64 install ran; the native Windows install is not yet run.
- A2: A script can be held to an instruction or time limit and a memory
  limit that it cannot disable from inside. Verification: escape tests in the
  Test Plan, run against the chosen runtime. Status: holds for Lua 5.4 with
  three host measures the probes forced (coroutines only through wrappers for
  `create` and `wrap`, a hook that rearms once tripped, a guarded `pcall`);
  fails for LuaJIT. Wall time is checked only when the hook runs, so one long
  C call (a `string.rep`, a pattern match) overruns it until it returns; not
  yet measured.
- A3: The read verbs RDR-240 needs can be served from the client without
  new engine routes. Verification: map each verb to an existing client call.
  Status: no engine route is needed for the verbs, but four of the seven need
  new client code (transcripts, both git verbs, tables by name). The registry
  does need engine routes (Technical Design 2).
- A4: The static check reads bytecode whose layout and opcode numbering stay
  fixed within the pinned `lupa` range. Verification: the calibration test in
  the suite fails on any change. Status: holds for `lupa` 2.8; the Lua 5.4
  patch release it bundles is not recorded, because `lupa` exposes only
  `(5, 4)`.
- A5: Sam has, or will get, a hardware-held SSH key that signs only after a
  touch, and its signature verifies in the client and in the engine.
  Verification: enroll it and approve one script on the laptop; one engine
  test per key type. Status: not run; which hardware is Sam's call (Open
  Questions).
- A6: No agent and no MCP principal can produce a valid approval.
  Verification: the credential tests in the Test Plan. Status: argued in
  Technical Design 7, not tested.
- A7: The engine can verify SSHSIG signatures in Java (the JDK has Ed25519
  and ECDSA P-256). Status: assumed; no Java code written.

## Proposed Solution

### Approach

Two layers. The first decides whether a script runs: a registry in the
engine holds every script version, each one checked and trial-run when it is
registered, and only a version Sam has approved with a signature from a key
held in hardware can run. The second bounds what a running script can do:
one Python function runs one Lua 5.4 script in a fresh, empty interpreter
and returns plain JSON, with only the granted verbs in its environment and
every limit enforced by the host from outside the script.

A run never takes source. `run_script(name, sha256, args)` names a
registered version; the runner fetches it, checks that its bytes hash to the
pinned sha256, verifies Sam's signature over that hash and its grants,
checks it again, and runs it. An edit is a new hash and a new pending
version. The first version has no write verb.

The surface is built for one consumer. Version 1 carries exactly the read
verbs RDR-240's first four procedures need (its Technical Design item 4).
Tuple-space verbs, described by the dictionary (RDR-239), arrive with the
consumer that needs them, the aura stage of nexus-mdbds, through the same
grant rule.

### Technical Design

The lifecycle of a script version:

```
register --> evaluate --> pending --(Sam signs)--> approved --(Sam signs)--> revoked
(any principal) (static check   (runs refused)     (runs allowed)           (terminal)
                 + trial run,
                 recorded)
```

1. Script versions. A version is identified by its name and the sha256 of
   its exact source bytes. Names match `[a-z][a-z0-9_]{0,63}`. A version
   carries: the source; its sha256; the declared grants (verb names); the
   fixtures for its trial run; its output shape; optional limits, each lower
   than the defaults; the registrant; the time of registration; the
   evaluation reports; and the checker version that wrote them. Approval and
   revocation are events about a version, never fields anyone can set.
2. Registry. Three engine tables, created by a Liquibase changeset and
   served by `/v1/scripts` routes, each scoped to the tenant:
   - `script_versions`: one row per (tenant, name, sha256), inserted once and
     never updated. Registering the same bytes again is a no-op. The
     registrant columns come from the authenticated principal (tenant,
     session, scope); the `agent` attribution the client sends, as
     `memory_put` does, is stored as informational only.
   - `script_events`: append-only rows (tenant, name, sha256, kind
     `approve` or `revoke`, the signed payload, the signature, the key
     fingerprint, the time). The engine inserts a row only when the signature
     verifies against the tenant's enrolled key and the payload names that
     row's version; it refuses every other insert, whatever the principal's
     scope.
   - `script_approver`: the tenant's enrolled public key (Technical Design 7).

   There is no update or delete route for any of the three. A version's state
   is derived, never stored: revoked if any revoke event exists for it,
   approved if a verified approve event exists and no revoke does, pending
   otherwise. The read routes return the derived state with the approving
   key's fingerprint and the event time, which is what `nx script show`
   prints as approver and approved_at. A tenant holds at most 100 pending
   versions; past that, registration is refused, so an agent in a loop cannot
   flood Sam's queue.

   Why an engine table: T2 memory is written and deleted by every agent
   through `memory_put` and `memory_delete`, so a revocation stored there
   could be removed and the state would be whatever the last writer left. A
   tuple subspace is worse for a record: `tuple_in` consumes a tuple, so any
   principal with tuple access could take a revocation out of the space. A
   table with no update or delete route is the one form where append-only is
   enforced below every client principal, and the NO-SQLITE rule already puts
   new persistent state in PG through the engine.
3. Registration. `nx script register <file> --name N --grants a,b
   --fixtures F.json --shape S.json [--limits L.json]`, or the same fields
   from RDR-240's procedure header. The client computes the sha256, runs the
   evaluation (items 4 and 5), and posts the version with both reports. No MCP
   tool registers in this RDR; whether agents register through RDR-240's front
   doors is RDR-240's decision. Registration is open to any principal of the
   tenant, because a pending version can do nothing.
4. Static check. The checker never parses Lua. It compiles the source with
   Lua 5.4's own `load(src, "=script", "t", {})` in a throwaway runtime,
   dumps the function with `string.dump` (debug information kept), reads the
   binary chunk, and walks every function prototype. It refuses on the first
   violation, naming the rule and the line:

   | Rule | Refuses | Why |
   | --- | --- | --- |
   | `binary`, `bytes`, `size` | a first byte of 27; any byte outside printable ASCII, tab, CR and LF; source over 64 KB | no bytecode; Sam reads exactly what will run, with no homoglyph or control character hiding text |
   | `parse` | source Lua 5.4 does not compile in mode `"t"` | |
   | `global` | a global read outside the allowlist (the base functions and libraries of item 10, and the granted verb namespaces) | `load`, `loadstring`, `dofile`, `require`, `debug`, `getmetatable`, `setmetatable`, `rawset`, `rawget`, `collectgarbage`, `coroutine` in version 1, `_G` |
   | `global-write` | any global assignment, `function f()` included | a script cannot plant or replace a name; it uses locals |
   | `env` | `_ENV` read or written as a value, indexed, or declared as a local | no dynamic global access |
   | `ns-value` | a library or verb namespace used other than as `ns.member` | no alias (`local m = memory`), no iteration (`pairs(string)`), no overwrite (`memory.get = f`) |
   | `ns-dynamic` | `ns[expr]` with a key built at run time | no dynamic verb or library names |
   | `ns-member` | a library member outside its allowlist | |
   | `field-dump` | a constant field or method named `dump`, on anything | `string.dump`, `("").dump`, `s:dump()` |
   | `verb-unknown` | a declared or called verb not in the verb table, which holds no write verb in version 1 | |
   | `verb-ungranted` | a call to a verb outside the declared grants | |
   | `grant-unused` | a declared grant the script never names | the grants describe what the script does |

   Reading bytecode rather than source means the checker and the runtime
   cannot disagree about the grammar: a long bracket or comment that some
   other parser reads differently cannot hide a call, because the checker sees
   what Lua compiled. Where Lua's code generation leaves the forms above (over
   255 constants in one function), the script is refused, not passed (Key
   Discoveries). The check runs at registration, again at approval, and again
   before every run, so a stricter checker refuses an old approval. A
   dynamic index on a value the checker cannot type (`s[k]` on a local
   string) passes; the second layer covers it.
5. Trial run. Registration declares one or more fixtures, each an argument
   object and the canned response for every verb call the script makes on
   it, keyed by verb and arguments. The trial runs the script in the real
   sandbox (items 9 to 15) with a dispatcher that serves only those
   responses, so an unapproved script never reads live data. A verb call
   with no fixture fails the trial, naming the call. The result must match
   the declared output shape, a small closed language: `string`, `number`,
   `boolean`, an array of one shape, or an object with exactly the listed keys
   and a shape for each. The limits apply. Each fixture's outcome is recorded
   on the version.
6. Approval and revocation. `nx script approve <name> <sha256>` fetches the
   version, prints its source (line-numbered), grants, shape, fixtures,
   limits and a diff against the latest approved version of the same name,
   then re-runs the static check and the trial itself; it never trusts the
   stored reports, which any registrant could have written. If both pass, it
   builds the payload, the canonical JSON of the kind, tenant, name, sha256,
   sorted grants, output shape, limits and checker version, and asks
   `ssh-keygen -Y sign -n nexus-script-approval` to sign it with the enrolled
   key, which waits for Sam's touch. It posts the event; the engine verifies
   it and inserts it. `nx script revoke <name> <sha256>` signs a revoke
   payload the same way. A revocation is terminal for that sha256: approving
   it again is refused, so a replayed old approval cannot revive it, and Sam
   re-registers a changed file instead. The verbs refuse to run without a
   terminal (a speed bump for an agent's Bash tool; the touch is the gate).
   `nx script list` and `nx script show` read the registry and need no key.
7. The approval credential. The approver key is an OpenSSH key whose private
   half lives in hardware and signs only after a physical touch: a FIDO2
   security key generated with `ssh-keygen -t ed25519-sk -O verify-required`
   (touch and PIN per signature), or a Secure Enclave key held by an SSH
   agent that asks for Touch ID on every use. The private half never exists
   as a file, an environment variable or a token.
   - Enrollment. `nx script enroll <public-key>` posts the public key with a
     signature over the enrollment payload made by that key (proof of
     possession, so one touch). The engine accepts it only when the tenant
     has no enrolled key and only from the `root` scope. A second enrollment
     is refused, naming the enrolled fingerprint.
   - Rotation. A new key is enrolled by a payload signed by the current key.
   - Loss. A lost key is cleared by an operator statement run on the
     engine's database, documented in the runbook, never by an HTTP route.
   - Verification. The engine verifies every event before inserting it. The
     runner verifies again before every run, against the enrolled key it
     fetches, and does not trust the engine's derived state. For a FIDO2 key
     both verifiers require the user-presence flag, and the user-verified
     flag when the key was enrolled with `verify-required`.

   Why no agent can reach it: there is nothing to read. No MCP tool
   approves, and no module under `src/nexus/mcp/` imports the signer. The
   engine accepts an approval from any principal, the root token's included,
   only when its signature verifies, so holding a token gains nothing. An
   agent with a shell can run `nx script approve` or `ssh-keygen -Y sign`, and
   the key then waits for a touch Sam did not ask for.
8. Running. `nexus.lua_runtime.run_script(name, sha256, args, limits=None)
   -> JSON value` fetches the version and its events, and refuses, naming the
   reason, when: the version is not registered; the source's sha256 is not
   the pinned one; no approve event verifies against the enrolled key for
   exactly this name, sha256, grants, shape and limits; any revoke event
   exists (fail closed, verified or not); or the static check fails today.
   Otherwise it builds the sandbox with only the version's grants and limits
   (lowered further by the caller's `limits`, never raised), runs it, and
   checks the result against the declared shape. `resolve(name) -> sha256`
   returns the most recently approved, unrevoked version of a name, for
   RDR-240's `run_procedure`. Source is never a parameter of any run path.
9. Runtime. `lupa.lua54` (PUC Lua 5.4) through `lupa>=2.8,<3`. LuaJIT is
   never loaded: its count hook skips compiled code and `lupa` cannot cap
   its memory (Key Discoveries). One `LuaRuntime` per run, discarded after
   it, so no state carries from one script to the next. Constructed with
   `register_eval=False`, `register_builtins=False`,
   `unpack_returned_tuples=False`, `max_memory` set, and an
   `attribute_filter` that refuses every attribute access. The static check
   uses a separate runtime that only compiles.
10. Environment. Built by host Lua code that runs before the script and
    is then dropped. The script's environment is a new table holding copies
    of: the safe base functions (`assert`, `error`, `ipairs`, `next`,
    `pairs`, `select`, `tonumber`, `tostring`, `type`, `rawequal`,
    `rawlen`), a guarded `pcall` (item 14), `string` without `dump`, `table`,
    `math`, `utf8`, and the granted verbs. The string metatable's `__index`
    is replaced by the same reduced `string` table, so `("").dump` is nil.
    `coroutine` is absent in version 1. When a consumer needs it, it comes
    only as host wrappers: `create` and `wrap` both arm the hook on each new
    thread, and `resume` is guarded like `pcall` (Key Discoveries). The real
    `_G` is wiped once the environment is built.
11. Loading. The host refuses source whose first byte is 27 (a binary
    chunk), then compiles it with `load(src, name, "t", env)`. The source is
    the registered version's, after the pin and signature checks of item 8.
12. Verbs. One Python dispatcher, `dispatch(verb, args) -> result`, is the
    only Python object that crosses into Lua. A host chunk loaded with an
    environment holding only that dispatcher builds one Lua function per
    granted verb; a verb the version was not granted has no function, so the
    script cannot name it, and the dispatcher also refuses it by name. The
    script cannot read those functions' upvalues, since `debug` is absent.
    Arguments are plain values (string, number, boolean, nil); results are
    plain values or tables the host builds, so no Python object, proxy or
    closure reaches the script. The dispatcher never raises into Lua: it
    catches every exception and returns an error value, and the verb function
    raises a Lua error carrying that message as a string. Otherwise `pcall`
    hands the script the Python exception itself, a host object
    (`design_probe.py`, case `exc_attr`: the refuse-all filter held, and only
    `tostring` worked, but the rule must not rest on the filter alone).
13. Version 1 verbs (read-only). `memory.get(project, title)`,
    `memory.list(project)`, `memory.search(query, project, limit)`,
    `transcripts.sessions(since, until, project, bodies)`,
    `git.diff_names(base)`, `git.diff_stat(base)`, `tables.get(name)`. Each
    returns structured values, not the formatted text the MCP tools print:
    the memory verbs call the T2 store layer (`db.resolve_title`,
    `db.list_entries`, `db.search`); the other four are new client functions
    (Key Discoveries, A3). `transcripts.sessions` never returns message
    bodies unless `bodies` is true. A write verb, when one ships, is a
    separate entry in the verb table, granted only when declared and covered
    by Sam's signature like any other grant.
14. Limits. Defaults, each lowered by the version's declared limits or the
    caller and never raised:
    - instructions: 50 million, counted by a hook every 10,000 instructions;
    - memory: 64 MB (`max_memory`);
    - wall time: 30 s, checked by the same hook against a host clock captured
      in its closure;
    - result: 1 MB of JSON and 32 levels of nesting, checked while the host
      walks the returned value.

    Once a limit trips, the hook rearms itself to fire on every instruction
    and raises on each, and the script's `pcall` is a host wrapper that
    re-raises when a limit has tripped or the caught error is a stack
    overflow. Together they stop the two `pcall` escapes the probes found. A
    run that hits a limit fails with `ScriptLimitError(limit, value)`; the
    host maps a `LuaMemoryError`, and a `LuaError` raised by the rearmed hook
    in the host's own wrapper, to it.
15. Results. The host walks the returned Lua value into JSON types itself
    (nil, boolean, number, string, array-like and string-keyed tables). A
    function, a userdata or a table with mixed keys fails the run with a named
    error. The caller never receives a `lupa` proxy.
16. Public surface. `run_script` and `resolve` (item 8), and the registry
    client behind the `nx script` verbs (`register`, `list`, `show`,
    `approve`, `revoke`, `enroll`). RDR-240's `run_procedure` calls `resolve`
    and `run_script`. Neither the CLI nor MCP runs a script directly in this
    RDR.

### Decision Rationale

A sandbox can only limit what a script reaches. Whether a script should run
is a judgement about its purpose, and Sam's direction is that he makes it.
A registry with a content pin turns that judgement into something the host
can check: the bytes that run are the bytes Sam read, under the grants he
read. A signature from a key held in hardware makes the approval something
no other principal can produce, rather than a state that any principal with
the right token could set. The engine table makes revocation stick, because
nothing can delete it.

The static check reads Lua's own bytecode so that it cannot be fooled by a
grammar difference, and it fails closed where the bytecode changes form. It
catches the escape classes before anyone is asked to approve, which keeps
Sam's review to purpose and grants. The trial run shows that the script does
something with plausible inputs without touching live data.

Lua 5.4 is the only candidate the probes could hold to all four limits from
outside, and `lupa` ships it as a wheel for every platform conexus supports.
Passing only plain values across the boundary removes the three leaks the
probes found (host-object attributes, a pre-built closure, table proxies) by
construction rather than by filtering. Approved-only source and no write verb
make version 1's worst case a read, by a script Sam approved, of something
the caller could already read, so an interpreter bug in Lua is a denial of
service, not a data change.

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
the OS, including during a long C call, which the in-process hook cannot
interrupt.

**Cons:** a process start per run; no `RLIMIT_AS` enforcement on macOS; the
verbs still run in the parent, so every verb call is an IPC round trip.

**Reason for rejection:** not needed while every script is approved and
nothing writes. It is the planned hardening if a write verb is granted, or
if the long-C-call overrun (A2) proves large.

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

### Alternative 5: Arbitrary source at run time, sandbox only

**Description:** the 2026-11-02 draft of this RDR: `run_script(source,
name, args, grants)` takes source from a library file and runs it in the
sandbox, with the caller's grants.

**Pros:** no registry, no approval step, no engine work; a new procedure
runs as soon as its file exists.

**Cons:** the sandbox is the only defence, and the probes found three ways
past its limits that a first design missed (native `coroutine.wrap`, a
`pcall` that swallows the limit, `pcall` recursion to the C-stack limit).
Nothing records who wrote a script or whether anyone read it; the grants are
whatever the caller passes; and a file an agent wrote under a prompt
injection runs the same as one Sam wrote.

**Reason for rejection:** Sam (2026-11-03): no arbitrary Lua. A script runs
only after it is registered, evaluated and approved by Sam.

### Alternative 6: Approval by an agent or by a token

**Description:** let a reviewing agent approve (for example after
`code-review-expert` passes the script), or gate the approve route on a
separate approver token held in config or the keychain.

**Pros:** no hardware; no wait on Sam.

**Cons:** an agent approval is exactly what a prompt injection can produce.
A token is a value; every agent running as Sam's user can read Sam's files
and environment, and an MCP client that holds it can replay it.

**Reason for rejection:** Sam chose approval by Sam only (option (a),
2026-11-03), through a credential no other principal holds. Only a key that
needs a physical act per signature meets that.

### Alternative 7: The registry in T2 memory or a tuple subspace

**Description:** store versions and approvals as T2 memory entries or as
tuples in a `scripts` subspace, with no engine change.

**Pros:** no Liquibase changeset, no new routes.

**Cons:** every agent can delete a memory entry, and `tuple_in` consumes a
tuple, so a revocation could be removed and the version would run again.

**Reason for rejection:** append-only has to be enforced below the client
principals (Technical Design 2).

### Alternative 8: A static check over a Python Lua parser

**Description:** parse the source with a Python Lua parser (an ANTLR grammar
such as `luaparser`) and check the AST.

**Pros:** source-level error messages; no dependency on the bytecode layout.

**Cons:** two grammars, the parser's and Lua's, and any place they differ
(long brackets, comment forms, a 5.4 feature the parser lacks) is a place to
hide a call from the checker; another dependency.

**Reason for rejection:** reading Lua's own bytecode removes the
difference; the cost is a reader pinned to the 5.4 layout, which a
calibration test guards (A4).

## Trade-offs

### Consequences

- A new runtime dependency, `lupa`, with a native extension in every
  install; `cryptography` becomes a direct dependency.
- Engine work: a changeset, three tables, the `/v1/scripts` routes, and
  SSHSIG verification in Java.
- Every new script and every edit waits for Sam. That is the point, and it
  is also a queue; the pending cap bounds it.
- A script with over 255 constants in one function is refused; it splits
  into functions.
- Scripts are slower than LuaJIT would be; RDR-240's A2 (a 30-day transcript
  query under a minute) is measured against Lua 5.4.
- Every new verb is Python code with a test, reviewed like any other API.

### Risks and Mitigations

The threat model names who might try what, and what stops them.

- A prompt-injected agent, with MCP tools and possibly a shell. It can
  register a hostile script, which stays pending. It cannot approve: no MCP
  tool approves, the engine accepts no event without a valid signature, and
  the key needs a touch. With a shell it can start `nx script approve` or
  `ssh-keygen -Y sign`; the key waits for a touch Sam did not ask for. It can
  ask Sam to approve something; Sam reads the source, grants, shape and diff
  that `nx script approve` prints before the touch. Residual: Sam approving a
  script whose purpose he misread.
- A compromised MCP client holding a tenant token. It can call any route its
  token's scope allows, the registry's included: it can register (pending,
  capped at 100) and read. It cannot approve, revoke, enroll a key while one
  is enrolled, or edit or delete a version or an event, because no route
  does those without the enrolled key's signature.
- A malicious registrant. It writes a script that looks harmless and is
  not. The static check refuses every escape class before Sam sees it; the
  approval signs exactly the bytes, grants, shape and limits Sam read; an
  edit is a new pending version; a source or grant swapped in the database
  fails the pin or the signature. Non-ASCII and control bytes are refused,
  so the source prints as it runs. A forged "passed" report changes nothing,
  since approval and every run re-run the checks.
- An approved script that misbehaves at run time, on inputs the trial did not
  cover or through a class the static check cannot see (a dynamic index on a
  string). The sandbox bounds it: only the granted verbs exist, all reads in
  version 1; every limit holds from outside, with the hook rearm and the
  guarded `pcall`; the result is walked and capped; and the output must match
  the declared shape. Its worst case is a read of what the caller could
  already read, or a run that fails on a limit. Data a verb returns may carry
  text that steers the agent reading the result; that is the same exposure
  `memory_get` has today, and this RDR does not change it.
- An agent with a shell running as Sam's OS user, on a local install. It can
  run any code without Lua, and it can edit the local engine's database
  directly, including the enrolled key. The registry does not and cannot stop
  that; what it guarantees is that no script runs through `run_script` unless
  its exact bytes and grants carry a signature from the hardware key, and a
  replaced key shows up as a new fingerprint on every `nx script show`. In
  cloud mode the tables live in the managed engine, out of the laptop's
  reach.
- A Lua interpreter bug escapes the sandbox (Redis shipped one in 2015,
  bytecode through `loadstring`). Mitigation: approved-only source, no write
  verbs, a pinned `lupa` range raised deliberately, and Alternative 2 before
  any write verb is granted.
- A `lupa` release changes defaults or the bytecode layout. Mitigation: the
  escape suite and the checker's calibration test run in the normal PR suite
  and fail on any new global or any opcode change.
- The hook interval lets a costly C call run long (one `string.rep` call or
  one pattern match is one instruction). Mitigation: the memory cap bounds
  the allocation; the wall time check runs at the next hook; the overrun is
  measured in Phase 1 (A2).
- A hardware-key signature fails to verify in one of the two verifiers.
  Mitigation: Phase 2 Step 1 tests each enrolled key type in both before any
  script is approved.
- Native Windows install fails. Mitigation: Phase 1 Step 1 installs it
  on qwent's Windows side before anything else lands.

## Implementation Plan

### Phase 1: The sandbox

#### Step 1: Dependency

Add `lupa>=2.8,<3`; install on macOS arm64, Linux x86_64 (chas) and native
Windows (qwent) and run one script on each.

#### Step 2: Runtime, loading, results and limits

`src/nexus/lua_runtime/` with Technical Design items 9 to 12, 14 and 15
(the guarded `pcall` and the hook rearm included), and the escape and limit
tests, with no verbs yet and a test-only entry point that takes source.
Measure the long-C-call overrun (A2).

### Phase 2: The registry and approval

#### Step 1: Engine tables, routes and verification

The Liquibase changeset for `script_versions`, `script_events` and
`script_approver`; the `/v1/scripts` routes with no update or delete; the
pending cap; SSHSIG verification in Java for `ssh-ed25519`,
`sk-ssh-ed25519` and `ecdsa-sha2-nistp256` keys; enrollment from the `root`
scope only, once, and rotation signed by the current key; the operator reset
documented in the runbook.

#### Step 2: Static check and trial

The bytecode reader with its calibration test, the rules of Technical
Design 4, the fixture dispatcher and the shape check of item 5, ported from
`scripts/spikes/rdr242-lua-sandbox/static_check.py` and `registry_probe.py`.

#### Step 3: CLI and the run path

`nx script register`, `list`, `show`, `approve`, `revoke` and `enroll`;
in-process SSHSIG verification in the client; `run_script(name, sha256,
args)` and `resolve(name)` replacing the test-only entry point.

#### Step 4: Enrollment and the first approval

Sam enrolls his key and approves one fixture script on the laptop; it runs
on macOS, chas and qwent.

### Phase 3: The read verbs

#### Step 1: Memory and tables

The three memory verbs over the T2 store layer, and `tables.get` with its
name-to-file map.

#### Step 2: Git and transcripts

`git.diff_names`, `git.diff_stat` and `transcripts.sessions`.

### Phase 4: Hand-off

RDR-240's runner calls `resolve` and `run_script`; this RDR closes when its
first procedure, registered and approved, runs through it.

## Test Plan

- Static-check corpus (ported from `scripts/spikes/rdr242-lua-sandbox/corpus/`
  and extended as escapes are found): every allowed script passes; every
  escape is refused under its named rule, including the known classes (`load`,
  `loadstring`, `dofile`, `require`, `debug`, `string.dump` by library,
  metatable and method, `setmetatable` and `getmetatable` on shared tables,
  `rawset` on the environment, `_ENV` in every form, global writes,
  `coroutine.create` and `coroutine.wrap`, an ungranted, unknown, dynamic or
  aliased verb, an unused grant, non-ASCII and control bytes, a binary chunk,
  and over 255 constants). The calibration test fails on any change to the
  bytecode layout or opcode numbering.
- Registry and run gate: an unregistered version, a pending version, a
  revoked version, an edited version (new sha256), a source swapped under an
  approved sha256, grants widened after approval, and an approval signed by a
  key other than the enrolled one are each refused by `run_script`, naming the
  reason; an old approval replayed after a revocation does not revive it; the
  engine refuses an unsigned or wrongly signed event, a second enrollment, and
  every update or delete, from a `root` principal too.
- Evaluation: a trial with a verb call that has no fixture, and a trial whose
  output has the wrong shape, are recorded on the version, and
  `nx script approve` refuses them after re-running both itself; a static
  failure is recorded and the trial is not run; a stored report forged as
  "passed" does not change what approval or a run decides.
- Grants at run time: with the static check bypassed through the test-only
  entry point, a call to an ungranted verb fails naming it, a namespace with no
  grant is absent, and the dispatcher refuses an ungranted verb by name.
- The approval credential: no module under `src/nexus/mcp/` imports the
  signer, and no MCP tool's name or description offers approval (a lint);
  `nx script approve` from a subprocess with no terminal, the way an agent's
  Bash tool runs it, refuses at once and posts nothing; a signing attempt with
  the hardware key present but not touched times out and posts nothing (a
  hand test on the laptop, recorded on its bead, since CI has no key).
- Escape suite (ported from `scripts/spikes/rdr242-lua-sandbox/`): every
  global listed in Key Discoveries is nil; `("").dump` is nil and so is a
  run-time-built `dump` key on a string; a binary chunk is refused before
  compile; no attribute of the dispatcher is reachable; a verb error reaches
  the script as a string, never a Python exception object; a script cannot
  reach a verb function's upvalues; a returned function, userdata or mixed
  table fails with a named error.
- Limit suite: `while true`, a long `for` loop and a hot call stop on the
  instruction limit; a loop that swallows the limit with `pcall`, and `pcall`
  recursion to the C-stack limit, both stop; the guarded `pcall` still
  catches an ordinary error; `string.rep` and table growth stop on the memory
  limit; unbounded recursion fails with a stack overflow; a loop that sleeps
  through a verb stops on wall time; a 2 MB result and 40-deep nesting stop
  on the result cap. Each asserts the limit named in the error. When
  `coroutine` is granted, loops inside `create` and `wrap` threads stop, and
  `resume` cannot swallow a limit.
- Isolation: a global set by one run is absent in the next.
- Verbs: each verb against the real T2 store (engine substrate) and a
  real git repository in a temp directory; `transcripts.sessions` against
  fixture transcripts, with and without `bodies`.
- Platforms: the escape, limit and static-check suites run on macOS, Linux
  and native Windows (`scripts/windows_hand_test.py`).
- Each new test is shown to fail with the code it guards removed, and the
  red run is recorded on its bead.

## Open Questions for Sam

- Which hardware holds the approver key: a FIDO2 security key, or the
  laptop's Secure Enclave through an SSH agent that asks for Touch ID? The
  design takes either; the verifiers need the type enrolled.
- Does `nx script revoke` need the key too? The design says yes, so an agent
  cannot revoke Sam's scripts. The cost is that stopping a script needs the
  key at hand.

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
- `scripts/spikes/rdr242-lua-sandbox/` (probes, corpus and outputs)
- OpenSSH `PROTOCOL.sshsig` (the signature format; verified against
  `ssh-keygen` 10.3p1) and `PROTOCOL.u2f` (the FIDO2 variant; not read)

## Revision History

- 2026-11-02: drafted (problem, context, research questions).
- 2026-11-02: research survey folded into Key Discoveries and the
  assumptions' status.
- 2026-11-02: Proposed Solution, Alternatives, Trade-offs, Implementation
  Plan and Test Plan drafted from the findings.
- 2026-11-03: rewritten at Sam's direction around a script registry: runs
  take a registered name and pinned sha256, never source; static checks and
  a trial run before approval; approval and revocation only by Sam, signed by
  a hardware-held key; grants enforced at run time; the sandbox kept as the
  second layer. Added Gap 5, research questions 7 and 8, the static-check and
  registry spikes and their findings (three new escapes and their
  mitigations), A4 to A7, Alternatives 5 to 8, the threat model, Phase 2 and
  its tests, and Open Questions. Retitled.
