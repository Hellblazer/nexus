---
title: "Aspect-Worker Hosting in the Post-RDR-152 Service Topology: The Extraction Worker Has No Persistent Host, So Short-Lived Store Paths Never Extract"
id: RDR-173
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-28
related_issues: [nexus-tih7j, nexus-575kd, nexus-8zog5]
related_rdrs: [RDR-089, RDR-152, RDR-155, RDR-156, RDR-163, RDR-172, RDR-140, RDR-149]
supersedes: []
related_tests: [tests/e2e/migration-rehearsal/rehearse_fullstack.sh, tests/test_aspect_worker.py]
---

# RDR-173: Aspect-Worker Hosting in the Post-RDR-152 Service Topology

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

In service mode (PG + Java service, RDR-152/155), structured aspect extraction (RDR-089)
silently never completes for any store path that does not keep a single process alive for
the full extraction window. `document_aspects` is enqueued but never populated; nothing
surfaces in logs or any green test.

#### Gap 1: The aspect extraction worker has no persistent host

The aspect **extraction worker** — the loop that claims a row
from `aspect_extraction_queue`, runs `extract_aspects` (a `claude -p` subprocess, ~25s/doc
on haiku), and upserts `document_aspects` — is spawned **lazily by the enqueue hook**. The
*only* caller of `ensure_worker_started()` in the codebase is
`aspect_extraction_enqueue_hook` (`src/nexus/aspect_worker.py:1216`). The worker is therefore
a **daemon thread inside whatever process performed the store**, and it dies when that
process exits. There is no persistent host:

- The T2 SQLite daemon never hosted extraction (it runs `reclaim_stale` only — "aspect_worker.py
  deliberately stopped after we61e", `t2_daemon.py:757`), and in service mode `t2_index_write`
  short-circuits past the daemon entirely (`mcp_infra.py:350-354`).
- The Java service does not (and arguably cannot trivially) host extraction, because
  `extract_aspects` is a Python `claude -p` subprocess, not a Java code path.
- No MCP/CLI startup path spawns a standalone worker.

**Consequence.** Extraction completes only when a *persistent* process — an interactive
Claude Code MCP session — stays alive long enough to claim and extract every enqueued row.
Every short-lived store path strands its aspects:

- one-shot `claude -p` (the `--fullstack` harness; and any scripted/headless MCP use),
- CLI `nx store put` from a shell (process exits immediately),
- fast batch ingest where the indexer exits before the worker drains,
- any deployment where the storing process is not the long-running interactive session.

#### Gap 2: No `reclaim_stale` owner in service mode

A worker killed mid-extraction leaves its claimed row in `in_progress`. In SQLite mode the T2
daemon's `reclaim_stale` loop (nexus-we61e) resets such rows to `pending`. In service mode the
daemon is short-circuited — so it is unclear who, if anyone, runs `reclaim_stale` against the
PG queue. A permanently-`in_progress` row blocks `is_drained()` and the drain-before-migration
gate. (Confirmed a real gap — nobody reclaims — by RF-5.)

## Context

- **Discovered** root-causing the RDR-172 P2.5 `--fullstack` validation. After the
  `claim_batch` contract fix (RDR-172 / nexus-575kd) made service-mode claims work at all,
  the run showed: `enqueued=4, pending=3` (one row claimed mid-extraction) and
  `document_aspects=0` — the worker claimed once, then died with the one-shot `claude -p`
  teardown before draining the rest or finishing extraction. Pre-fix the same run showed
  `pending=4` (the `claim_batch` bug was masking this hosting gap as a flat zero-claims
  failure).
- **Worker contract today** (`aspect_worker.py`): lazy singleton daemon thread; polls every
  ~2s; `claim_batch` from the queue (service-aware via `t2_index_write` → `HttpAspectQueue`);
  `extract_aspects` per row via `claude -p`; `mark_done` (DELETE) on success. The async design
  exists because extraction is ~25s/doc (RDR-089 P1.3 spike) and must not block ingest.
- **`nx aspects drain`** (`commands/aspects.py`) is **local-sqlite only**: `drain_worker`
  opens `T2Database(default_db_path())` directly and reads the SQLite queue — it does not
  drain the service-backed PG queue, so it is inert in service mode.
- This is the last open child blocking the **RDR-172 epic** close (P2.5 / nexus-8zog5).

## Research Findings

Findings already established while root-causing nexus-tih7j (to be re-verified/expanded
during rdr-research).

### RF-1 (Verified): the worker has exactly one spawn site — the enqueue hook
`grep -rn ensure_worker_started src/nexus/` returns a single non-definition caller:
`aspect_worker.py:1216`, inside `aspect_extraction_enqueue_hook`. No persistent process
spawns the worker. Method: source search (2026-06-28).

### RF-2 (Verified): the worker dies with its host process
The worker is a `daemon=True` thread; daemon threads are killed at process exit without a
join. A one-shot `claude -p` tears down nx-mcp on completion → the worker dies. Confirmed
empirically by the `--fullstack` run (1 of 4 claimed, 0 extracted, process exited).

### RF-3 (Verified): the local-mode T2 daemon does not host extraction either
`t2_daemon.py:757` documents that the per-worker reclaim was moved to the daemon but the
daemon does not run the extraction loop. In service mode `t2_index_write` short-circuits the
daemon (`mcp_infra.py:350-354`), so even reclaim ownership in service mode is unestablished.

### RF-4 (Verified by construction): a persistent host DOES drain; the gap is short-lived paths
The worker is a `daemon=True` thread (`aspect_worker.py:233-238`) with **no idle-stop or
self-stop** — the poll loop (`aspect_worker.py:321-373`) runs until `stop()` is called or the
host process dies (empty queue → `_stop_event.wait(poll_interval)` → loop). So it drains **iff
its host process lives at least as long as all pending extractions (~25s/doc) plus claim
latency**. A persistent interactive MCP session therefore **does** drain; the failure is
scoped to **short-lived store paths** (CLI `nx store put`, one-shot `claude -p`, fast batch),
**not** all service-mode paths. Method: source search (2026-06-28). An empirical container
spike would confirm end-to-end but the mechanism is settled.

### RF-5 (Verified): nobody reclaims `in_progress` rows in service mode — they strand permanently
No process resets a stranded `in_progress` row to `pending` in service mode: (a) `t2_index_write`
short-circuits to a transient `T2Database`, the daemon is never probed (`mcp_infra.py:342-354`);
(b) the SQLite T2 daemon is **not started** in service mode, so its `_reclaim_stale_loop`
(`t2_daemon.py:1224-1292`) never fires; (c) the Java service's only scheduled task is the T1
scratch TTL sweep (`NexusService.java:264-281`) — **no scheduled `reclaimStale`**; (d) the
`reclaimStale` SQL (`AspectRepository.java:894-904`) + endpoint `POST /queue/reclaim_stale`
(`AspectHandler.java:544-551`) exist but are **never called**; (e) the worker poll explicitly
does not reclaim (nexus-we61e). **Net:** a worker-process death strands the row permanently;
`is_drained()` (`http_aspect_queue.py:241-244`) stays false → the drain-before-migration gate
is permanently blocked. Method: source search (2026-06-28).

### RF-6 (Verified): Java-hosted extraction is infeasible
The Java service spawns no Python/Claude subprocess: the only `ProcessBuilder` is
`LocalChromaServer.java:86` (the `chroma` binary). `extract_aspects` is fundamentally a
`claude -p` subprocess, so the extraction loop cannot live inside the Java service. **Decision-
space Candidate C (Java-hosted orchestration) is refuted.** Method: source search (2026-06-28).

### RF-7 (Verified): the service-aware drain is a narrow fix; reclaim must be closed regardless
`drain_worker` opens a **local SQLite** `AspectExtractionQueue(queue_path)` directly
(`aspect_worker.py:964-967`) and polls `is_drained()` on it — spuriously "drained" while the PG
queue has rows. The fix is one branch: when `storage_backend_for("aspect_queue") == SERVICE`,
use `HttpAspectQueue()` for the drain poll (the worker-stop side and the claim path already
route through `t2_index_write` → `HttpAspectQueue.claim_batch`). The RDR-149 leased registry
(`service_registry.py`) is reusable as the host substrate for a leased Python worker daemon
(Candidate A), mirroring the T2 daemon. Method: source search (2026-06-28).

### RF-8 (Verified): a worker killed by SIGKILL orphans its `claude -p` and strands the row
`extract_aspects` calls `subprocess.run(["claude","-p",...], timeout=180)`
(`aspect_extractor.py:1531-1537` single; `1365-1371` batch) with **no process-group isolation**
(no `start_new_session`/`setsid`). On host **SIGTERM** the child shares the process group, gets
the signal, terminates (row stays `in_progress`). On host **SIGKILL** the child is **orphaned**
(reparented to init), keeps running and consuming API quota, with the row stuck `in_progress`
and no reclaimer (RF-5). Mitigation (separate hardening): `start_new_session=True` +
process-group kill on `TimeoutExpired`. Method: source search (2026-06-28).

## Decision

**Chosen: a leased aspect-worker daemon** (Hal, 2026-06-28). Host the extraction loop in a
long-running Python process supervised by the **existing** RDR-149 leased service-registry
substrate (`service_registry.py`) — the same lease / heartbeat / single-flight discipline that
already governs the T1/T2/T3 daemons. This is *not* a new bespoke daemon class; it is one more
leased tier on the unified substrate (RF-7 confirmed the registry is reusable). That framing is
what reconciles the choice with RDR-152's "one strict service, no extra daemons" intent: the
aspect worker joins the **existing** lifecycle substrate rather than a hand-rolled supervisor
(the bug class RDR-149 unified). Extraction stays Python because it must (`claude -p`, RF-6) —
the Java service cannot host it.

The daemon owns, independent of any storing process:
1. **the extraction loop** — claim from the service queue (`HttpAspectQueue.claim_batch`),
   `extract_aspects` (`claude -p`), upsert `document_aspects`, `mark_done`;
2. **the `reclaim_stale` loop** — the reclaim owner that closes RF-5 (a stranded `in_progress`
   row is reset to `pending` on the daemon's interval, so a worker death no longer permanently
   blocks the migration gate).

The enqueue hook stops spawning an in-process daemon thread; instead it **ensures the leased
daemon is up** (spawn-if-absent via the registry's single-flight election), exactly as the
other tiers are discovered/spawned. Extraction therefore completes for **every** store path —
short-lived CLI / one-shot / batch included — because it no longer depends on the storing
process's lifetime (RF-4).

### Credential context (load-bearing — new vs the T2/T3 daemons)

Unlike the T1/T2/T3 daemons (which manage SQLite/ChromaDB and need **no** LLM credentials), the
aspect-worker daemon issues `claude -p` (`extract_aspects`) — it needs the `claude` binary on
`PATH`, the claude config dir (`~/.claude`), and the Anthropic credential context. The
substrate gives no precedent here. **Decision:** the daemon is spawned **as a child of the
process that first triggers the enqueue** (the MCP server / CLI store process via the
single-flight election), so it **inherits that process's environment** — exactly the context
those store paths already use to run `claude -p`. This makes credential flow automatic for the
normal paths and is the only spawn model permitted: a daemon started from a credential-bare
context (e.g. a hypothetical `nx daemon aspect start` with no inherited env) MUST NOT be the
spawn path unless credentials are explicitly plumbed. Stated as a hard prerequisite so the
spawn entrypoint is designed for inherited-env, not discovered after auth failures.

### Why not the alternatives
- **B / D (in-process + drain trigger)** — leaves the worker tied to the storing process's
  lifetime; short-lived paths still depend on *someone* triggering a drain, and the reclaim gap
  (RF-5) is unaddressed. A only needs the lease the substrate already provides.
- **C (Java-hosted)** — refuted (RF-6): no Python/Claude subprocess path in the service.

### Carried regardless (RF-5, RF-7), now folded into A
- the daemon's `reclaim_stale` loop is the reclaim owner (closes RF-5);
- **service-aware `drain_worker`** (the one-branch fix, RF-7) still ships so the migration gate
  and operators have a working drain against the service queue. Open: with the daemon as the
  reclaim owner, decide whether a *second* scheduled `reclaimStale` in the Java service is
  redundant (likely yes — the daemon suffices; revisit at planning).

## Approach

> Candidate phasing for the leased aspect-worker daemon; refine + lock the bead plan at accept.

1. **Lease the worker on the existing registry.** Register an aspect-worker tier on
   `service_registry.py` (lease key, heartbeat, single-flight election) mirroring the T2/T3
   daemon registration. Define the lease scope (per-host vs per-tenant — see Open Questions) and
   the spawn entrypoint (a small `nx`-internal daemon process hosting `AspectExtractionWorker`).
2. **Spawn-if-absent from the enqueue hook.** Replace the in-process `ensure_worker_started()`
   thread spawn with a registry discover/spawn: the hook ensures the leased daemon is running
   (election guarantees one), then returns. No extraction work happens in the storing process.
3. **Reclaim loop in the daemon.** Run `reclaim_stale` against the service queue on an interval
   (matching the old `stale_timeout_seconds` window) so a daemon/worker death self-heals
   (closes RF-5). Decide whether the Java-side scheduled `reclaimStale` is then redundant.
4. **Service-aware `drain_worker`** (RF-7): branch at `aspect_worker.py:964-967` to drive +
   poll the SERVICE queue (`HttpAspectQueue.is_drained()`) when the aspect-queue backend is
   SERVICE, so `nx aspects drain` and the migration gate work.
5. **Observability** (per the failure-mode requirement): emit a structured signal/metric when a
   process enqueues but the leased daemon is unreachable, and when the daemon resets a stranded
   row — make the previously-silent store-time failure loud.
6. **(Decide) SIGKILL-orphan hardening** (RF-8): `start_new_session=True` + process-group kill
   on `TimeoutExpired` in `extract_aspects` — ship here or as a separate fix (Open Question).
7. **`--fullstack` validation**: with the daemon hosting extraction, the harness's one-shot
   `claude -p` workload must reach `document_aspects>0` (closes RDR-172 P2.5 / nexus-8zog5),
   proving the storing-process lifetime no longer gates extraction.

## Failure mode / observability (today, broken)

The current failure is **silent where it matters and loud where it confuses** — the inverse
of a good signal:

- **At store time: silent.** The enqueue succeeds (RDR-172 + the tripwire are green); a
  short-lived storing process returns success and exits, the daemon worker is killed, and
  `document_aspects` silently never populates. No error, no warning to the user. This is the
  silent-loss class, one stage downstream of RDR-172.
- **The only *loud* signal was the now-fixed `claim_batch` bug** (a warning every ~2s poll —
  log spam, not user-facing, gone with nexus-575kd).
- **Deferred-loud and disconnected:** a worker killed mid-extraction strands a row in
  `in_progress` → `is_drained()` is false → the drain-before-migration gate fails loudly at
  `nx upgrade`/migration time, far from the cause and hard to diagnose.
- **Possible secondary mess (verify):** a worker killed mid-`extract_aspects` may orphan its
  `claude -p` subprocess; whether these accumulate needs a spike.

**Design input:** any chosen host MUST make the store-time failure observable (a metric /
warning when a process exits with undrained owned rows) and MUST establish reclaim ownership
so a stranded row cannot silently block the migration gate. "Loud at store time, not loud at
migration time" is the target — the opposite of today.

## Consequences

- Closes the last gap blocking the RDR-172 epic (P2.5).
- Restores RDR-089 aspect extraction for all service-mode store paths (currently inert for
  short-lived ones — a silent product regression introduced by the RDR-152 topology shift).
- **RDR-152 Phase 4 (`nexus-gmiaf.24`) must carve out `service_registry.py`.** That bead's
  decommission scope is "delete `src/nexus/daemon/` (... `service_registry`, ...)". The
  aspect-worker daemon is a **continuing consumer** of the leased registry, so Phase 4 must NOT
  delete `service_registry.py` — either retain it (relocated out of `daemon/` if the rest of
  the directory goes) or document it as the surviving leased-registry substrate. This is a
  forward pointer that resolves the apparent contradiction, not a re-opening of RDR-152's
  decision. Adds a cross-RDR dependency: RDR-173 must land (or its registry carve-out be
  agreed) before RDR-152 Phase 4 executes.
- The daemon's `reclaim_stale` loop is a **net improvement over today** even for the SIGKILL
  case (RF-8): an orphaned `claude -p` may burn quota, but the stranded `in_progress` row is
  eventually reset to `pending` and re-extracted — whereas today, in service mode, nothing
  reclaims it (RF-5) and it blocks the migration gate forever.
- Adds one leased tier to the existing substrate (not a new lifecycle class); the cost is the
  registry's lease/heartbeat overhead, already paid by T1/T2/T3.

## Alternatives Considered

- **Do nothing / rely on interactive sessions only** — rejected as the default: it silently
  drops aspects for CLI, headless, scripted, and batch paths, which is the present (broken)
  behavior.
- **Block ingest on inline extraction** — rejected by RDR-089 (extraction is ~25s/doc; inline
  would stall the store path).

## Open Questions

- ~~RF-4: does a persistent service-mode MCP session drain end-to-end?~~ Settled by
  construction (RF-4); an empirical container spike remains optional confirmation.
- ~~RF-5: who owns `reclaim_stale` in service mode?~~ Answered: **nobody** (RF-5) — must be
  closed regardless of the hosting model.
- ~~Hosting model choice (A vs B vs D)~~ **Decided: A** (leased aspect-worker daemon on the
  existing RDR-149 registry).
- **Lease scope** for the aspect-worker daemon: per-host single daemon vs per-tenant.
  **Constrained, not free (RDR-152):** a per-host daemon claiming across all tenants would have
  to query without the `nexus.tenant` GUC set — which under RDR-152's RLS safe-default returns
  zero rows — or hold `BYPASSRLS`, which RDR-152 **prohibits** for the service role. So
  **per-tenant scope (one daemon per active tenant, each running with its tenant's GUC) is the
  only RLS-compatible answer** and is the default planning assumption. (For v1's single default
  tenant the practical difference is nil, but the constraint governs every later tenant.)
- **claude -p fan-out cap (multi-tenant):** N active tenants × M docs ⇒ up to N×M concurrent
  `claude -p` subprocesses. A per-tenant and/or global concurrency cap should be set at
  planning (irrelevant for v1's one tenant). (planning)
- With the daemon owning `reclaim_stale`, is a *second* Java-side scheduled `reclaimStale`
  redundant? (likely yes — planning)
- Does the leased daemon replace the in-process worker in **local** mode too, or service-mode
  only? (unifying on the daemon is cleaner; weigh against local-mode simplicity — planning)
- Is fast batch ingest (`nx index`) also affected, and does it need the same host?
- Should the SIGKILL-orphan hardening (RF-8: `start_new_session` + group-kill) ship with this
  RDR or as a separate fix?

## History

- 2026-06-28: Draft. Root-caused from the RDR-172 P2.5 `--fullstack` failure: after the
  `claim_batch` contract fix (nexus-575kd) unmasked it, the run showed the worker claims but
  cannot drain before a short-lived process exits. Confirmed the worker has exactly one spawn
  site (the enqueue hook) and no persistent host. Filed as nexus-tih7j; escalated to RDR.
- 2026-06-28: Research pass 1 (source analysis). **RF-4 settled by construction** — the worker
  is a daemon thread with no idle-stop, so a persistent host drains; the gap is short-lived
  paths only. **RF-5 VERIFIED as a gap** — nobody reclaims `in_progress` rows in service mode
  (daemon not started; Java has no scheduled reclaim; endpoint never called), so a stranded row
  blocks the migration gate permanently. **RF-6** — Java-hosted extraction infeasible
  (Candidate C refuted). **RF-7** — service-aware drain is a one-branch fix; the leased registry
  is reusable for a Python worker daemon. **RF-8** — SIGKILL orphans the `claude -p` and strands
  the row (no process group). Two fixes are needed regardless of the hosting model: service-mode
  reclaim ownership + a service-aware drain. Evidence: T2 `nexus_rdr/173-research-1`.
- 2026-06-28: **Decision locked — Candidate A** (Hal): a leased aspect-worker daemon on the
  existing RDR-149 service-registry substrate, owning the extraction loop + a reclaim_stale loop,
  spawned-if-absent by the enqueue hook. Decouples extraction from the storing process (closes
  the short-lived-path gap, RF-4) and gives reclaim an owner (RF-5). C refuted (RF-6); B/D
  rejected (still process-lifetime-bound). Carried: service-aware drain (RF-7), observability,
  optional SIGKILL-orphan hardening (RF-8). Approach phased; ready for the gate.
- 2026-06-28: Gate Layer-3 fixes (1 Critical + 2 Significant from the substantive-critic).
  CRITICAL — added the credential-context decision (the daemon is spawned as a child of the
  enqueue-triggering process and inherits its `claude -p` credentials; credential-bare spawn
  paths are forbidden unless plumbed). SIGNIFICANT-1 — added the RDR-152 Phase-4
  (`nexus-gmiaf.24`) carve-out for `service_registry.py` (continuing consumer) to Consequences.
  SIGNIFICANT-2 — lease scope is RLS-constrained (per-host needs BYPASSRLS, prohibited by
  RDR-152) → per-tenant is the only compatible scope, now the default planning assumption. Plus
  the reclaim-improves-on-SIGKILL note and a fan-out concurrency-cap open question.
