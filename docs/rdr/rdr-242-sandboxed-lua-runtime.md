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

Provenance. Stage 3 of the scriptable tuple space (epic nexus-mdbds),
pulled ahead of stage 2 (declarative auras) on 2026-10-31 at Sam's direction,
because procedures move to Lua and RDR-240 needs a runtime to run them in.
Stage 1, the tuple-space dictionary (RDR-239), shipped and closed on
2026-11-03. RDR-240 cannot be accepted before this RDR is (its assumption
A1). Rewritten on 2026-11-03 at Sam's direction (bead nexus-mdbds.1): no
arbitrary Lua at run time; a script runs only after it is registered,
checked, trial-run and approved by Sam. On 2026-11-07 Sam withdrew the
approval design of that rewrite, a hardware SSH key touched at the laptop
(`nexus_rdr/242-research-30`). Approval moves to its own document
(Technical Design 1), and until it exists version 1 runs only scripts
reviewed like source (Technical Design 2).

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
the host names, and runs only scripts that ship in a procedure library and
are reviewed and committed like any source file. Approval of other scripts
by Sam is the subject of a later document (Technical Design 1).

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
wall time or result size for code the host runs on someone else's behalf. A
limit checked from inside the interpreter cannot meet this alone: one pattern
match is one instruction, and the gate round 1 probe ran one for 10.56 s, and
others past 60 s and 20 s, with the hook never firing (A2,
`nexus_rdr/242-research-29`).

#### Gap 4: There is no rule for granting a write

RDR-240's `gate_record` and `research_add` write one T2 record. Read-only
first means the runtime ships with no write verbs, but it must say how a write
is granted later (declared by the script, named per verb, covered by an
approval, refused otherwise) so that the read-only version does not have to
be redesigned when the first write arrives. The approval that covers a write
is the later document's (Technical Design 1); until it exists, no write verb
ships.

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
then run. On 2026-11-07 Sam withdrew the hardware-key approval that direction
had been designed around (`nexus_rdr/242-research-30`). This RDR now closes
Gap 5 for version 1 by origin alone: a run takes a name from a procedure
library, never source (Technical Design 2). Registration and approval, which
close it for scripts outside the libraries, move to their own document.

## Relationship to Prior RDRs

- RDR-239 (closed scope, stage 1): the dictionary is the description of
  the tuple surface this runtime exposes. A tuple verb in the runtime is
  callable only for a subspace whose template the dictionary describes.
- RDR-240 (draft): the first consumer. Its Technical Design item 4 lists
  the host verbs it needs; its A1 depends on this RDR's write-grant rule.
  Its premise stands as written: a run reads a procedure file from one of
  its two library roots (its Technical Design 1). The edit the 2026-11-03
  rewrite asked of RDR-240 (a library file as what a registration reads) is
  withdrawn with the registry (Technical Design 1).
- RDR-110 (abandoned): the semantic tuple space was scrapped for building
  for every consumer at once. This RDR builds for one consumer (RDR-240) and
  adds verbs only when a named consumer needs them.
- RDR-205 (closed): the tuple space and the mailbox, whose read verbs a
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

Sam's direction (2026-11-03, same bead): no raw source at run time.
`run_script` takes a registered name, a pinned version (the content's
sha256) and arguments. Registry entries carry the source, its hash, declared
grants, the registrant and a state (pending, approved, revoked). Evaluation
comes before approval: static checks and a sandboxed trial run on fixture
inputs with an output-shape check. Grants are enforced at run time, and the
sandbox stays as the second layer. Approval is by Sam only, through a CLI
verb whose credential no other principal holds, at least while the runtime
is read-only.

Sam's direction (2026-11-07, `nexus_rdr/242-research-30`), on the
hardware-key approval designed for that: "note I have *no* access to the
laptop touch id. anything that requires these changes are not a good idea at
this point and likely need a *lot* more thought given how I work". And: "wrt
'deliberate yes' there are on all the devices I use, as they are all ios.
however, each is specific to the device and not the shell". He approved the
split that followed ("looks good"): the approval layer leaves this RDR for
its own document (Technical Design 1); the sandbox, the worker process, the
static check, the read-only verbs and the limits stay.

### Technical Environment

- Python 3.12+ client (`nx` CLI and the MCP server), shipped on PyPI as
  `conexus`, installed by `uv` on macOS (arm64), Linux (x86_64, arm64) and
  native Windows.
- Scripts run in a worker process the client starts, one per run; the
  verbs run in the client (Technical Design 5, 6 and 9,
  `nexus_rdr/242-research-29`). This RDR adds no engine table and no engine
  route.
- No Lua dependency today (`lupa` is absent from `pyproject.toml` and
  `uv.lock`, checked 2026-11-02).

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
   swapped source does to a run. Withdrawn from this RDR on 2026-11-07, with
   the registration and approval parts of question 5 (Technical Design 1);
   the findings below that answer it are kept as a record for the approval
   document.

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
  not run. This finding and the next serve the approval layer withdrawn on
  2026-11-07 (Technical Design 1); they are kept as a record, and nothing in
  this RDR builds on them.
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

Worker probes of 2026-11-07 (`nexus_rdr/242-research-32`; `lupa` 2.8,
CPython 3.13.5, macOS arm64; Linux and Windows not probed):

- Verified: on macOS, `resource.setrlimit(RLIMIT_AS, ...)` raises
  "ValueError current limit exceeds maximum limit", so a worker that sets it
  unguarded fails to start. `RLIMIT_CPU` holds: a child that set it to
  (2, 3) and spun ended by SIGXCPU after 2.02 s.
- Verified: a child started as `sys.executable -I` with an empty
  environment, `cwd` set and `close_fds=True` imported `lupa.lua54`, built a
  `LuaRuntime` with a 64 MB `max_memory` and evaluated `1+1`, in 20 to 29 ms
  over three runs.

What the probes force on any design: PUC Lua 5.4; host-side compile in mode
`"t"` and a refusal of any chunk that starts with byte 27 (ESC); plain
values in and a host-walked JSON conversion out, with depth and size caps;
no host object and no pre-built closure in the environment; `coroutine`
only through host wrappers for both `create` and `wrap`, or not at all in the
first version; a `pcall` that cannot swallow a limit or a stack overflow; a
hook that rearms on every instruction once tripped; a result cap on the host;
a static check that reads Lua's own bytecode; and a kill timer outside the
interpreter's process, because the hook cannot interrupt one pattern match
(A2, `nexus_rdr/242-research-29`).

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
  fails for LuaJIT. Wall time does not hold in-process. Measured in gate
  round 1 (`nexus_rdr/242-research-29`; `lupa` 2.8, Lua 5.4, count hook every
  10,000 instructions, 64 MB `max_memory`):
  `("a"):rep(200):find(("a*"):rep(4) .. "b")` ran 10.56 s in one instruction;
  the same subject with 6 repetitions did not return in 60 s;
  `("a"):rep(30):find(("a?"):rep(30) .. ("a"):rep(30) .. "c")` did not
  return in 20 s. The hook never fired. A pattern match allocates nothing, so
  `max_memory` cannot stop it. The overrun is unbounded, so every run
  runs in a worker process whose kill timer enforces wall time
  (Technical Design 6 and 11, Alternative 2). Status for wall time: the kill
  timer is not built; the worker-process tests in the Test Plan verify it.
- A3: The read verbs RDR-240 needs can be served from the client without
  new engine routes. Verification: map each verb to an existing client call.
  Status: no engine route is needed for the verbs, but four of the seven need
  new client code (transcripts, both git verbs, tables by name).
- A4: The static check reads bytecode whose layout and opcode numbering stay
  fixed within the pinned `lupa` range. Verification: the calibration test in
  the suite fails on any change. Status: holds for `lupa` 2.8; the Lua 5.4
  patch release it bundles is not recorded, because `lupa` exposes only
  `(5, 4)`.
- A5, A6 and A7 no longer apply (2026-11-07, `nexus_rdr/242-research-30`).
  They were: A5, Sam has a hardware-held SSH key that signs only after a
  touch; A6, no agent and no MCP principal can produce a valid approval; A7,
  the engine can verify SSHSIG signatures in Java. Each one is a premise of
  the hardware-key approval layer, and that layer is withdrawn: this RDR
  builds no approval credential, no SSHSIG verifier and no approve route, so
  nothing in it rests on them. Sam has no access to the laptop's Touch ID,
  which also refutes the premise of A5. The approval document states its own
  assumptions (Technical Design 1).

## Proposed Solution

### Approach

Two layers. The first decides whether a script runs. In version 1 that is
decided by origin: a run names a procedure in one of RDR-240's two library
roots, whose files are reviewed and committed like any source file, and the
static check passes it before every run. Approval by Sam, for scripts from
anywhere else, is replaced by a later document (Technical Design 1). The
second layer bounds what a running script can do: one Python function runs
one Lua 5.4 script in a fresh, empty interpreter inside a worker process with
OS memory and CPU limits and a kill timer, and returns plain JSON, with only
the granted verbs in its environment and every limit enforced by the host
from outside the script. Each verb call is a round trip to the parent
(`nexus_rdr/242-research-29`).

A run never takes source. `run_script(name, args)` names a procedure; the
runner reads its file from a library root, checks it, and runs it. The first
version has no write verb.

The surface is built for one consumer. Version 1 carries exactly the read
verbs RDR-240's first four procedures need (its Technical Design item 4).
Tuple-space verbs, described by the dictionary (RDR-239), arrive with the
consumer that needs them, the aura stage of nexus-mdbds, through the same
grant rule.

### Technical Design

1. Approval. Replaced: approval by a device-held passkey
   (WebAuthn on Sam's iOS devices, Face ID), to be designed in its own
   document; nothing is built for approval until then. The direction, and no
   more: a pending approval shows on a page Sam opens on the device; the
   passkey signs a challenge built from the script's exact bytes and grants,
   and the engine checks it against the enrolled passkey; no shell, agent or
   MCP principal holds the key. Withdrawn from this RDR with the hardware-key
   layer on 2026-11-07 (`nexus_rdr/242-research-30`): the script registry,
   its three engine tables and `/v1/scripts` routes, registration, the trial
   run on fixtures, the `nx script` verbs (`register`, `list`, `show`,
   `approve`, `revoke`, `enroll`), the SSHSIG approval credential (a
   hardware-held key, a touch, enrollment, rotation, the operator reset),
   SSHSIG verification in Java and in the client, and `resolve(name) ->
   sha256`. The approval document decides which of them it keeps.
2. What a run needs in version 1. With no approval layer, a run needs three
   things. The script is a file in one of RDR-240's two library roots,
   `src/nexus/procedures/` (ships in the package) or `.nexus/procedures/` in
   a repo (RDR-240 Technical Design 1), and those files are "written by hand,
   reviewed and committed like any source file" (RDR-240, its RDR-190 row;
   `nexus_rdr/242-research-31`). The caller names it, and no run path takes
   source: "The runtime takes no source from a tool argument", as this RDR's
   2026-11-02 draft said. And the static check of item 3 passes it, in the
   worker, before it runs. Its grants are the host verbs its header names
   (RDR-240 Technical Design 1). No registered, unapproved script can run,
   because there is no registry. Residual: an agent that can write to
   `.nexus/procedures/` in a checkout can run a file nobody has reviewed yet.
   Version 1 bounds that only by the sandbox and the read-only verbs, whose
   worst case is a read the caller could already make. Approval (item 1) is
   what closes it, and a write verb waits for it.
3. Static check. The checker never parses Lua. It compiles the source with
   Lua 5.4's own `load(src, "=script", "t", {})` in a throwaway `LuaRuntime`
   inside the run's worker process (item 6), before the script's own runtime
   is built, so no Lua parser runs in the client's process. It dumps the
   function with `string.dump` (debug information kept), reads the binary
   chunk, and walks every function prototype. It refuses on the first
   violation, naming the rule and the line:

   | Rule | Refuses | Why |
   | --- | --- | --- |
   | `binary`, `bytes`, `size` | a first byte of 27; any byte outside printable ASCII, tab, CR and LF; source over 64 KB | no bytecode; a reviewer reads exactly what will run, with no homoglyph or control character hiding text |
   | `parse` | source Lua 5.4 does not compile in mode `"t"` | |
   | `global` | a global read outside the allowlist (the base functions and libraries of item 7, and the granted verb namespaces) | `load`, `loadstring`, `dofile`, `require`, `debug`, `getmetatable`, `setmetatable`, `rawset`, `rawget`, `collectgarbage`, `coroutine` in version 1, `_G` |
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
   Discoveries). The check runs before every run, so a stricter checker
   refuses a script an older one passed. A dynamic index on a value the
   checker cannot type (`s[k]` on a local string) passes; the second layer
   covers it.
4. Running. `nexus.lua_runtime.run_script(name, args, limits=None) -> JSON
   value` finds the name in the two library roots, refusing a repo name that
   shadows a shipped one (RDR-240 Technical Design 1), and reads the file. It
   refuses, naming the reason, when the name is in neither root or the static
   check fails. Otherwise it builds the sandbox with only the header's grants
   and the limits of item 11 (lowered by the caller's `limits`, never
   raised), and runs it in a worker process (item 6). Source is never a
   parameter of any run path.
5. Runtime. `lupa.lua54` (PUC Lua 5.4) through `lupa>=2.8,<3`. LuaJIT is
   never loaded: its count hook skips compiled code and `lupa` cannot cap
   its memory (Key Discoveries). Every run runs in its own worker process
   (item 6). The worker holds one script `LuaRuntime`, discarded with the
   process, so no state carries from one script to the next. Constructed with
   `register_eval=False`, `register_builtins=False`,
   `unpack_returned_tuples=False`, `max_memory` set, and an
   `attribute_filter` that refuses every attribute access. The in-process
   hook and `max_memory` are the inner layer; the worker's OS limits and the
   parent's kill timer are the outer one. The worker is needed because one
   pattern match is one instruction and ran unbounded in-process
   (`nexus_rdr/242-research-29`).
6. Worker process. Every run gets its own worker, a child the client starts
   and ends with the run. The worker contains crashes and runaway time, not
   an escape: it runs as the client's OS user, so native code that escapes
   Lua has that user's reach, less what the start below removes. The
   contract:
   - Start. `subprocess.Popen([sys.executable, "-I", "-m",
     "nexus.lua_runtime.worker"])`, never `fork` or `multiprocessing`
     (inferred, not read: a fork of the threaded MCP server copies its
     state and its held locks). `-I` ignores `PYTHON*` variables and the
     user site. The environment is empty, with only `SYSTEMROOT` added on
     native Windows (`nexus_rdr/242-research-32` for macOS, `-34` for
     Windows), so no tenant token or key reaches the worker. `close_fds=True`;
     stdin and stdout are the pipe, and stderr goes to a pipe of which the
     parent keeps the first 64 KB for the crash error. The working directory
     is the platform temp directory (`tempfile.gettempdir()`), never the
     caller's checkout. The worker sets its own OS limits first, then reads
     nothing and runs nothing until the parent's first frame, which the
     parent sends after any limit it sets from outside (the Windows job).
   - Frames. Each message is a length-prefixed JSON frame. The worker sends
     at most 64 KB per frame (a verb name and plain arguments); the parent
     sends at most 64 MB (a verb result; the same bound as `max_memory`,
     since a larger value cannot be held in Lua). A frame over its cap, a
     frame that is not valid JSON of the expected form, or a 10,001st verb
     call in one run ends the run with `ScriptWorkerError`. The parent checks
     every verb call before it runs it: the verb is granted, and each
     argument has the type the verb table gives it, and the count.
   - OS limits. CPU time: the wall-time limit plus 5 s (soft) and plus 10 s
     (hard), as `RLIMIT_CPU` on Linux and macOS and a per-process time limit
     of the job on Windows. Memory: 1 GiB, as `RLIMIT_AS` on Linux and a
     per-process memory limit of the job on Windows, above the 64 MB
     `max_memory` so that Python, `lupa` and one 64 MB frame fit. On macOS
     `setrlimit(RLIMIT_AS)` raises `ValueError` (`nexus_rdr/242-research-32`),
     so the worker sets it in a `try`, and `max_memory` is the only memory
     bound there. Any other failure to set a limit ends the run with
     `ScriptWorkerError` before the source is read.
   - Wall time. The parent's kill timer starts when the parent starts the
     worker, so worker start counts. The timer runs in its own thread,
     independent of the parent's run loop, so it fires while the parent is
     inside a verb. Verb time counts toward wall time: each verb call gets
     the remaining budget as its timeout (the HTTP timeout of a T2 read, the
     `timeout` of a git subprocess). When the timer kills the worker during
     a verb, the parent abandons the verb and discards its result; version
     1's verbs only read, so nothing is left half done.
   - Parent death. On Linux and macOS a worker that is running Lua is ended
     by its CPU limit, and a worker waiting on the pipe reads end of file and
     exits, so nothing more is needed (`nexus_rdr/242-research-32`: SIGXCPU
     on macOS). On native Windows the parent puts the worker in a Job Object
     with kill-on-close and holds the job's only handle, so the OS ends the
     worker when the parent dies (inferred, not read;
     `nexus_rdr/242-research-34`).
   - Concurrency. One client process (the `nx` CLI, or one MCP server) runs
     at most 4 workers at once. A fifth `run_script` fails at once with
     `ScriptBusyError` and does not wait. The memory a client process can
     commit to workers is then at most four times the memory limit.
   - Ends. The parent maps how the worker ended to one error:

     | Worker end | How the parent knows | Error |
     | --- | --- | --- |
     | the script returned | a result frame, then exit 0 | none |
     | an inner limit (instructions, `max_memory`, stack, result) | a limit frame naming it, then exit 0 | `ScriptLimitError(limit, value)` |
     | the kill timer | the timer thread recorded that it killed the worker | `ScriptLimitError("wall_time", limit)` |
     | the OS CPU limit | SIGXCPU on Linux and macOS, with the timer not fired; on Windows the job reports its time limit (inferred, not read) | `ScriptLimitError("cpu_time", limit)` |
     | the OS memory limit | the worker caught `MemoryError` and exited with code 3 | `ScriptLimitError("memory", limit)` |
     | anything else: another signal (SIGSEGV, SIGABRT, SIGKILL), another exit code, end of file with no result frame, a bad frame | none of the above | `ScriptWorkerError`, carrying the exit status or signal and the first 64 KB of stderr |
7. Environment. Built by host Lua code that runs before the script and
   is then dropped. The script's environment is a new table holding copies
   of: the safe base functions (`assert`, `error`, `ipairs`, `next`,
   `pairs`, `select`, `tonumber`, `tostring`, `type`, `rawequal`,
   `rawlen`), a guarded `pcall` (item 11), `string` without `dump`, `table`,
   `math`, `utf8`, and the granted verbs. The string metatable's `__index`
   is replaced by the same reduced `string` table, so `("").dump` is nil.
   `coroutine` is absent in version 1. When a consumer needs it, it comes
   only as host wrappers: `create` and `wrap` both arm the hook on each new
   thread, and `resume` is guarded like `pcall` (Key Discoveries). The real
   `_G` is wiped once the environment is built.
8. Loading. The host refuses source whose first byte is 27 (a binary
   chunk), then compiles it with `load(src, name, "t", env)`. The source is
   the library file item 4 read, after the static check of item 3.
9. Verbs. One Python dispatcher, `dispatch(verb, args) -> result`, is the
   only Python object that crosses into Lua. A host chunk loaded with an
   environment holding only that dispatcher builds one Lua function per
   granted verb; a verb the script was not granted has no function, so the
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
   The dispatcher lives in the worker, and the verbs run in the parent, so
   each verb call is a round trip to the parent: the worker sends the verb
   name and its arguments as JSON over a pipe, and the parent runs the verb
   and sends back JSON (`nexus_rdr/242-research-29`). The parent refuses an
   ungranted verb by name as well, and checks its arguments (item 6).
10. Version 1 verbs (read-only). `memory.get(project, title)`,
    `memory.list(project)`, `memory.search(query, project, limit)`,
    `transcripts.sessions(since, until, project, bodies)`,
    `git.diff_names(base)`, `git.diff_stat(base)`, `tables.get(name)`. Each
    returns structured values, not the formatted text the MCP tools print:
    the memory verbs call the T2 store layer (`db.resolve_title`,
    `db.list_entries`, `db.search`); the other four are new client functions
    (Key Discoveries, A3). `transcripts.sessions` never returns message
    bodies unless `bodies` is true. A write verb, when one ships, is a
    separate entry in the verb table, granted only when declared and covered
    by an approval (item 1); none ships before that document.
11. Limits. Defaults, each lowered by the caller and never raised:
    - instructions: 50 million, counted by a hook every 10,000 instructions;
    - memory: 64 MB (`max_memory`), with an OS memory limit on the worker
      where the OS enforces one (item 6);
    - CPU time: an OS limit on the worker, the wall-time limit plus 5 s
      (item 6);
    - wall time: 60 s, enforced by the parent's kill timer (item 6), which
      ends the worker at the limit even inside one C call the hook cannot
      interrupt, such as a pattern match (`nexus_rdr/242-research-29`). The
      hook also checks a host clock captured in its closure, as an inner
      layer; whichever of the two ends the run, the error names `wall_time`.
      The default is 60 s, not 30 s, because RDR-240's A2 holds its 30-day
      transcript query to "under a minute" (`nexus_rdr/242-research-31`),
      and 30 s would kill a run that meets it. A runaway then holds a worker
      for 60 s, and the cap of item 6 bounds how many do;
    - result: 1 MB of JSON and 32 levels of nesting, checked while the host
      walks the returned value.

    Once a limit trips, the hook rearms itself to fire on every instruction
    and raises on each, and the script's `pcall` is a host wrapper that
    re-raises when a limit has tripped or the caught error is a stack
    overflow. Together they stop the two `pcall` escapes the probes found. A
    run that hits a limit fails with `ScriptLimitError(limit, value)`; the
    host maps a `LuaMemoryError`, and a `LuaError` raised by the rearmed hook
    in the host's own wrapper, to it. The parent maps every other way a
    worker ends by the table of item 6, so a pathological pattern fails with
    an error and never hangs the MCP server or the CLI (Gap 3).
12. Results. The host walks the returned Lua value into JSON types itself
    (nil, boolean, number, string, array-like and string-keyed tables). A
    function, a userdata or a table with mixed keys fails the run with a named
    error. The walk runs in the worker, which sends the JSON to the parent
    over the pipe. The caller never receives a `lupa` proxy.
13. Public surface. `run_script` (item 4). RDR-240's `run_procedure` calls
    it. Neither the CLI nor MCP runs a script directly in this RDR, and the
    `nx script` verbs are withdrawn with the registry (item 1).

### Decision Rationale

A sandbox can only limit what a script reaches. Whether a script should run
is a judgement about its purpose, and Sam's direction is that he makes it.
The hardware-key approval designed for that does not fit how Sam works
(`nexus_rdr/242-research-30`), so this RDR builds no approval and leaves it
to its own document (Technical Design 1). Until then the judgement is the
one every source file already gets: a script runs only from a procedure
library, reviewed and committed like code (Technical Design 2).

The static check reads Lua's own bytecode so that it cannot be fooled by a
grammar difference, and it fails closed where the bytecode changes form. It
catches the escape classes before a script runs, which keeps a reviewer's
reading to purpose and grants.

Lua 5.4 is the only candidate the probes could hold to the instruction,
memory and result limits from outside the script, and `lupa` ships it as a
wheel for every platform conexus supports. No in-process measure holds wall
time: one pattern match is one instruction, and it ran 10.56 s, and past
60 s and 20 s in two other cases, with the hook never firing and nothing for
`max_memory` to stop (`nexus_rdr/242-research-29`). So every run runs in a
worker process with OS memory and CPU limits and a kill timer, as Sam chose
on 2026-11-07; the in-process hook stays as the inner layer and the error
that names the limit.
Passing only plain values across the boundary removes the three leaks the
probes found (host-object attributes, a pre-built closure, table proxies) by
construction rather than by filtering. Library-only source and no write verb
make version 1's worst case a read of something the caller could already
read. An interpreter bug that crashes or hangs is a denial of service, and
the worker contains it. An escape into native code is not contained: it runs
as the client's OS user with the worker's scrubbed start (Technical Design
6), and only reviewed source and the read-only surface stand against it.

## Alternatives Considered

### Alternative 1: LuaJIT

**Description:** the faster runtime `lupa` also ships.

**Pros:** much faster loops.

**Cons:** the count hook does not fire in compiled code, so a loop cannot be
stopped unless the JIT is off for the whole runtime; `lupa` cannot cap its
memory on 64-bit; absent from every macOS wheel.

**Reason for rejection:** two of the limits fail (instructions and memory),
and the platform the project runs on daily has no LuaJIT wheel.

### Alternative 2: A worker process per script (chosen)

**Description:** run each script in a child process with OS limits
(`RLIMIT_AS`, `RLIMIT_CPU`, a kill on timeout), with the in-process hooks
kept inside it.

**Pros:** contains crashes and runaway time, not an escape: a worker
that crashes or hangs ends alone, and the OS enforces its limits during a
long C call, which the in-process hook cannot interrupt. Native code that
escapes Lua still runs as the client's OS user (Technical Design 6).

**Cons:** a process start per run; on macOS `setrlimit(RLIMIT_AS)` raises
`ValueError`, so `max_memory` is the only memory bound there
(`nexus_rdr/242-research-32`); the verbs still run in the parent, so every
verb call is an IPC round trip. Phase 1 Step 2 measures both costs against a
pass line.

**Why chosen:** the in-process option alone cannot enforce wall time. The
gate round 1 probe (`nexus_rdr/242-research-29`; `lupa` 2.8, Lua 5.4, count
hook every 10,000 instructions, 64 MB `max_memory`) ran one pattern match
for 10.56 s in one instruction, and two others did not return in 60 s and
20 s. The hook never fired, and a pattern match allocates nothing, so the
memory cap cannot stop it. The draft deferred this alternative until the
overrun proved large; it is unbounded. Sam chose it on 2026-11-07 (ask
`rdr242-c1-runtime`, "Worker process (Recommended)"): every script, the
trial included, runs in a worker process with OS memory and CPU limits and a
kill timer, and each verb call becomes a round trip to the parent. The
trial left this RDR with the approval layer (Technical Design 1).

### Alternative 3: Starlark, WebAssembly or embedded JavaScript

**Description:** other embeddable languages with sandboxing stories.

**Pros:** Starlark has no unbounded loops by design; WebAssembly has strong
isolation.

**Cons:** Sam chose Lua for the WeakAuras and Augur model; each needs a
native dependency with less wheel coverage than `lupa`.

**Reason for rejection:** the direction is Lua, and Lua 5.4 in a worker
process meets the limits (Alternative 2).

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
only after it is registered, evaluated and approved by Sam. Version 1
(Technical Design 2) still reads a library file, but it takes a name and
never source, takes the grants from the file's header and not from the
caller, and runs the static check first. Registration and approval of
scripts from elsewhere wait for the approval document (Technical Design 1).

### Alternative 6: Approval by an agent or by a token

**Description:** let a reviewing agent approve (for example after
`code-review-expert` passes the script), or gate the approve route on a
separate approver token held in config or the keychain.

**Pros:** no hardware; no wait on Sam.

**Cons:** an agent approval is exactly what a prompt injection can produce.
A token is a value; every agent running as Sam's user can read Sam's files
and environment, and an MCP client that holds it can replay it.

**Reason for rejection:** Sam chose approval by Sam only (option (a),
2026-11-03), through a credential no other principal holds. The
hardware SSH key designed to meet that is withdrawn; a passkey on Sam's own
device is the direction the approval document takes (Technical Design 1).

### Alternative 7: The registry in T2 memory or a tuple subspace

**Description:** store versions and approvals as T2 memory entries or as
tuples in a `scripts` subspace, with no engine change.

**Pros:** no Liquibase changeset, no new routes.

**Cons:** every agent can delete a memory entry, and `tuple_in` consumes a
tuple, so a revocation could be removed and the version would run again.

**Reason for rejection:** append-only has to be enforced below the client
principals. The registry, and this rejection with it, moves to the approval
document (Technical Design 1).

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
  install.
- No engine work: no table, no route, no engine release.
- Every new script and every edit is a source change, reviewed and committed
  like code. Until the approval document lands, nothing else adds a script.
- A script with over 255 constants in one function is refused; it splits
  into functions.
- Scripts are slower than LuaJIT would be; RDR-240's A2 (a 30-day transcript
  query under a minute) is measured against Lua 5.4 in the worker process,
  and the 60 s wall-time default is set to meet it (Technical Design 11).
- Every run starts a worker process, and every verb call is a round trip to
  the parent (Alternative 2). Phase 1 Step 2 measures both against a pass
  line.
- Every new verb is Python code with a test, reviewed like any other API.

### Risks and Mitigations

The threat model names who might try what, and what stops them.

- A prompt-injected agent, with MCP tools and possibly a shell. Through
  MCP it can only name a procedure that is already in a library and pass it
  arguments; no run path takes source. With write access to a checkout it
  can add a file under `.nexus/procedures/` and run it before anyone reviews
  it (Technical Design 2). Version 1 bounds that by the sandbox and the
  read-only verbs; the approval document is what closes it. Residual: a
  reviewer merging a script whose purpose they misread.
- A compromised MCP client holding a tenant token. It gains nothing new
  here: `run_script` runs only library files, with read verbs that reach
  what the token can already read.
- A malicious author. It writes a script that looks harmless and is not.
  The static check refuses every escape class before the script runs, and
  the file goes through the same review as any source change. Non-ASCII and
  control bytes are refused, so the source a reviewer reads is the source
  that runs.
- A reviewed script that misbehaves at run time, on inputs its tests did not
  cover or through a class the static check cannot see (a dynamic index on a
  string). The sandbox bounds it: only the granted verbs exist, all reads in
  version 1; every limit holds from outside, with the hook rearm, the
  guarded `pcall` and the worker's kill timer; and the result is walked and
  capped. Its worst case is a read of what the caller could already read,
  or a run that fails on a limit. Data a verb returns may carry text that
  steers the agent reading the result; that is the same exposure
  `memory_get` has today, and this RDR does not change it.
- An agent with a shell running as Sam's OS user, on a local install. It can
  run any code without Lua. The runtime gives it nothing it lacks, and does
  not try to stop it.
- A Lua interpreter bug escapes the sandbox (Redis shipped one in 2015,
  bytecode through `loadstring`). Mitigation: library-only source reviewed
  like code, no write verbs, and a pinned `lupa` range raised deliberately.
  The worker process contains a crash or a hang, not an escape: native code
  that escapes runs as the client's OS user, with the worker's empty
  environment and closed descriptors (Technical Design 6).
- A `lupa` release changes defaults or the bytecode layout. Mitigation: the
  escape suite and the checker's calibration test run in the normal PR suite
  and fail on any new global or any opcode change.
- The hook interval lets a costly C call run long (one `string.rep` call or
  one pattern match is one instruction). Measured in gate round 1
  (`nexus_rdr/242-research-29`): one pattern match ran 10.56 s, and two
  others did not return in 60 s and 20 s; the hook never fired, and a
  pattern match allocates nothing, so the memory cap cannot stop it.
  Mitigation: the parent's kill timer ends the worker at the wall-time limit
  and the run fails with `ScriptLimitError` naming it; the OS CPU limit
  backs the timer (Technical Design 6 and 11).
- Native Windows install fails. Mitigation: Phase 1 Step 1 installs it
  on qwent's Windows side before anything else lands.

## Implementation Plan

### Phase 1: The sandbox

#### Step 1: Dependency

Add `lupa>=2.8,<3`; install on macOS arm64, Linux x86_64 (chas) and native
Windows (qwent), and on each run a one-line script through a bare child
process (`python -I -c` that imports `lupa.lua54` and evaluates one
expression). The worker is built in Step 2.

#### Step 2: The worker process

`src/nexus/lua_runtime/` with the worker of Technical Design 6: its start
(interpreter, empty environment, closed descriptors, working directory, the
first-frame wait), the frame caps and the call count, the parent's argument
check, the OS CPU and memory limits on each platform (the macOS `RLIMIT_AS`
call in a `try`), the kill-timer thread and the per-verb remaining budget,
the parent-death bound per platform, the cap of 4 workers, and the table of
worker ends, each with its test. No Lua script runs yet beyond a one-line
fixture.

The same step measures the worker's cost on each of the three platforms:
the median of 20 worker starts (from the spawn to the worker's first ready
frame, `lupa` imported), and the round trip of a 1 MB and a 50 MB verb
result (the parent encodes it, the worker decodes it into a Lua table). The
pass line: a start of at most 500 ms; a 1 MB round trip of at most 100 ms;
a 50 MB round trip of at most 5 s. The step also records whether the 50 MB
table fits under the 64 MB `max_memory`, because RDR-240's 30-day
transcript query depends on that answer. A missed line comes back to this
RDR before Step 3 starts.

#### Step 3: Runtime, loading, results and limits

Technical Design items 5, 7, 8, 9, 11 and 12 (the guarded `pcall` and the
hook rearm included), inside the Step 2 worker, with the escape and limit
tests, no verbs yet, and a test-only entry point that takes source. The
pathological patterns of A2 (`nexus_rdr/242-research-29`) are tests here,
not a measurement left open.

#### Step 4: Static check

The bytecode reader with its calibration test and the rules of Technical
Design 3, ported from `scripts/spikes/rdr242-lua-sandbox/static_check.py`,
running in the worker before the script's runtime is built.

### Phase 2: Approval (replaced)

Replaced (Technical Design 1). Nothing is built for approval in this RDR.

### Phase 3: The read verbs and the run path

#### Step 1: Memory and tables

The three memory verbs over the T2 store layer, and `tables.get` with its
name-to-file map.

#### Step 2: Git and transcripts

`git.diff_names`, `git.diff_stat` and `transcripts.sessions`.

#### Step 3: The run path

`run_script(name, args)` over the two library roots (Technical Design 2
and 4), replacing the test-only entry point.

### Phase 4: Hand-off

#### Step 1: RDR-240's runner on the run path

RDR-240's runner calls `run_script`; this RDR closes when its first
procedure, a reviewed file in a library root, runs through it.

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
  bytecode layout or opcode numbering. The check runs in the worker, never in
  the calling process.
- Run path: a name in neither library root, a repo name that shadows a
  shipped one, and a file that fails the static check are each refused by
  `run_script`, naming the reason; no public run function takes source.
- Grants at run time: with the static check bypassed through the test-only
  entry point, a call to an ungranted verb fails naming it, a namespace with no
  grant is absent, and the dispatcher refuses an ungranted verb by name.
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
  limit; unbounded recursion fails with a stack overflow; a verb call that
  outlasts the remaining budget stops the run on wall time, the verb gets the
  remaining budget as its timeout, and the parent discards its result; a 2 MB
  result and 40-deep nesting stop on the result cap. Each asserts the limit
  named in the error. When `coroutine` is granted, loops inside `create` and
  `wrap` threads stop, and `resume` cannot swallow a limit.
- Worker process: each pathological pattern of A2
  (`nexus_rdr/242-research-29`), `("a"):rep(200):find(("a*"):rep(6) .. "b")`
  and `("a"):rep(30):find(("a?"):rep(30) .. ("a"):rep(30) .. "c")`, is
  killed at the wall-time limit and fails with an error naming it, and the
  calling process is still serving afterwards; a verb call reaches the parent
  as a round trip and its result returns as JSON; an ungranted verb, and a
  verb argument of the wrong type, sent over the pipe are refused by the
  parent by name. The worker's environment is empty (on Windows it holds
  only `SYSTEMROOT`), it holds no descriptor but its three pipes, and its
  working directory is the temp directory. A frame over each cap, a
  malformed frame and a 10,001st verb call each end the run with
  `ScriptWorkerError`. Each row of the worker-end table of Technical Design 6
  yields its error: a timer kill, SIGXCPU, a `MemoryError` exit, a SIGSEGV
  the test sends, and end of file with no result. On macOS the `RLIMIT_AS`
  `ValueError` is caught and the run proceeds under `max_memory`. A worker
  whose parent is killed ends within its CPU limit (Linux, macOS) or at once
  (the Windows job). A fifth concurrent run fails with `ScriptBusyError`.
- Isolation: a global set by one run is absent in the next.
- Verbs: each verb against the real T2 store (engine substrate) and a
  real git repository in a temp directory; `transcripts.sessions` against
  fixture transcripts, with and without `bodies`.
- Platforms: the escape, limit, worker-process and static-check suites run
  on macOS, Linux and native Windows (`scripts/windows_hand_test.py`).
- Each new test is shown to fail with the code it guards removed, and the
  red run is recorded on its bead.

## Open Questions for Sam

- The approval design: approval by a device-held passkey (WebAuthn on
  Sam's iOS devices, Face ID), in its own document (Technical Design 1). Until
  it exists, version 1 runs only reviewed library files and ships no write
  verb.

## Finalization Gate

### Contradiction Check

No contradictions found between the research findings, the design and the
plan. Each constraint in "What the probes force on any design" has a design
item: PUC Lua 5.4 only (Technical Design 5); compile in mode `"t"` and refuse
byte 27 (items 3 and 8); plain values in and a host-walked, capped
conversion out (items 9, 11 and 12); no host object and no pre-built
closure in the environment (items 7 and 9); no `coroutine` in version 1,
and wrappers for both `create` and `wrap` when it comes (item 7); the
guarded `pcall` and the rearming hook (item 11); the result cap (item 11);
a static check over Lua's own bytecode (item 3); and a kill timer outside
the interpreter's process (items 6 and 11).

Four places were read for conflict and agree. The dynamic index on a
string passes the static check in Key Discoveries, and Technical Design 3
says so and gives it to the second layer. Wall time: A2 records that one
long C call overruns the hook without bound (`nexus_rdr/242-research-29`);
item 11 enforces wall time with the kill timer of item 6, which runs in its
own thread, so verb time counts and a slow verb cannot hold it off; the
hook's clock is an inner layer that reports the same limit, with no claim
about which of the two fires first. The 60 s default of item 11 meets
RDR-240's A2 ("under a minute", `nexus_rdr/242-research-31`). Phase 1 Step
1 runs a bare child process and needs nothing Step 2 builds. The registry
probe's findings serve the approval layer withdrawn on 2026-11-07 and bind
nothing here (Technical Design 1).

### Assumption Verification

Research records: 34 in T2, `nexus_rdr/242-research-1` to `-34`: one for
each of the 21 Key Discoveries of 2026-11-02 and 2026-11-03 (1 to 21), one
for each of A1 to A7 (22 to 28), the gate round 1 measurement of A2 (29),
Sam's scope change of 2026-11-07 (30), the source text for the version 1
run rule and the related frontmatter (31), the macOS worker probes (32), the
worker facts taken without a probe (33), and the census for this fix round
(34). By class: 26 verified (22 by spike, 4 by source search), 2
documented, 6 assumed. Record 23 says A2's overrun is not measured; record
29 measured it, and the RDR follows 29.

Verified: A3, by a code read of each verb's nearest client call. A4, for
`lupa` 2.8 only: the calibration passed there, no other release in
`>=2.8,<3` was checked, and the calibration test in the suite is what fails
on a change.

Open, not resolved:

- A1: the native Windows install has not run. Phase 1 Step 1 runs it on
  qwent before anything else lands.
- A2: the wall-time overrun of one long C call is measured and unbounded
  (`nexus_rdr/242-research-29`: one pattern match ran 10.56 s in one
  instruction, two others did not return in 60 s and 20 s, the hook never
  fired). The in-process hook cannot hold wall time; the worker's kill timer
  does, and it is not built. Phase 1 Step 2 builds it and its tests. The
  instruction and memory limits hold for Lua 5.4 by spike.
- The worker facts of record 33 (Linux `RLIMIT_AS`, the Windows job and its
  limits, `SYSTEMROOT`, end of file on parent death): assumed, not probed.
  Phase 1 Step 2 tests each one on its platform.

A5, A6 and A7 no longer apply (`nexus_rdr/242-research-30`). They were
premises of the hardware-key approval layer, and this RDR no longer builds
it. Records 26 to 28 carry Sam's acknowledgement of 2026-11-07 (ask
`rdr242-assumptions`); the Layer 2 census still lists them as assumed. A1
and A2 (records 22 and 23) are assumed and not acknowledged, so the census
lists them too.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `LuaRuntime(register_eval, register_builtins, unpack_returned_tuples, attribute_filter, max_memory)` | `lupa` 2.8, `lupa.lua54` | Spike |
| `load(src, name, "t", env)`, `string.dump`, `debug.sethook` count hook | Lua 5.4 bundled in `lupa` 2.8 | Spike |
| `resource.setrlimit(RLIMIT_AS)` raises `ValueError`; `RLIMIT_CPU` ends a child with SIGXCPU | CPython 3.13, macOS | Spike (`nexus_rdr/242-research-32`) |
| `subprocess` start with `-I`, an empty environment and `close_fds=True` | CPython 3.13, macOS | Spike (`nexus_rdr/242-research-32`) |
| `RLIMIT_AS` on Linux; Job Object kill-on-close, time and memory limits | Linux; Win32 | Not verified (inferred, `nexus_rdr/242-research-33`) |
| `db.resolve_title`, `db.list_entries`, `db.search` | nexus T2 store | Source Search |
| `load_packaged_table` | `nexus.tables` | Source Search |

### Scope Verification

The five gaps map to deliverables. Gap 1 to Technical Design 5, 7 and 8 and
Phase 1. Gap 2 to items 9 and 10 and Phase 3. Gap 3 to items 11 and 12, the
worker process and kill timer of item 6, and Phase 1 Steps 2 and 3. Gap 4
to the end of item 10: a write verb is its own entry in the verb table,
granted only when declared and covered by an approval, and version 1 ships
none. Gap 5 to items 2 and 4 for version 1 (a run takes a library name,
never source) and to item 1 for the rest, which is the approval document's.

The RDR has no section headed Minimum Viable Validation. The end-to-end
proof in scope is Phase 4: RDR-240's first procedure, a reviewed file in a
library root, runs through `run_script`. It is in scope, not deferred.

Out of scope as written: approval and everything Technical Design 1 lists,
write verbs, tuple-space verbs (the aura stage of nexus-mdbds), and
`coroutine`. The worker process of Alternative 2 is in scope
(`nexus_rdr/242-research-29`).

### Cross-Cutting Concerns

- **Versioning**: `lupa>=2.8,<3`, raised deliberately. The calibration test
  and the escape suite fail on a bytecode layout change or a new global. The
  check re-runs before every run, so a stricter checker refuses a script an
  older one passed. The bundled Lua 5.4 patch release is not recorded (A4).
- **Build tool compatibility**: `lupa` installs from wheels with no
  compiler on the platforms checked; native Windows is open (A1).
- **Licensing**: `lupa` 2.8's package metadata says "MIT style" (read
  2026-11-07). The license of the bundled Lua was not read for this
  response.
- **Deployment model**: scripts run in a worker process started by the
  client (the `nx` CLI or the MCP server), one per run; the verbs run in the
  client (Technical Design 6 and 9). There is no engine change, so nothing
  rides an engine-service release.
- **IDE compatibility**: N/A.
- **Incremental adoption**: nothing runs a script until RDR-240's
  `run_procedure` calls `run_script`; neither the CLI nor MCP runs one
  directly (item 13). The phases land the worker, the sandbox, the static
  check and the verbs separately.
- **Secret/credential lifecycle**: N/A in this RDR, which holds no
  credential. The worker starts with an empty environment, so no tenant
  token reaches it (item 6). The approval credential is the approval
  document's (item 1).
- **Memory management**: one `LuaRuntime` per run, in its own worker
  process, discarded with it; a 64 MB `max_memory` cap, with a 1 GiB OS
  memory limit on the worker on Linux and Windows and none on macOS, where
  `setrlimit(RLIMIT_AS)` raises (`nexus_rdr/242-research-32`); at most 4
  workers per client process; a result cap of 1 MB of JSON and 32 levels,
  checked while the host walks the value. The 200 MB string returned with no
  cap is why the result cap is the host's.
- **Tenancy**: the verbs read T2 through the caller's own client, so a
  script reads only what its caller's tenant can (inferred, not read); this
  RDR adds no tenant-scoped state.
- **Day 2 operations**: none. The runtime stores nothing, and a worker
  leaves nothing behind.

### Proportionality

The RDR is about 1,240 lines for one layer: a sandbox in a worker process,
with a static check and seven read verbs. Approval, the other layer of the
2026-11-03 rewrite, moved out on 2026-11-07 (Technical Design 1). The
sandbox exists because of Gaps 1 and 3. Research Findings is the longest
part because it records 23 probe findings; each one forced a design item, is
cited by one, or is kept as a record for the approval document. Version 1 is
built for one consumer: seven read verbs, no write, no `coroutine`. The
worker process adds a process start per run and a round trip per verb call,
measured against a pass line in Phase 1 Step 2; it is there because no
in-process measure holds wall time (`nexus_rdr/242-research-29`). Two
sections could be trimmed before locking: the Investigation list repeats
questions the Key Discoveries answer, and Alternative 4 is a pointer to
RDR-240. Neither is trimmed in this fix round, which changes only what the
scope change and the round 2 findings named.

## References

- Bead nexus-mdbds.1; epic nexus-mdbds.
- `docs/rdr/rdr-239-tuple-space-dictionary.md`
- `docs/rdr/rdr-240-procedures-as-sandboxed-lua-scripts.md`
- `scripts/spikes/rdr242-lua-sandbox/` (probes, corpus and outputs)

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
- 2026-11-07: research recorded as structured findings, Finalization Gate
  responses written, plan steps re-headed to the template grammar (no design
  change).
- 2026-11-07: Gate round 1 — BLOCKED (1 Critical, 7 Significant, 1 ship-blocker(s)); commit `d2ccb784e`; critique `nexus_rdr/242-gate-critique-2026-11-07-r1`.
- 2026-11-07: gate round 1 fix for C1 (commit `a2ac4a885`, Sam's choice of
  the worker process): every run and every trial in a worker process with OS
  memory and CPU limits and a kill timer; Alternative 2 chosen.
- 2026-11-07: Gate round 2 — PASSED (0 Critical, 4 Significant, 0 ship-blocker(s)); commit `a2ac4a885`; critique `nexus_rdr/242-gate-critique-2026-11-07-r2`.
- 2026-11-07: post-gate fix round, Sam's choice "fix round, then accept".
  Scope change at Sam's direction (`nexus_rdr/242-research-30`): the
  hardware-SSH-key approval layer, the registry, the trial run and the
  `nx script` verbs withdrawn and marked replaced by a device-held passkey
  design in its own document (Technical Design 1); version 1 runs only
  reviewed library files (Technical Design 2); A5 to A7 no longer apply;
  Phase 2 replaced; retitled to the 2026-11-02 title. Round 2's S1 to S4
  fixed: the worker contract (Technical Design 6), wall time across verb
  calls, the 60 s default against RDR-240's A2, the Phase 1 cost
  measurement, Phase 1 Step 1 on a bare child process, and the static
  check in the worker. Technical Design renumbered 1 to 13.
