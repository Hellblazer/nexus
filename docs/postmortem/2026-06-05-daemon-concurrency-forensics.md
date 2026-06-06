# Daemon Concurrency Forensics — T2 Daemon Silent 100% CPU Peg + Lifecycle Bug Class

- **Date:** 2026-06-05
- **Version under test:** conexus 5.10.3 (local + PyPI), branch `chore/reconcile-develop-with-main`
- **Primary bead:** nexus-xmohw (P1, open) — "Daemon self-pegs 100% CPU … root-cause RDR needed"
- **Related beads:** nexus-x47yx, nexus-we61e (closed), nexus-hcw0g, nexus-whl8n (RDR-140 P4), nexus-00en9
- **Method:** 6-agent diagnostic workflow (5 parallel hypothesis owners → synthesis) followed by an independent verification/authentication pass against the live tree, culminating in a **live `sample(1)` capture of the daemon while pegged**.

> Authentication policy for this document: every claim is tagged
> **[CODE]** (verified by reading the cited file:line), **[LIVE]** (verified
> against the running system / live artifact), or **[INFERRED]** (consistent
> with evidence but not directly observed). Claims that the diagnostic
> workflow asserted but that verification **overturned** are called out
> explicitly under "Corrections to the workflow synthesis."

---

## 0. Executive summary

The recurring "daemon pegs 100% CPU, no logs, lone daemon" symptom (nexus-xmohw)
was **captured live** during this investigation. The macOS `sample` profiler
shows the asyncio event loop spinning at ~100% CPU **entirely inside the kqueue
selector syscall** (`kevent`), with **3561 of 3565 samples (99.9%)** in
`select_kqueue_control_impl → kevent`. The daemon had **only 5–7 open fds and
zero connected client processes** at the time. 100% CPU with no work and no
clients is the textbook signature of an **asyncio selector busy-loop**: a file
descriptor that is perpetually reported "ready" by the kqueue, so the event
loop never sleeps.

The ready fd is a **half-open accepted UNIX-domain socket**. `lsof` showed
accepted connections on `t2.sock` **accumulating** (fds 31u, 33u, 35u over the
observation window) with **no peer process alive** — clients that connected and
vanished, leaving the daemon-side socket half-closed and still registered in
the selector. The application-level read loop is correct (it breaks on EOF and
closes the writer); the defect is at the **asyncio transport / EOF-handling
layer**, which the workflow's H1 agent did not inspect.

This **refutes the workflow synthesis's headline** (RC-1: "aggregate dispatch
saturation from ~35 orphan MCP clients"). At capture time there were **zero**
production MCP client orphans; the "~35 processes" were 66 **test** daemons
against isolated `/tmp` DBs (a separate, real defect — RC-5 below). The CPU
peg is a selector spin, not load.

Five additional, independently-confirmed structural defects in the
daemon-lifecycle path compound the cascade. They are catalogued below with
authentication tags.

---

## 1. The live capture (the decisive evidence)

During verification the production T2 daemon (pid 82698) was observed rising
from 2.3% to **100.0% CPU** (uptime ~10 min, `ppid 1`, singular). Captured with
the macOS native sampler (works on one's own processes; **py-spy requires root
on macOS and could not be used** — `py-spy dump` returned "This program
requires root on OSX"):

```
$ sample 82698 5
…
3561  _PyEval_EvalFrameDefault + 191600
  3561  select_kqueue_control_impl + 456
    3561  kevent  (in libsystem_kernel.dylib)        # 99.9% of 3565 samples
```
**[LIVE]** Sample preserved at `/tmp/daemon-peg-sample-82698.txt`. A second 3s
sample reproduced the same `kevent` dominance (2064/2064 in the selector
subtree), confirming a **stable** spin, not a transient drain.

```
$ lsof -p 82698 | grep -E 'unix|TCP'
fd  5u  unix  ->0x7bb9…    # socketpair half  (asyncio self-pipe / loopback pair)
fd  6u  unix  ->0xd7d2…    # socketpair half
fd 31u  unix  …/t2.sock    # UDS listener
fd 32u  IPv4  TCP LISTEN
fd 33u  unix  …/t2.sock    # accepted connection — NO peer process
fd 35u  unix  …/t2.sock    # accepted connection — NO peer process  (appeared later)
$ lsof …/t2.sock           # who else is connected?
COMMAND  PID   USER          FD
python3  82698 hal.hildebrand …   # ONLY the daemon's own fds — no client
```
**[LIVE]** Interpretation: accepted server-side UDS connections accumulate
(33u, then 35u) with no client process on the other end. 100% CPU with 0 active
clients ⇒ the loop is not doing work; it is being woken immediately and
repeatedly by a ready-but-dead fd.

### 1a. Second capture — replacement daemon, multi-thread view [LIVE]

The first daemon (82698) exited mid-investigation (a takeover/restart — the
cascade itself). Its replacement (pid **31605**, v5.10.3, lone, ppid 1) reached
**100% CPU in ~4 minutes** — a faster re-peg, confirming the reproducer
(`/tmp/daemon-peg-CAPTURE-B-31605.txt`). A full multi-thread sample shows two
*simultaneous* problems:

- **Main event-loop thread: 3079/3080 samples in `kevent`** — the same selector
  spin. The mechanism is now fully explained: the leaked half-open fd keeps a
  transport `_read_ready` callback perpetually scheduled, so the loop's ready
  queue never empties; asyncio then calls the selector with **timeout 0** on
  every iteration. `kevent(timeout=0)` returns instantly → 100% CPU, with the
  per-iteration Python callback work too small to register in the profile.
- **A thread-pool thread: 3080/3080 in
  `pysqlite_connection_execute_impl → sqlite3_step → sqlite3VdbeExec →
  sqlite3BtreeBeginTrans → btreeInvokeBusyHandler → sqlite3OsSleep → nanosleep`**
  — a write transaction parked in **SQLite's busy-handler retry**, unable to
  acquire the write lock. A *third* thread sat in `rlock_acquire →
  _pthread_cond_wait` (queued on the store's Python lock behind it).

A 3s re-sample showed the SQLite busy-handler frames **gone** while CPU stayed
at 100% — so the write-lock stall is **transient/intermittent** (a write
briefly waiting for the lock, the "database is locked" mechanism), whereas the
`kevent` spin is the **persistent** 100% burn. Two distinct defects, captured
together. fd count oscillated 2↔4 across samples (connections leak, some clear)
— net accumulation is the leak.

---

## 2. Root causes (authenticated)

### RC-α — Silent 100% CPU peg = asyncio kqueue selector busy-loop on a half-open accepted socket  **[LIVE + CODE]**  — THE headline, confidence HIGH

- **Live mechanism [LIVE]:** event loop pinned in `kevent` (§1); accepted UDS
  connections leak with no peer; spin is stable across samples.
- **Code surface [CODE]:** `_handle_connection` (`src/nexus/daemon/t2_daemon.py:1271-1304`)
  reads frames with `await read_frame(reader)` and on EOF raises
  `IncompleteReadError` → `break` → `finally: writer.close(); await writer.wait_closed()`.
  The **application** loop is correct. The leak is therefore at the asyncio
  **transport** layer: when a peer dies without a clean FIN (RST / abrupt exit),
  the accepted socket reaches EOF but the `_SelectorSocketTransport`'s read fd
  is not unregistered from the kqueue, so `kevent` reports it readable on every
  iteration and the loop never blocks. (Compounded by RC-ε: `ConnectionResetError`
  is not caught in the handler, so an abrupt-RST path exits without the orderly
  EOF `break`.)
- **Why "no logs":** all RPCs that *do* arrive succeed; the spin itself logs
  nothing; reclaim no-ops; the leaked-fd read events feed `b''`/EOF that the
  transport handles silently.
- **Open sub-question [INFERRED]:** exact asyncio internal (transport not
  closed vs. reader not paused after `feed_eof`) needs one more capture with
  `loop.set_debug(True)` slow-callback logging, or a `gc`-walk of live
  transports. The *class* is settled; the precise line in CPython/our protocol
  is the remaining unknown.

### RC-β — Intra-daemon SQLite write-lock contention (the "database is locked" burn)  **[LIVE + CODE]** confidence HIGH
Capture B (§1a) caught a thread-pool thread stuck in
`sqlite3BtreeBeginTrans → btreeInvokeBusyHandler → sqlite3OsSleep` — a
`BEGIN`/write that could not get the WAL write lock — while a second thread
queued on the store's Python `rlock`. It cleared within 3s (transient, not a
deadlock). This is the **intra-daemon** form of "database is locked": multiple
`asyncio.to_thread` write dispatches (serve-path writes + the 30s reclaim + the
per-`hello` connection from RC-1) contend for SQLite's single writer slot.
`reclaim_stale` bounds its own retry (3 attempts, sleeps `(0.1, 0.5)`,
`aspect_extraction_queue.py:489-491`), but serve-path writes fall back on
SQLite's default busy handler, which spins in `OsSleep` rather than yielding
cooperatively. Distinct from RC-α (the steady 100% CPU); RC-β is the
intermittent write-stall surfaced as `T2ClientError(database is locked)` to
callers (beads nexus-00en9, nexus-x47yx), amplified when RC-2/RC-3 let a second
daemon coexist.

### RC-2 — `stop()` never calls `mark_shutting_down()` → zero-daemon window  **[CODE]** confidence HIGH
`T2Daemon.stop()` (`t2_daemon.py:1079`) closes the servers (`:1133-1136`,
bounded by `_GRACEFUL_STOP_TIMEOUT`) and then `relinquish()`s (`:1151`), but
never calls `ServiceRegistry.mark_shutting_down()` — which **exists** at
`service_registry.py:328` and is **never invoked from the daemon**. For the
whole drain the lease still reads "live" (TTL 3.0s) while sockets are closing,
so a concurrent `ensure-running` resolves an apparently-live endpoint, fails to
connect, and treats it as a crash. **This is the x47yx cascade trigger.**

### RC-3 — `heartbeat_tick()` blocks the event-loop thread on `flock`  **[CODE]** confidence HIGH
`_reassert_discovery_loop` (`t2_daemon.py:990`) calls `supervisor.heartbeat_tick()`
**directly in the coroutine** (not via `asyncio.to_thread`). Chain:
`heartbeat()` (`service_registry.py:284`) → `_elect()` →
`fcntl.flock(fd, LOCK_EX)` (`:200`, blocking). With `DEFAULT_TTL = 3.0`
(`:64`), any flock stall > 3s freezes the event loop long enough for the lease
to age out, making a *running* daemon look absent → spawn cascade. Independent
of RC-α; either can trigger the cascade.

### RC-4 — Crash-loop sentinel R-M-W is non-atomic when the election lock times out  **[CODE]** confidence HIGH
`ensure-running` proceeds **unguarded** when `_acquire_election_lock` returns
`None` (`commands/daemon.py:1040-1046`), then performs the read-modify-write of
the crash-loop sentinel (`_crashloop_tripped` → `_read_crashloop` →
`_write_crashloop_atomic` → `_record_restart`, `:1123-1143`). `os.replace` is
atomic per write but does not serialize concurrent writers, so K racers
lost-update the count. (= bead nexus-whl8n / RDR-140 P4.) Over-counts from
RC-2's false crashes and under-counts from this race both degrade guard
accuracy.

### RC-5 — Test daemon reaper is format-blind after RDR-149 P2 → live orphan leak  **[CODE + LIVE]** confidence HIGH
`tests/_daemon_leak_guard.py:39` reads `json.loads(disc.read_text()).get("pid")`
— **top-level** `pid`. RDR-149 P2 moved the pid under `endpoint`. Verified
against the live lease file:
```
$ cat ~/.config/nexus/t2_addr.501
{"endpoint": {"pid": 82698, "tcp_host": "127.0.0.1", "tcp_port": 63817,
              "uds_path": ".../t2.sock"}, … "ttl": 3.0, "version": "5.10.3"}
# top-level "pid"? NO.  endpoint.pid? YES.
```
**[LIVE]** So `_read_daemon_pid` returns `None`, the reaper signals nobody, and
every `test_t2_multistack_race` run leaks one daemon (spawned
`start_new_session=True`, `commands/daemon.py:1146-1154`, ppid→1). **Live count:
66 orphan test daemons + 252 `/tmp/nxt2race-*` dirs.** These run against
**isolated** `/tmp` DBs and do **not** touch the production daemon — they are
the population the workflow misread as "orphan MCP clients."

### RC-ε — `_handle_connection` does not catch `ConnectionResetError`  **[CODE]** confidence HIGH, and a contributor to RC-α
`t2_daemon.py:1274` catches `asyncio.IncompleteReadError` (graceful FIN) but not
`ConnectionResetError`/`OSError` (abrupt RST). An abrupt client death therefore
does **not** take the orderly `break`→`finally: writer.close()` path; the
exception propagates out of the handler task, the transport teardown is skipped,
and the half-open fd is left registered — directly feeding RC-α.

### RC-1 (workflow's headline) — per-`hello()` fresh SQLite connect  **[CODE], but NOT the peg cause** confidence: real-inefficiency / not-causal
`T2Database.stored_schema_version()` (`db/t2/__init__.py:477`) opens a fresh
`sqlite3.connect()` on every call; `hello()` calls it per handshake. Genuine
waste worth caching. **But** the live capture shows 0 clients and a `kevent`
spin, so this is **not** the cause of the silent peg. Documented to prevent it
being mistaken for the root cause again.

---

## 3. Refuted / corrected claims

| Claim | Source | Verdict | Authentication |
|---|---|---|---|
| Silent peg = aggregate dispatch saturation from ~35 orphan MCP clients (RC-1) | workflow synthesis (primary) | **REFUTED as cause** | [LIVE] 0 prod MCP orphans; 100% CPU with 5–7 fds, 0 clients; spin is in `kevent`, not in `sqlite3.connect`/thread-pool |
| "~35 lingering nexus.cli processes" are orphan MCP clients | dossier/synthesis | **MISATTRIBUTED** | [LIVE] they are 66 *test* daemons (`nxt2race`) on isolated /tmp DBs |
| Silent peg = selector busy-loop from a half-open socket (H1 lead) | original hypothesis | **VINDICATED** | [LIVE] kqueue/`kevent` spin + accumulating peerless accepted sockets — but the H1 *agent* wrongly refuted it by only checking the app read loop |
| Reclaim_stale is a tight no-backoff retry spin | dossier (historical) | **NOT present in 5.10.3** | [CODE] `reclaim_stale` retries a bounded 3 attempts with sleeps `(0.1, 0.5)` (`aspect_extraction_queue.py:489-491`) |
| `_reclaim_stale_loop` / `_reassert_discovery_loop` can spin | candidate | **REFUTED** | [CODE] both reach an unconditional `await asyncio.sleep(...)` every iteration (`:1058`, `:980`) |

---

## 4. Causal graph

```
RC-α  half-open accepted socket left registered in kqueue
  └─> event loop spins in kevent at ~100% CPU            [THE silent peg]
        └─> daemon slow to answer hello()
              ├─ RC-3 (heartbeat flock on loop thread) independently stalls hello() too
              └─> staleness/lease misjudgment by ensure-running
                    ├─ RC-2 (no mark_shutting_down) widens the false-crash window
                    └─> spawn replacement  ──> 2+ daemons ──> "database is locked" on T2 writes (x47yx)
                          └─ RC-4 (R-M-W race) corrupts the crash-loop counter
                                └─> guard trips early / under-counts

RC-ε (ConnectionResetError uncaught)  ──feeds──>  RC-α (skips transport teardown on abrupt client death)
RC-5 (reaper format-blind)  ──>  66 leaked TEST daemons + 252 /tmp dirs   [isolated; NOT in the prod cascade]
RC-1 (per-hello sqlite connect)  ──>  wasted CPU under real load   [not causal for the lone-daemon peg]
```
**Trigger of the silent peg:** RC-α (+RC-ε). **Trigger of the multi-daemon
"database is locked" cascade:** a slow-`hello()` daemon (RC-α and/or RC-3) →
RC-2 amplifies → spawn → RC-4 corrupts accounting.

---

## 5. Fix-directions (diagnosis-level; not an implementation plan)

Ranked by leverage:

1. **RC-α (the peg):** ensure abrupt-death accepted sockets are torn down and
   unregistered from the selector. Concretely: catch `(ConnectionResetError,
   OSError)` alongside `IncompleteReadError` in `_handle_connection` (RC-ε
   one-liner, `:1274`); audit that the `finally` transport-close actually
   unregisters the fd; consider an idle/read-timeout on accepted connections so
   a peerless socket cannot live forever. **This warrants the RDR nexus-xmohw
   calls for** and is the only fix that stops the 100% CPU spin.
2. **RC-2:** call `self._registry.mark_shutting_down(self._lease_record)` as the
   first statement of `stop()` (`:1079`) — closes the zero-daemon window and the
   x47yx cascade.
3. **RC-3:** `await asyncio.to_thread(supervisor.heartbeat_tick)` (`:990`) — keep
   the blocking flock off the event-loop thread.
4. **RC-5:** `data.get("endpoint", {}).get("pid") or data.get("pid")` in
   `_daemon_leak_guard.py:39` — one line; stops the live 66-daemon/252-dir leak.
   (Bead, not RDR.)
5. **RC-4:** dedicated `flock` around the crash-loop R-M-W, independent of the
   election lock. (Bead.)
6. **RC-1:** cache `stored_schema_version()` at `T2Database.__init__`. (Bead;
   efficiency, not correctness.)

RDR scope = RC-α + RC-2 + RC-3 (the lone-daemon peg and its cascade). RC-4/5/ε/1
are standalone beads. **Do not ship a fourth speculative patch on this
subsystem without the RC-α teardown fix** — that is the repeated-patch trap
(5.10.1/2/3 all patched around this without catching the selector spin).

---

## 6. What remains unknown

- **[INFERRED]** The exact asyncio internal keeping the fd registered
  (transport-not-closed vs. reader-not-paused-after-`feed_eof` vs. an
  exception-skipped teardown via RC-ε). Settle with one capture under
  `loop.set_debug(True)` slow-callback logging, or a live `gc` walk enumerating
  `asyncio` transports and their fds at peg time.
- **[INFERRED]** Whether RC-3's flock stall ever independently exceeds the 3.0s
  TTL in production, vs. only contributing latency. Needs the debug-loop slow
  callback log.
- The reproducer for RC-α: which client-exit pattern (RST vs. half-close vs.
  timeout mid-frame) leaves the registered fd. The accumulation (33u→35u)
  during a quiescent window suggests even normal client churn leaks slowly.

---

## 7. Verification log (what was independently authenticated this pass)

- [LIVE] `sample 82698` ×2 — 99.9% in `kevent`; spin stable. Saved to `/tmp/daemon-peg-sample-82698.txt`.
- [LIVE] `lsof -p 82698` — 5→7 fds; accepted UDS conns 33u/35u with no peer process.
- [LIVE] `py-spy dump` — refused without root on macOS (documents the bead's "cannot py-spy without root").
- [LIVE] `~/.config/nexus/t2_addr.501` — pid under `endpoint`, absent top-level (authenticates RC-5).
- [LIVE] 66 `nxt2race` daemons, 252 `/tmp/nxt2race-*` dirs; 0 production `nexus.cli` orphans.
- [CODE] `t2_daemon.py:1271-1304` (handler), `:990` (heartbeat direct call), `:1079/1133/1151` (stop ordering), `:1058`/`:980` (loops sleep-yield), `:1274` (except clause).
- [CODE] `service_registry.py:64` (TTL 3.0), `:200` (flock LOCK_EX), `:284` (heartbeat R-M-W), `:328` (mark_shutting_down exists, uncalled).
- [CODE] `commands/daemon.py:1040-1046` (unguarded on lock-timeout), `:1123-1143` (R-M-W), `:1146-1154` (start_new_session).
- [CODE] `db/t2/__init__.py:477` (fresh sqlite connect), `aspect_extraction_queue.py:489-491` (bounded reclaim backoff).

Diagnostic workflow run id: `wf_b48bbf58-8b9`. Full T2 record:
`nexus/daemon-concurrency-diagnosis-2026-06-05`.
