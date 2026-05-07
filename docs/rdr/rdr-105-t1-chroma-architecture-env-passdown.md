---
title: "T1 Chroma Architecture: Eliminate On-Disk Session Records via Env-Var Passdown"
id: RDR-105
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-07
accepted_date: 2026-05-07
related_issues: [nexus-lqp7, "GH-579", "GH-567", "GH-572", "GH-575", "GH-576"]
---

# RDR-105: T1 Chroma Architecture: Eliminate On-Disk Session Records via Env-Var Passdown

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

T1 (session-scoped ephemeral chroma) coordination across processes has produced six consecutive bug iterations in a 7-day window, `#567 → #572 → #574 → #575 → #576 → #579`, each closing a witnessed instance of the same class while a new instance manifests at the next un-audited code path. PR #577's commit message states the pattern explicitly: *"Three iterations each closed a witnessed instance of a class-of-bugs; the class re-instantiated at the next code path that wasn't audited."* The six-phase fix in 4.26.7 added invariant test scaffolding but the class re-instantiated again as #579 in 4.26.8, in multi-MCP-process territory the scaffolding doesn't reach.

The class is structural, not local: any defense added to the current architecture is overhead on a coordination problem the architecture *creates*. The defense plumbing has accumulated to ~1000-1300 source LOC (plus ~500 test LOC) across `t1_watchdog.py` (297), parts of `session.py` (~400), `mcp/core.py` (~250), `db/t1.py` (~200), and `hooks.py` (~100), all of it solving "two processes coordinating on a shared chroma server they discover via on-disk session records." Each layer adds an invariant another layer must respect; the bug class is what happens when one layer misses one of the others' invariants.

The requirement T1 actually serves is narrower than the architecture assumes: a **Claude-session-scoped ephemeral store** whose scope is exactly the lifetime of the MCP server that owns the session, with discovery limited to processes the dispatcher (or Claude Code itself) explicitly grants access to. The current architecture serves a broader, harder requirement, *"any process anywhere on the host can discover and connect to T1 via a multi-field session record that several writers maintain in sync"*, and the multi-writer coordination is what generates the bug class. The fix is structural: keep the single discovery file, but reduce to **one writer** writing **one trivial fact** (`host:port`); replace the rest of the discovery surface with explicit env passing for processes the dispatcher controls.

### Enumerated gaps to close

#### Gap 1: Discovery-via-on-disk-record is inherently racy

`sessions/<uuid>.session` is a coordination surface multiple writers (MCP lifespan, SessionStart hook, reconcile, sweep, watchdog) read and rewrite. Every fix in the #567..#579 series patched a missed write-order, missed comparison key, or missed ordering invariant. The class is "anything that breaks one of these invariants in any future code path." There is no terminal fix; only "no more witnessed instances yet."

#### Gap 2: Cross-process idempotency is unenforceable in-process

`_t1_chroma_init_if_owner` guards spawn via `if _OWNED_CHROMA: return`. This is module-level state, scoped to a single Python process. When a second MCP server starts under the same `claude_pid` (the #579 scenario), it sees an empty `_OWNED_CHROMA`, falls through to spawn, and overwrites the canonical record with a fresh `server_pid`. The first MCP's watchdog then exits with `reason=session_file_removed` and reaps the still-needed chroma. There is no in-process check that can detect another process's state without consulting the on-disk record, but the on-disk record is exactly what Gap 1 says is racy.

#### Gap 3: Silent ephemeral fallback is the data-loss generator

Multiple historical paths constructed `chromadb.EphemeralClient()` when discovery failed. `nx scratch put` in shell would land in a per-process ephemeral while `nx scratch list` later spawned a different ephemeral, hiding writes. PR #569 closed the constructor path; PR #576 closed the `_reconnect` path; the `_EPHEMERAL_ALLOWLIST` invariant test pins the rule. The pattern remains a temptation at every new resolver site. **The architectural answer is to never fall back.**

#### Gap 4: The watchdog's role is muddled

`t1_watchdog.py` serves two unrelated jobs: (a) reap orphan chroma when Claude Code dies ungracefully, and (b) detect "our session file was removed" as a coordination signal. (b) is only meaningful in the on-disk-record discovery model; (a) can be replaced by OS-level process-group cleanup or a periodic best-effort sweep that doesn't participate in any coordination protocol. Conflating the two created the failure mode in #579 (watchdog killing its own chroma when a sibling MCP overwrote the record).

#### Gap 5: Sub-agent dispatch sharing is implicit, not contractual

T1 sharing across sub-agents currently happens "if the sub-agent's process can find the parent's session record." This works for in-process Agent-tool sub-agents (which use the parent's MCP via tool calls anyway) and for `claude -p` sub-processes that inherit `NX_SESSION_ID`. There is no explicit dispatcher decision *"this sub-process should/should-not share the parent's T1"*. Sharing is a side effect of discovery succeeding. The result is that "isolation" and "sharing" both produce the same code path, with the choice driven by environment quirks rather than caller intent.

## Context

### Background

GH #579 (filed 2026-05-07, severity P1) reported that every chroma child the lifespan owns dies within ~5–30s of spawn, leaving T1 unreachable session-wide after the first reap. Investigation in bead `nexus-lqp7` traced the smoking gun to a cross-process spawn-and-overwrite race: a second MCP server starting under the same `claude_pid` reads `current_session`, finds the existing record, but its `_tcp_probe_alive(host, port, timeout=0.5s)` fails under load → falls through to spawn → `write_session_record_by_id` overwrites the canonical record with the new chroma's `server_pid`. The original watchdog then sees `_find_record_by_chroma_pid(old_pid)` return None → exits via `reason=session_file_removed` → its `_cleanup` calls `stop_t1_server(old_pid)` → original chroma killed.

The same investigation revealed that the unit test scaffolding for `_t1_chroma_init_if_owner` has 7 cases, all single-process. The cross-process spawn-and-overwrite scenario is structurally untestable in the existing scaffold. The release sandbox's "T1 sniff" counts BEFORE/AFTER session files but does not exercise an interactive Claude+MCP loop with sub-agent dispatch, which is where the bug fires.

### What T1 actually needs to be

The user-facing contract is:

1. **Session-scoped ephemeral storage** for the duration of one Claude Code session.
2. **Visible to sub-agents** dispatched by that session, when the dispatcher *intends* sharing.
3. **Isolatable** for sub-agents the dispatcher *intends* to seal off.
4. **Fail-loud on misconfiguration.** A consumer that cannot connect must raise, not silently land in a sandbox.

The current architecture serves a fifth, undocumented requirement that's responsible for the bug class:

5. *"Any process on the host can discover T1 via a multi-writer record system maintained by lifespan + hook + reconcile + sweep + watchdog in concert."*

(5) is implementational drift. Reverse the multi-writer property: discovery is allowed, but only one process (the top-level MCP) ever writes the discovery surface, and only once at lifespan start. Then (1)-(4) can all be served. The single-writer file is structurally trivial; the multi-writer coordination is what produced six bugs.

### Three architectural alternatives evaluated

**Alternative A, patch in place (status quo + #579 fix).** Add cross-process idempotency check via record TCP probe + retry with backoff. Extend `_EPHEMERAL_ALLOWLIST` invariant scope. Add multi-process integration test. Capture chroma stderr. Cost: ~200 LOC additions, 2-3 new tests, retains the entire bug class shape, the seventh manifestation lands at the next un-audited path. Predicted on the same logic that produced PR #577's prediction in its own commit message.

**Alternative B, collapse T1 into MCP process (in-process EphemeralClient only).** No HTTP server, no watchdog, no records. Cross-process consumers (`nx scratch` CLI, SessionEnd flush, `claude -p` shared sub-agents) must access via MCP. Cost: deletes ~1500 LOC; loses `claude -p` sharing semantics entirely. Requires reframing T1 as MCP-API-only.

**Alternative C, hybrid discovery (env for dispatched, single-writer file for siblings; this RDR's recommendation).** Top-level MCP runs an HTTP chroma server with lifecycle bound to its own lifespan. Discovery is hybrid:

- For **MCP-dispatched subprocesses** (`claude -p` shared, plan-runner, operator dispatch): explicit env passing in the dispatcher's `subprocess.Popen` / `asyncio.create_subprocess_exec` call (`env={**parent_env, "NX_T1_HOST": ..., "NX_T1_PORT": ...}`). This is how the current dispatcher already passes `NEXUS_SKIP_T1` and `NX_SESSION_ID`, proven mechanism.
- For **Claude-Code-dispatched siblings** (Bash-tool `nx scratch`, SessionStart/SessionEnd hooks, shell-side `nx` CLI): a single-writer address file at `~/.config/nexus/t1_addr.<claude_pid>` containing only `<host>:<port>\n`. Written once by the top-level MCP at lifespan start, deleted at lifespan finally. No multi-writer coordination, no UUID, no PID metadata in the file, no reconcile, no sweep. Sibling discovery is "walk PPID chain to your owning Claude → open `t1_addr.<claude_pid>` → connect."

Watchdog reduced to an optional periodic GC of orphan chroma processes (and orphan address files). Cost: deletes ~1500 LOC of the multi-writer coordination layer; retains a tiny address-file primitive (one writer, two-line file) for the cross-process discovery the current architecture needs to support but has been over-engineering. Retains `claude -p` sharing as an explicit dispatcher decision, retains `nx scratch` from Bash via the address file.

The recommendation is **Alternative C**. It is structurally narrower than A (eliminates the bug class instead of layering defense), structurally broader than B (preserves cross-process access for the dispatcher's chosen consumers), and aligns the discovery surface with the dispatcher's intent rather than with implicit filesystem state.

## Decision

Adopt **hybrid discovery** for T1: env-var passing for MCP-dispatched subprocesses, single-writer address file for Claude-Code-dispatched siblings. Eliminate the multi-writer session-record machinery, watchdog-coordination logic, reconcile, sweep, PID drift handling, UUID rolls, and the entire `_OWNED_CHROMA` complexity. Replace with one writer (top-level MCP at lifespan start) emitting one tiny file (`<host>:<port>\n`) consumed by sibling lookups.

### Why hybrid (research finding RDR-105-research-1)

Initial draft proposed env-var-only discovery. POSIX subprocess semantics rule that out for siblings: a child process (the MCP server) cannot mutate its parent's (Claude Code's) env, so Bash-tool subprocesses spawned by Claude Code do not see anything the MCP server set in `os.environ` at runtime. Verified in source: `src/nexus/operators/dispatch.py:142` explicitly passes env in its `Popen` call because implicit propagation doesn't exist; `src/nexus/hooks.py` and `src/nexus/commands/scratch.py` use on-disk discovery for the same reason. Env-var passing remains correct for MCP-dispatched subprocesses (the dispatcher pattern it already uses); siblings need a file.

### Address file is keyed by IMMEDIATE Claude ancestor, not topmost

A naive "walk PPID for the topmost `claude*` ancestor", the current `find_claude_root_pid` semantic, silently breaks owned-mode isolation. An owned `claude -p` subprocess's process tree contains two `claude*` ancestors: itself (`claude -p`, the immediate Claude parent) and the user's top-level Claude further up. The topmost-walk returns the user's Claude, so the owned subprocess would write its address file at the parent's `claude_pid` key (overwriting the parent's file) and read its own discovery from the parent's file (silently sharing instead of isolating).

The new architecture introduces `find_immediate_claude_pid()`, returns the FIRST `claude*` ancestor walking up from `os.getpid()`, not the topmost. Used by both the lifespan's address-file write and the constructor's Path B read. Verified across all four nesting cases (top-level Bash, owned subprocess MCP, Bash inside owned subprocess, nx scratch standalone), it produces the correct file key in each. The legacy `find_claude_root_pid` is deleted along with the watchdog code that used it.

### The six T1 cases

| Case | Process model | T1 source | Failure if missing |
|---|---|---|---|
| **Top-level MCP** | Owns the session | Spawns chroma HTTP server at lifespan start; tears down at lifespan end | n/a (it spawns) |
| **Agent-tool sub-agent** | Same process as parent | Uses parent's MCP scratch tool via tool dispatch | n/a (no separate T1 needed) |
| **`claude -p` shared** (`share_t1=True`) | Subprocess | Inherits `NX_T1_HOST` / `NX_T1_PORT` from parent's env at dispatch time (explicit `env=` in `Popen`), connects via HTTP | **Raise** `T1ServerNotFoundError` if env present but unreachable |
| **`claude -p` owned** (default) | Subprocess; its own MCP becomes a top-level | Dispatcher unsets `NX_T1_HOST/PORT` and `NX_T1_ISOLATED`; subprocess MCP spawns its own chroma + writes its own `t1_addr.<own_claude_pid>`. Sealed from parent; internally consistent for its own Bash tools and sub-agents. | n/a (it spawns its own) |
| **`claude -p` ephemeral** (`ephemeral=True`) | Stateless one-shot subprocess | Dispatcher sets `NX_T1_ISOLATED=1`; subprocess opens its own `chromadb.EphemeralClient()`. No chroma spawn, no address file. Current operator-dispatch pattern. | **Raise** if `NX_T1_ISOLATED` not set |
| **Bash-tool / shell `nx scratch`, SessionStart / SessionEnd hooks** | Sibling subprocess of MCP (spawned by Claude Code, not by MCP) | Calls `find_immediate_claude_pid()` (first `claude*` ancestor walking up); reads `~/.config/nexus/t1_addr.<claude_pid>`; connects via HTTP | **Raise** if file missing or unreachable |

### Fail-loud invariant

The only place `chromadb.EphemeralClient()` may be constructed in T1 code is the explicit `NX_T1_ISOLATED=1` branch. The existing `_EPHEMERAL_ALLOWLIST` invariant test enforces this for current sites; the new architecture removes the *implicit* fallback paths entirely. The constructor has four primary branches plus a test-injection escape hatch:

```python
def __init__(self, *, client=None):
    if client is not None:
        self._client = client                      # explicit injection (tests)
        return

    host = os.environ.get("NX_T1_HOST", "")
    port = os.environ.get("NX_T1_PORT", "")
    if host and port:
        # Path A: MCP-dispatched subprocess. Env was injected by the dispatcher.
        self._client = chromadb.HttpClient(host=host, port=int(port))
        # validate connectivity, raise T1ServerNotFoundError on failure
        return

    addr = _read_sibling_addr_file()  # find_immediate_claude_pid → t1_addr.<that_pid>
    if addr is not None:
        # Path B: Claude-Code-dispatched sibling (Bash tool, hook, shell nx scratch).
        host, port = addr
        self._client = chromadb.HttpClient(host=host, port=port)
        return

    if os.environ.get("NX_T1_ISOLATED") == "1":
        # Path C: explicit isolation opt-in (operator dispatch, sealed sandbox).
        self._client = chromadb.EphemeralClient()  # the ONLY ephemeral construction
        return

    raise T1ServerNotFoundError(
        "T1 not configured for this process. Either inherit "
        "NX_T1_HOST/NX_T1_PORT from a parent MCP server, run as a "
        "sibling of a top-level MCP server (so the address file is "
        "discoverable via PPID walk), or set NX_T1_ISOLATED=1 to opt "
        "in to an in-process ephemeral T1."
    )
```

### Dispatcher policy (three modes)

Per research finding RDR-105-research-5, `claude -p` dispatch has three semantically distinct modes, not two:

- **`share_t1=True`**, subprocess connects to caller's T1 via inherited `NX_T1_HOST/PORT`. Cross-process visibility into the parent session's scratch.
- **`owned` (default)**, subprocess gets its own T1 session: its MCP spawns its own chroma + writes its own `t1_addr.<own_claude_pid>` file. Internally consistent (its own Bash tools and sub-agents see the same T1), externally isolated from the parent.
- **`ephemeral=True`**, subprocess uses in-process `EphemeralClient` only (no chroma spawn). For stateless one-shot subprocesses (current operator dispatch pattern). Mutually exclusive with `share_t1`.

```python
from nexus.mcp import _t1_state                      # minimal shared-state module, no heavy deps

def dispatch_claude_p(*, share_t1: bool = False, ephemeral: bool = False, ...):
    env = parent_env.copy()
    if share_t1:
        assert not ephemeral, "share_t1 and ephemeral are mutually exclusive"
        if _t1_state.T1_ADDR is None:
            raise RuntimeError("share_t1=True requires top-level MCP's T1 to be live")
        host, port = _t1_state.T1_ADDR
        env["NX_T1_HOST"] = host
        env["NX_T1_PORT"] = str(port)
        env.pop("NX_T1_ISOLATED", None)
    elif ephemeral:
        env.pop("NX_T1_HOST", None)
        env.pop("NX_T1_PORT", None)
        env["NX_T1_ISOLATED"] = "1"          # disables chroma spawn in subprocess MCP
    else:
        # Default: subprocess owns its own T1 session
        env.pop("NX_T1_HOST", None)
        env.pop("NX_T1_PORT", None)
        env.pop("NX_T1_ISOLATED", None)
    subprocess.run(["claude", "-p", ...], env=env, ...)
```

**Default mode `owned`** (the subprocess gets its own T1, isolated from parent). Sharing or ephemeral semantics are explicit caller decisions, forcing the question *"do I want this subprocess to see my session's notes, or to skip T1 entirely?"* at dispatch time.

The current `src/nexus/operators/dispatch.py:142` use of `NEXUS_SKIP_T1=1` maps to the new `ephemeral=True` mode. Plan-runner dispatch maps to default `owned`.

### Lifespan spawn gate

The MCP server's lifespan replaces `_t1_chroma_init_if_owner` with a three-branch gate that mirrors the constructor's signal hierarchy. Mismatching gate vs constructor would leak orphan chroma processes (lifespan spawns; constructor decides not to use it; chroma runs forever). Implementation is a single `@asynccontextmanager` generator so `(server_pid, claude_pid, host, port)` are local variables shared across the `yield`, no class state, no callback indirection:

```python
from nexus.mcp import _t1_state                      # minimal shared-state module

@asynccontextmanager
async def t1_lifespan():
    # Branch 1: Subprocess that inherited parent's chroma. Don't spawn, don't write file.
    if os.environ.get("NX_T1_HOST") and os.environ.get("NX_T1_PORT"):
        yield                                        # constructor's Path A connects via env
        return

    # Branch 2: Subprocess opted into ephemeral. Don't spawn, don't write file.
    if os.environ.get("NX_T1_ISOLATED") == "1":
        yield                                        # constructor's Path C uses EphemeralClient
        return

    # Branch 3: Top-level or owned subprocess MCP. Spawn chroma + publish address.
    host, port, server_pid, tmpdir = start_t1_server()
    claude_pid = find_immediate_claude_pid()
    write_t1_addr(claude_pid, host, port)            # atomic temp-then-rename
    _t1_state.T1_ADDR = (host, port)                 # dispatcher reads this for share_t1=True
    try:
        yield
    finally:
        stop_t1_server(server_pid)
        unlink_t1_addr(claude_pid)                   # delete file as last act
        _t1_state.T1_ADDR = None
```

Three properties of the gate:

- **No `os.environ` mutation.** The lifespan stores `(host, port)` in a module-level variable, not in `os.environ`. The dispatcher reads the module variable when constructing subprocess env. This avoids the GIL-vs-FastMCP-thread-pool concern from Observation 3 and keeps `os.environ` immutable from the MCP's runtime perspective.
- **Symmetric branches with the constructor.** Each lifespan branch corresponds to exactly one constructor branch. Lifespan Branch 1 ↔ Constructor Path A; Branch 2 ↔ Path C; Branch 3 ↔ Path B (for siblings reading the file we just wrote) or "this MCP serves itself" (for tools running inside this MCP process). No path can spawn chroma that the constructor will then ignore.
- **Address file deleted as last act.** On shutdown, stop chroma first, unlink the address file last. A sibling that reads the file mid-shutdown either gets the prior valid contents (and fails to connect, which is fail-loud) or gets a missing file (also fail-loud). No partial-state window.

### Watchdog disposition

`t1_watchdog.py` is deleted. Its two responsibilities split:

- **Graceful chroma shutdown on Claude Code crash:** handled by the MCP server's lifespan finally + signal handler + atexit (the existing three-layer defense in `mcp/core.py`'s lifespan path), which already runs on stdio transports per the FastMCP integration.
- **Orphan chroma reap on ungraceful Claude Code death (SIGKILL, OOM):** handled by a periodic best-effort GC sweep at top-level MCP startup. The sweep enumerates `chroma run` processes whose anchoring claude_pid (recorded by the parent MCP at spawn time, in a single small file or via process-tree walk) has disappeared, and reaps them. **Not load-bearing**, a missed reap leaks ~50 MB until the next session's sweep catches it.

### Sub-agent T1 contract

CLAUDE.md and nx-skills documentation will be updated to state explicitly:

- Agent-tool sub-agents share T1 with their parent via the parent's MCP. No separate T1 instance.
- `claude -p` sub-processes default to `owned` mode, their MCP spawns its own session-scoped T1, sealed from the parent. Their internal Bash tools and sub-agents see consistent state within their own session.
- Stateless one-shot operators (`ephemeral=True`) skip chroma entirely, `EphemeralClient` only.
- Sub-processes that genuinely need parent-T1 visibility opt in via `share_t1=True` at dispatch time. Cross-process findings between sibling sub-processes should still go to T2 (`memory_put`), that's the process-shared tier by design.

This makes T2 the de-facto "shared bus across all processes" tier and T1 the per-MCP-process "working memory" tier, aligning the tier semantics with their underlying storage properties (T2 is SQLite + WAL = multi-process-safe; T1 is ephemeral = process-local).

## Consequences

### Code changes (deletions)

Approximately 1000-1300 source LOC removed (~1500 with stale tests):

- `src/nexus/t1_watchdog.py`, entire file (~300 LOC).
- `src/nexus/session.py`: `sweep_stale_sessions`, `write_session_record_by_id`, `write_session_record`, `find_session_by_id`, `find_ancestor_session`, `find_claude_root_pid` (replaced by new `find_immediate_claude_pid`; the topmost-walk semantic is what broke owned isolation), `_resolve_session_record_with_retry`, `read_claude_session_id`, `write_claude_session_id`, the `SESSIONS_DIR` concept (~400 LOC). **Retain** `start_t1_server`, `stop_t1_server`, `_ppid_of`, `_command_name_of`, `sweep_orphan_tmpdirs`, `_is_pid_alive`, all reused by the new architecture (server lifecycle + PPID-walk primitive + tmpdir reaping unchanged).
- `src/nexus/mcp/core.py`, `_t1_chroma_init_if_owner` reduced from ~150 LOC to ~30, `reconcile_owned_chroma` deleted (~60 LOC), `_tcp_probe_alive` retained (used for connectivity validation), `_OWNED_CHROMA` simplified to `(host, port, server_pid, tmpdir)` only.
- `src/nexus/db/t1.py`, `_reconnect`'s elaborate resolver chain deleted (~80 LOC), the PPID walker deleted (~40 LOC), the legacy numeric-stem migration deleted, constructor reduces to the three-branch shape above.
- `src/nexus/hooks.py`, sweep call deleted, `_resolve_session_records` simplified, the chroma-stop block in `session_end` simplified (just relies on lifespan finally).
- `tests/test_t1_invariants.py`, `tests/test_t1_watchdog.py`, large chunks of `tests/test_mcp_chroma_lifecycle.py`, `tests/test_session.py`, gone or replaced with new env-var-discovery invariants.

### Code changes (additions)

- `src/nexus/db/t1.py` constructor: 4-branch fail-loud gate (test-injection / env-HTTP / file-HTTP / isolation / raise) as specified in §"Fail-loud invariant".
- `src/nexus/session.py`: new helper `find_immediate_claude_pid()`, reuses existing `_ppid_of` walker but returns the FIRST `claude*` ancestor encountered (not the topmost like the legacy `find_claude_root_pid`).
- `src/nexus/session.py`: new helpers `write_t1_addr(claude_pid, host, port)` (atomic temp-then-`Path.replace`) and `read_t1_addr_for(claude_pid)`. Single writer (top-level / owned MCP at lifespan start), single reader contract (siblings via Path B).
- `src/nexus/mcp/_t1_state.py` (new minimal module, no upward imports): owns the `T1_ADDR: tuple[str, int] | None = None` module-level variable. Imported by both the lifespan (writer) and dispatch.py (reader). The minimal module avoids a circular import that would arise if dispatch.py imported from `mcp.core` (which transitively imports FastMCP, corpus, T3, heavy and circular-prone).
- `src/nexus/mcp/core.py` lifespan: three-branch spawn gate from §"Lifespan spawn gate", implemented as a single `@asynccontextmanager` generator so `(server_pid, claude_pid, host, port)` are local variables shared across the `yield` (no class state, no callback indirection). On startup writes `_t1_state.T1_ADDR = (host, port)`; on shutdown stops chroma, unlinks address file, sets `_t1_state.T1_ADDR = None`.
- `src/nexus/operators/dispatch.py` (and any other `claude -p` dispatch site): three-mode `share_t1` / `ephemeral` parameters per §"Dispatcher policy". Reads `(host, port)` from `nexus.mcp._t1_state.T1_ADDR` when `share_t1=True`. The minimal `_t1_state` module pulls only stdlib imports, no FastMCP, no chromadb, no corpus dependencies leak into operator subprocesses.
- `tests/test_t1_discovery.py` (new): end-to-end tests exercising all six cases (top-level / Agent-tool / shared / owned / ephemeral / sibling-discovery) plus failure paths (env present but unreachable, no env and no file, no env no file no isolation flag) and asserting fail-loud in each. Includes the **owned-mode respawn race** test: kill an owned MCP mid-flight; verify a respawned MCP either finds its own correctly-keyed address file OR raises (never lands on the parent's file).
- Periodic-GC orphan reaper: ~10 LOC at MCP top-level startup. Iterates `~/.config/nexus/t1_addr.<pid>` files; unlinks any whose `<pid>` is dead. Best-effort; reuses the existing `sweep_orphan_tmpdirs` for `nx_t1_*` tmpdir cleanup (independent surface, unchanged).

### Behavioral changes (user-visible)

- **`nx scratch` CLI from a Bash tool subprocess** continues to work, the Bash subprocess walks its PPID chain to its owning `claude_pid` and reads `~/.config/nexus/t1_addr.<claude_pid>` to discover the chroma address. (Env-var inheritance does NOT serve this case, per RF-1; the address file does.)
- **`nx scratch` CLI from a fresh shell with no Claude Code session running** now fails loud with `T1ServerNotFoundError` instead of silently landing in a per-shell ephemeral. Acceptable: there is no useful work to do without a parent session.
- **`claude -p` operator subprocesses** continue to use ephemeral T1 (the new `ephemeral=True` mode); existing code paths that set `NEXUS_SKIP_T1=1` keep working through the deprecation cycle (`NEXUS_SKIP_T1` honored alongside `NX_T1_ISOLATED` for the 4.27 → 4.28 cycle, removed in 5.0).
- **`claude -p` plan-runner / non-operator subprocesses** default to `owned` mode (their own session-scoped T1). Today these implicitly share with parent via on-disk-record discovery; the new default is sealed-from-parent, which is the safer default. Callers that genuinely need parent T1 visibility opt in via `share_t1=True`.
- **Session-record files** (`~/.config/nexus/sessions/<uuid>.session` and `current_session` pointer) are no longer written. The directory will be empty after migration; the one-time post-upgrade sweep removes any leftovers from prior installs.
- **MCP server crash mid-session** loses T1. Today's architecture nominally preserves it (orphan chroma survives MCP crash), but in practice the user always restarts Claude Code, which restarts MCP, which is the operative recovery anyway. Acceptable.

### Bug class elimination

GH #567, #572, #574, #575, #576, #579 all share the shape *"on-disk session record discovery races with another writer."* The new architecture has no on-disk session records and no second writer. The class cannot manifest because the surface it lived on is gone. New bug classes may emerge, every architecture has its own, but they will not be the class the prior six bugs belonged to.

### Test coverage shift

The `test_t1_invariants.py` scaffolding from PR #577 covered file-level invariants on the existing architecture (no silent EphemeralClient, reconcile rewrites JSON, subprocess SessionStart skips sweep, etc.). Most of those invariants become unenforceable because the code they pinned no longer exists. New invariants take their place:

- AST audit: `chromadb.EphemeralClient(` constructions confined to the explicit `NX_T1_ISOLATED` branch (the existing allowlist test, narrowed).
- AST audit: `T1Database.__init__` always raises if neither env condition is met (no implicit fallback).
- Integration test: dispatcher `share_t1=True` produces a working subprocess connection; `share_t1=False` produces a sealed sandbox; `share_t1=True` against a dead parent raises loudly.
- Integration test: top-level MCP startup spawns chroma, populates env, tears down on lifespan finally.

## Migration plan

### Phase 1, design lock + spike

- Author this RDR; gate it; accept it.
- Spike the hybrid-discovery lifespan in a branch alongside the current path (feature-flagged off). Two integration points to validate:
  1. **MCP-dispatched subprocess (env path):** dispatcher's `subprocess.Popen(env={..., "NX_T1_HOST": ..., "NX_T1_PORT": ...})` reaches the subprocess's `T1Database` constructor via `os.environ`. Standard POSIX subprocess semantics, but worth running once to confirm the FastMCP shell is not stripping or rewriting env.
  2. **Sibling discovery (file path):** Bash-tool subprocess calls `find_immediate_claude_pid()` and reads `~/.config/nexus/t1_addr.<claude_pid>`. The `_ppid_of` + `_command_name_of` primitives already work today (`session.py:120-147` / `:638-653`); the new helper `find_immediate_claude_pid` is a 15-line variant that returns the FIRST `claude*` ancestor instead of the legacy topmost-walk.
- **Exit criterion:** spike branch demonstrates (1) `claude -p` shared subprocess sees parent T1 via inherited env, (2) `nx scratch put` from a Bash tool reaches the parent MCP's chroma via the address file, both with the OLD discovery code disabled by feature flag.

### Phase 2: constructor gate + dispatcher

- Land the four-branch `T1Database.__init__` (test-injection / env-HTTP / file-HTTP / isolation / raise) behind a feature flag `NX_T1_NEW_DISCOVERY=1`. The flag-off path retains the existing record discovery.
- Land `find_immediate_claude_pid` and the new lifespan spawn gate behind the same flag.
- Land the dispatcher's explicit `share_t1` / `ephemeral` parameters behind the same flag.
- **Flag isolation contract:** flag-on and flag-off paths are mutually exclusive PER PROCESS. A given MCP server runs entirely on one path; tests target one path or the other but never both within a single process. The existing test suite continues to target flag-off (legacy session-record discovery); the new `tests/test_t1_discovery.py` targets flag-on. Cross-process tests (parent flag-on dispatching subprocess) are flag-on; the legacy session-record machinery is bypassed entirely on flag-on processes.
- **Exit criterion:** flag-on test suite green; flag-off behavior unchanged; CI runs both modes.

### Phase 3, flip the default

- Flip `NX_T1_NEW_DISCOVERY` default to on.
- Sandbox shakeout: run an interactive Claude Code session with multiple Agent-tool sub-agents, at least one `share_t1=True` `claude -p` dispatch, at least one `owned`-default `claude -p` dispatch, and the 10-parallel `claude -p` shared stress test (RF-3 verification). Watchdog log still exists at this phase; verify no `chroma_died_externally` events fire under normal operation.
- **Exit criterion:** sandbox session produces zero record-related log events under normal operation; T1 round-trip works across all six cases (top-level / Agent-tool / shared / owned / ephemeral / sibling); 10-parallel stress test runs to completion without errors.

### Phase 4, delete the old code paths

- Remove the feature flag.
- Delete `t1_watchdog.py`, the session-record machinery in `session.py` (sweep_stale_sessions, write_session_record_*, find_session_by_id, **`find_ancestor_session`** the record-based ancestor-walker, **`find_claude_root_pid`** the topmost-walker that breaks owned isolation; the generic `_ppid_of` + `_command_name_of` are retained as primitives for `find_immediate_claude_pid`), `reconcile_owned_chroma`, `_t1_chroma_init_if_owner` (replaced by lifespan spawn gate from §"Lifespan spawn gate"), the legacy numeric-stem migration, the stale tests.
- Add the periodic orphan-reaper (best-effort GC at MCP startup).
- **Exit criterion:** ~1000-1300 LOC source deletion + ~500 LOC stale test deletion on the diff; full unit + integration suite green; release sandbox shakedown shows zero session-record files written.

### Phase 5, release

- Versioning: this is a minor bump (4.27.0). The two user-visible behavior changes, `nx scratch` from a parent-less shell now raises, and `NEXUS_SKIP_T1` enters deprecation alongside `NX_T1_ISOLATED`, are both contained and documented. Neither breaks normal Claude-Code-mediated usage. If `NEXUS_SKIP_T1` removal warrants a major bump, it lands in 5.0 alongside that removal.
- CHANGELOG entry referencing this RDR, the six closed issues (#567/#572/#574/#575/#576/#579), and the deprecation notice.
- `nx doctor` extended to detect "no `t1_addr.*` file but Claude Code is the parent" and explain the diagnosis.

## Risks & open questions

All risks identified across the draft + research + Layer 3 critique have been bounded or closed. Summary of resolution status:

| Risk | Status | Resolved by |
|---|---|---|
| Env propagation MCP→sibling | Closed (negative) | RF-1, POSIX rules it out; architecture revised to hybrid |
| Discovery-file correctness (atomicity, races, PPID walk, concurrent sessions) | Bounded | RF-2, all four sub-risks reduce to standard mechanisms |
| chromadb HttpClient concurrency under load | Bounded | RF-3, workload audit + Phase 3 stress test |
| Migration path + behavior change in `nx scratch` standalone | Bounded | RF-4, one-time sweep + CHANGELOG + doctor diagnostic |
| Subprocess-of-subprocess nesting semantics | Bounded | RF-5, three-mode dispatcher API + recursion proof |
| Owned-mode isolation breaks under topmost-walk discovery (CRITICAL) | Closed (fix applied) | RF-6, `find_immediate_claude_pid` introduced; legacy `find_claude_root_pid` deleted |
| Lifespan spawn gate vs constructor signal mismatch (orphan chroma leak) | Closed | §"Lifespan spawn gate", three branches symmetric with constructor signals |
| `os.environ` mutation thread-safety vs FastMCP thread pool | Closed | Module-level variable used instead of `os.environ`; see §"Code changes (additions)" |
| Phase 2 feature-flag behavioral divergence | Closed | §"Phase 2" flag-isolation contract: flag-on/flag-off mutually exclusive per process |
| LOC deletion overstatement | Closed | Corrected to ~1000-1300 source + ~500 test = ~1500 total |
| `mcp.core._T1_ADDR` circular-import risk in dispatch.py | Closed | RF-7, extracted into minimal `nexus.mcp._t1_state` module with no upward deps |

### Closed during research

- **Orphan-reaper placement:** RF-2/RF-4, keep inside top-level MCP startup. Best-effort, not load-bearing. Two independent reap loops: (a) for each `t1_addr.<pid>` whose `<pid>` is dead, unlink the file; (b) reuse the existing `sweep_orphan_tmpdirs` (mtime-based, well-tested) for `nx_t1_*` tmpdirs. The two surfaces don't reference each other, `t1_addr` carries `host:port` only; tmpdir reaping is independent and handled by the existing logic. Total addition: ~10 lines for (a); (b) is unchanged from today.
- **`NEXUS_SKIP_T1` rename:** RF-4, honor both names for the 4.27 → 4.28 cycle, deprecation warning when only the old name is set, remove in 5.0.
- **chromadb HttpClient concurrency:** RF-3, workload audit shows expected concurrency stays at ≤10 per collection (the chromadb ceiling, which queues rather than drops). Phase 3 sandbox shakeout adds a 10-parallel `claude -p` stress test as empirical verification.

### Remaining low-stakes residuals

- **Address file namespace.** Current proposal: `~/.config/nexus/t1_addr.<claude_pid>`. Alternative: `$XDG_RUNTIME_DIR` (auto-cleaned by systemd on user logout, free orphan-file GC on Linux). Recommendation: stick with `~/.config/nexus/` for cross-platform consistency (macOS doesn't have `$XDG_RUNTIME_DIR`); rely on the periodic GC sweep. Decide during Phase 1 spike if `XDG_RUNTIME_DIR` is worth conditional Linux-specific use.
- **`exec -a` / process renaming.** A user running `exec -a custom_name claude` defeats the `find_immediate_claude_pid`'s `comm.startswith("claude")` match; the walk falls through to immediate PPID fallback, the address-file lookup misses, and `T1Database()` raises `T1ServerNotFoundError`. This is fail-loud (correct) but the error message could be misleading. Mitigation: `T1ServerNotFoundError` message includes the line "If you launched Claude Code via `exec -a` or a custom wrapper, ensure the process name starts with `claude`." Cosmetic, low priority.
- **PID-reuse edge case in orphan reaper.** On Linux, PIDs wrap at 4,194,304 (lower under some kernels). A `claude_pid` from a long-dead session could be reused by an unrelated process; the GC sweep would then skip cleanup of the genuine orphan (its PID looks alive, even though the alive process isn't Claude). Low-stakes: the reaper is best-effort, not load-bearing; the orphan eventually gets reaped when its (reused) PID dies. No mitigation required, but worth noting.

## Research Findings

### RF-1, Env-var propagation from MCP server to Claude-Code-spawned siblings does NOT work (NEGATIVE; verified)

**Source:** T2 entry `nexus_rdr/RDR-105-research-1` (id 1123).

**Claim that was wrong:** Initial draft proposed env-var-only T1 discovery, assuming the MCP server's runtime `os.environ` mutations would propagate to Bash-tool subprocesses spawned by Claude Code.

**What's actually true:** POSIX subprocess semantics are immutable on this. A child process (the MCP server) cannot mutate its parent's (Claude Code's) env. Sibling subprocesses (Bash tools) inherit Claude Code's env-at-spawn-time, not the MCP server's runtime env. Confirmed in source: `src/nexus/operators/dispatch.py:142` explicitly passes env via `env={**os.environ, ...}` because implicit propagation doesn't exist.

**Architectural impact:** Forced the revision from "env-only" to "hybrid" (env for MCP-dispatched subprocesses; single-writer address file for siblings). The class-elimination still holds, the deletion volume is unchanged because it's the multi-writer coordination layer that goes, not the discovery primitive itself. Replacing today's per-session UUID files (5 writers, racy) with a single host:port file (1 writer, trivially correct) is still the structural simplification the RDR exists for.

**Confidence:** High. POSIX semantics + source verification + dispatch.py:142 already demonstrating the workaround. No spike needed.

### RF-2, Discovery primitive (single-writer address file) correctness is bounded

**Source:** T2 entry `nexus_rdr/RDR-105-research-2` (id 1124).

Four sub-risks evaluated and resolved:

- **PPID walk reliability:** primitives `_ppid_of` (session.py:120-147) and `_command_name_of` (session.py:638-653) are already implemented and battle-tested. Handle `/proc` (Linux + containers) + `ps` fallback (macOS). Production-tested. The new `find_immediate_claude_pid` is a 15-line variant that uses the same primitives but returns the FIRST (not topmost) `claude*` ancestor.
- **Atomic file write:** standard temp-then-`Path.replace()` pattern. POSIX `rename(2)` semantics, a concurrent reader sees either the prior contents or the new contents, never torn. 3 lines.
- **Read-during-startup race:** lifespan writes `t1_addr` *before* signaling MCP-ready. Practically unobservable in normal usage; mitigation is two lines of ordering.
- **Concurrent Claude sessions:** file naming `t1_addr.<claude_pid>` is collision-free by construction. Each Claude session writes its own keyed file; sibling PPID walks find their own.

**Confidence:** High. All four reduce to standard mechanisms; no novel coordination invented.

### RF-3, chromadb HttpClient concurrency is not load-bearing under expected workload

**Source:** T2 entry `nexus_rdr/RDR-105-research-3` (id 1125).

Per `src/nexus/db/chroma_quotas.py`: `MAX_CONCURRENT_READS = 10`, `MAX_CONCURRENT_WRITES = 10` per collection. Workload audit shows worst-case concurrency (10 parallel `claude -p` shared subprocesses) brushes but doesn't exceed the ceiling, and chromadb queues rather than drops on ceiling-hit. The new architecture does not increase chroma load relative to today, same connection count, same op rate, only different discovery mechanism.

**Phase 3 verification:** sandbox shakedown adds a 10-parallel `claude -p` shared scratch stress test as empirical confirmation. Cheap to add.

**Confidence:** High. Quota math + quota behaviour (queue-not-drop) bound the risk.

### RF-4, Migration is a one-time sweep + a contained behavior change

**Source:** T2 entry `nexus_rdr/RDR-105-research-4` (id 1126).

Pre-existing on-disk state to handle:
- `~/.config/nexus/sessions/<uuid>.session` files (legacy, post-migration unread)
- `~/.config/nexus/current_session` pointer (legacy, post-migration unread)
- Orphan `nx_t1_*` tmpdirs in `$TMPDIR`

One-time cleanup at first MCP startup post-upgrade: sweep dead-pid records, unlink `current_session`, sweep orphan tmpdirs (existing `sweep_orphan_tmpdirs` logic retained). Mid-upgrade sessions are not disrupted (legacy chroma keeps running; T1 contents lost on next session, which matches T1's ephemeral contract).

Behavior change: `nx scratch` invoked from a fresh shell with no Claude Code parent currently silently lands in a per-shell EphemeralClient → writes vanish. New architecture **raises** with explicit guidance message + `nx doctor` diagnostic. Communicate via CHANGELOG.

`NEXUS_SKIP_T1` honored alongside `NX_T1_ISOLATED` for 4.27→4.28 cycle with deprecation warning, removed in 5.0.

**Confidence:** High. Migration is mechanical; behavior change is intentional and contained.

### RF-5, Subprocess-of-subprocess nesting semantics are clean across all cases

**Source:** T2 entry `nexus_rdr/RDR-105-research-5` (id 1127).

Four nesting cases evaluated (top-level Claude → Bash; top-level → `claude -p` shared → Bash; top-level → `claude -p` owned → Bash; operator dispatch → ephemeral). All four resolve to exactly one matching constructor branch under the precedence env > file > isolation > raise.

**Refinement to dispatcher API:** initially drafted as two-mode (shared/isolated) but research surfaced a third semantically distinct mode: **owned**. An isolated `claude -p` that owns a session of its own (typical case) should still spawn its own chroma + write its own `t1_addr` so its internal Bash tools and sub-agents share consistent state within the isolated context. Only operator-style stateless subprocesses (current `NEXUS_SKIP_T1=1` pattern) want true ephemeral-only behavior.

**API revision:** dispatcher takes `share_t1: bool = False` and `ephemeral: bool = False` (mutually exclusive). Default `owned` (subprocess gets its own T1 session). The `Decision` section above reflects this revised three-mode API.

**Nested owned recursion (Observation 2 from Layer 3 critique):** an owned `claude -p` subprocess that itself dispatches a further owned sub-subprocess works by construction. The outer dispatcher unsets `NX_T1_HOST/PORT/ISOLATED`; the inner dispatcher does the same; each owned MCP at each nesting level calls `find_immediate_claude_pid()` from inside its OWN process tree, finding the IMMEDIATE `claude*` ancestor (which is the `claude -p` subprocess that spawned it, with its own unique PID). Each level writes `t1_addr.<that_immediate_pid>`, sealed from the next level up. Depth-N nesting is correct as long as each level has its own unique `claude_pid` (always true: `claude -p` spawns are independent OS processes with distinct PIDs).

**Confidence:** High. Consistent precedence semantics across all four nesting cases plus correct recursion to arbitrary depth.

### RF-6, Owned-mode isolation requires `find_immediate_claude_pid` semantic, not topmost-walk (CRITICAL fix; verified)

**Source:** Layer 3 substantive-critic finding; T2 entry `nexus_rdr/RDR-105-gate-latest` (id 1128).

**The bug the initial draft would have shipped:** Constructor Path B used the existing `find_claude_root_pid` (returns topmost `claude*` ancestor). For an owned `claude -p` subprocess MCP, the topmost claude IS the user's top-level Claude (the parent). So:

- The owned MCP's lifespan would write `t1_addr.<top_level_claude_pid>`, **overwriting the parent's address file** with the owned subprocess's chroma's address. Catastrophic for parent.
- The owned MCP's `T1Database()` constructor's Path B would read `t1_addr.<top_level_claude_pid>`, **connecting the owned subprocess to the PARENT'S chroma** instead of its own. Owned isolation silently broken, the architecture would reproduce the very behavior it claimed to eliminate, just via a different file.

**The fix:** Introduce `find_immediate_claude_pid()`, returns the FIRST `claude*` ancestor walking up from `os.getpid()`, NOT the topmost. Used by both the lifespan address-file write and the constructor's Path B read. Verified across all four nesting cases:

| Caller | Topmost (broken) | Immediate (correct) |
|---|---|---|
| Bash from top-level Claude | top-level | top-level ✓ (same as topmost since only one claude in chain) |
| Owned subprocess MCP | top-level (BUG) | own claude -p ✓ |
| Bash inside owned subprocess | top-level (BUG) | own claude -p ✓ |
| nx scratch standalone | immediate_ppid fallback | immediate_ppid fallback ✓ |

The legacy `find_claude_root_pid` is deleted (its only production caller was `_t1_chroma_init_if_owner`, also deleted).

**Confidence:** High. The defect is structural in the topmost semantic; the immediate semantic is structurally correct. Verified by walking each nesting case's process tree.

### RF-7, Shared T1 address state lives in a minimal module to avoid circular imports

**Source:** Layer 3 re-gate finding (substantive-critic, 2026-05-07).

**The risk identified:** Initial revision specified the dispatcher reading `(host, port)` from `mcp.core._T1_ADDR`. `mcp/core.py` transitively imports FastMCP, corpus, T3, db.t1, and `db/t1.py` imports from `nexus.session`. The dispatcher (`operators/dispatch.py`) sits at a different layer than `mcp/core`. Importing `mcp.core` from dispatch.py would (a) pull heavy deps into operator subprocesses that don't need them, and (b) risk a circular-import path if any of those transitively imported modules ever needs to call into dispatch.

**Resolution:** Extract a minimal `nexus.mcp._t1_state` module containing only:

```python
# nexus/mcp/_t1_state.py, minimal shared state, no upward imports.
T1_ADDR: tuple[str, int] | None = None
```

Stdlib-only (just typing). Imported by both the lifespan (writer) and the dispatcher (reader). No FastMCP, no chromadb, no corpus dependencies leak into operator subprocesses. No circular path possible because the module has no imports beyond stdlib.

**Lifespan implementation note:** The gate must be a single `@asynccontextmanager` generator so `(host, port, server_pid, claude_pid)` are local variables shared across the `yield`. A two-function presentation (`async def lifespan_startup()` + `async def lifespan_shutdown()`) loses this implicit shared closure and would force class-state or callback indirection. The pseudocode in §"Lifespan spawn gate" reflects the contextmanager pattern.

**Confidence:** High. Three-line module addition; pattern is the canonical Python solution to this layering issue.

## References

- GH #579, open P1 shakeout that triggered this RDR.
- GH issues closed by the prior six iterations: #567, #572, #574, #575, #576 (plus #579 still open).
- Bead `nexus-lqp7`, investigation findings, including the cross-process spawn-and-overwrite trace.
- PRs that landed the prior iterations: #569 (closed #567), #573 (closed #572), #574 (sticky-flag for #573-era issue), #575 (closed #574-era), #577 (six-phase fix, closed #575+#576).
- T2 entries: `nexus_rdr/RDR-105` (1122), `RDR-105-research-1..5` (1123–1127), research findings preserved beyond session.
- `src/nexus/t1_watchdog.py`, `src/nexus/mcp/core.py`, `src/nexus/db/t1.py`, `src/nexus/session.py`, `src/nexus/hooks.py`, source files this RDR rewrites.
- `tests/test_t1_invariants.py`, `tests/test_t1_watchdog.py`, `tests/test_mcp_chroma_lifecycle.py`, `tests/test_session.py`, current scaffolding (will be replaced).
- `docs/architecture.md`, three-tier T1/T2/T3 design context.
