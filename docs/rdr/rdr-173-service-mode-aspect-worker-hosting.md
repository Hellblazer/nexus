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

The root is a hosting gap. The aspect **extraction worker** — the loop that claims a row
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

**Secondary gap (reclaim ownership).** A worker killed mid-extraction leaves its claimed row
in `in_progress`. In SQLite mode the T2 daemon's `reclaim_stale` loop (nexus-we61e) resets
such rows to `pending`. In service mode the daemon is short-circuited — so it is unclear who,
if anyone, runs `reclaim_stale` against the PG queue. A permanently-`in_progress` row blocks
`is_drained()` and the drain-before-migration gate.

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

### RF-4 (Assumed → verify): a persistent MCP session DOES drain
The hypothesis that an interactive Claude Code session (long-lived nx-mcp) drains the queue
because the worker lives for the session is plausible but **not yet empirically confirmed**.
A spike (persistent service-mode MCP → store_put → wait → assert `document_aspects>0`) should
verify this before any design assumes it.

### RF-5 (Open): reclaim ownership in service mode
Who runs `reclaim_stale` against the PG queue in service mode is unestablished. If no one
does, stranded `in_progress` rows are permanent and block `is_drained()`.

## Decision

> To be locked at gate. The decision space (candidate hosting models) below is the
> brainstorming surface, not a locked choice.

**Candidate A — persistent Python worker daemon (service-mode).** A long-running supervised
Python process that hosts the extraction worker and a `reclaim_stale` loop, independent of
any storing process. Mirrors the RDR-149 leased-registry / RDR-140 single-flight supervision
patterns. Pro: extraction is already Python (`claude -p`); decouples from MCP/CLI lifetime.
Con: a new always-on daemon in the post-RDR-152 "one strict service" world (re-introduces a
lifecycle class RDR-152 worked to retire).

**Candidate B — service-aware `nx aspects drain` + explicit post-ingest drain.** Make
`drain_worker`/`nx aspects drain` service-aware (drain the PG queue, not local sqlite), and
have ingest paths (or a periodic trigger) call it. Pro: no always-on daemon; reuses the
worker logic. Con: someone must call it; interactive MCP sessions still rely on the in-process
worker; batch/CLI must opt in.

**Candidate C — Java-service-hosted orchestration.** The Java service owns the
claim/reclaim/scheduling and shells out to a Python extraction sidecar. Pro: one host, aligned
with RDR-152. Con: extraction is `claude -p` (auth, subprocess) — awkward to host from Java;
large surface.

**Candidate D — accept in-session-only + make the gap loud.** Keep the in-process worker as
the only host, but (a) document that short-lived paths do not extract, (b) fail loud (a
metric/warning) when a process exits with undrained owned rows, (c) provide a manual
service-aware drain. Pro: minimal. Con: leaves the user-visible symptom (no aspects for
CLI/headless/batch) unfixed.

## Approach

> Candidate; refine in rdr-research, lock at gate.

1. **Spike RF-4 first**: confirm whether a persistent service-mode MCP session actually drains
   (store_put → wait → `document_aspects>0`). This decides whether the gap is "all short-lived
   paths" or "all service-mode paths".
2. **Resolve RF-5**: establish reclaim ownership in service mode (does anything reset stranded
   `in_progress`?); decide where it lives.
3. **Choose a hosting model** (A–D) against the evidence; bias to the smallest change that
   makes extraction complete for the real store paths without re-introducing a daemon-lifecycle
   bug class.
4. **Service-aware drain** regardless of model: `nx aspects drain` must operate on the PG queue
   (the local-sqlite `drain_worker` is inert in service mode) so the migration drain gate and
   operators have a working tool.
5. **`--fullstack` validation**: the harness must be able to drive the chosen host to a real
   `document_aspects>0` (closes RDR-172 P2.5 / nexus-8zog5).

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
- Depending on the model, may add or avoid a persistent process; the choice must weigh the
  RDR-152 "retire the daemon lifecycle class" intent against the need for a drain host.

## Alternatives Considered

- **Do nothing / rely on interactive sessions only** — rejected as the default: it silently
  drops aspects for CLI, headless, scripted, and batch paths, which is the present (broken)
  behavior.
- **Block ingest on inline extraction** — rejected by RDR-089 (extraction is ~25s/doc; inline
  would stall the store path).

## Open Questions

- RF-4: does a persistent service-mode MCP session drain end-to-end? (spike)
- RF-5: who owns `reclaim_stale` in service mode?
- Does the RDR-152 "one strict service, no extra daemons" principle permit a Python worker
  daemon, or must hosting route through the Java service?
- Is fast batch ingest (`nx index`) also affected, and does it need the same host?

## History

- 2026-06-28: Draft. Root-caused from the RDR-172 P2.5 `--fullstack` failure: after the
  `claim_batch` contract fix (nexus-575kd) unmasked it, the run showed the worker claims but
  cannot drain before a short-lived process exits. Confirmed the worker has exactly one spawn
  site (the enqueue hook) and no persistent host. Filed as nexus-tih7j; escalated to RDR.
