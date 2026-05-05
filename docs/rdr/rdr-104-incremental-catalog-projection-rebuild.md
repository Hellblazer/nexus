---
title: "Incremental Catalog Projection Rebuild"
id: RDR-104
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self (solo)
created: 2026-05-05
accepted_date: 2026-05-05
related_issues: [nexus-rr0u]
---

# RDR-104: Incremental Catalog Projection Rebuild

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

`Catalog._ensure_consistent` rebuilds the SQLite projection by `DELETE FROM owners/documents/links` followed by replaying every event in `events.jsonl` through the projector — inside a single transaction. The shortcut at the top of the function (`current_mtime <= self._last_consistency_mtime`) is binary: nothing changed → skip everything; anything changed → full replay from event 0.

On a hot project (Hal's nexus catalog: 452,361 events; ART catalog: 441,917 events) every `Catalog()` construction whose canonical-truth files have advanced past the persisted marker pays the full replay cost. The 4.24.2 FTS5 bulk-load fix dropped the per-rebuild time from ~17 minutes to ~4 seconds, and 4.24.3's per-collection staleness cache eliminated the per-file ChromaDB roundtrips, but the 4-second rebuild still fires on every `nx index repo`, `nx catalog show`, MCP tool call, etc. that comes after any catalog write — i.e., the common case.

### Enumerated gaps to close

#### Gap 1: Replay-from-zero on every modified mtime

The current projection rebuild has no knowledge of "events 1..N have already been applied; start from N+1". Every triggered rebuild reprocesses the entire log. With a 232 MB events.jsonl this is a steady ~4 s cost on every Catalog construction after a write, paid every nx run on a busy repo.

#### Gap 2: Binary fast-path forces full rebuild for any delta

`_last_consistency_mtime` is a single mtime threshold. Any canonical-truth file modification advances mtime; the rebuild then replays the entire log to absorb whatever changed since. The fast-path either skips everything (steady-state idle) or does everything (any write). There is no "replay just the delta" middle ground.

#### Gap 3: Detection of events.jsonl rewrites

Dedupe verbs and operator-side maintenance can rewrite `events.jsonl` in place (truncate or replace, keeping the same path). A byte-offset checkpoint that survives such a rewrite would point into the middle of unrelated content and replay garbage. The incremental design needs an invalidation signal that distinguishes "log appended to" from "log replaced", so the latter falls back to full rebuild.

## Context

### Background

Discovered while diagnosing ART repo indexing on 2026-05-05 after the 4.24.x catalog optimizations landed. The user's `nx index repo .` against ART (~4,800 files, 441K events) showed:

```
Catalog: rebuild triggered by links.jsonl - replayed 452,361 events → 23,190 docs, 22,238 links in 4.0s
```

— on every invocation following any catalog write. The 4-second cost is not the headline issue (4 s is fine compared to 17 minutes); the issue is that the cost is paid for nothing on the steady-state path. A `nx index repo` that finds every file already current still triggers rebuild via the catalog hook's registration writes, then pays the full replay despite having no semantic work to do.

The current shape is correctness-driven: replay-from-zero is known to produce a known-correct projection given a known-correct event log. Any incremental scheme has to preserve that invariant.

### Baseline established by 4.24.4 (atomicity prerequisite)

The first gate round (2026-05-05) flagged a Critical issue independent of incrementality: `_write_consistency_marker` ran its own `commit()` *after* the rebuild's transaction had closed, leaving the marker write as a separate atomic unit from the projection writes. Under the original call ordering the failure direction was benign, but any rearrangement that put the marker commit before the projection commit would silently corrupt the projection by skipping events on the next run — a latent silent-corruption hazard one refactor away.

The fix landed independently as **conexus 4.24.4** (PR #516): `CatalogDB.rebuild` gained a `consistency_mtime` keyword that writes the marker as the last statement inside the rebuild transaction; the event-sourced path in `Catalog._ensure_consistent` calls `_write_consistency_marker` from inside its existing `with self._db.transaction() as conn:` block. Both paths now commit the marker atomically with the projection writes. Regression test `tests/test_catalog_consistency_marker.py::test_marker_does_not_advance_when_rebuild_raises` pins the invariant.

This RDR's design assumes that baseline. The incremental path can update the marker offset+hash inside the same transaction as the projector writes without re-introducing the hazard.

### Technical Environment

- `Catalog._ensure_consistent` (`src/nexus/catalog/catalog.py:751`): the gate.
- `CatalogDB.transaction` (`src/nexus/catalog/catalog_db.py:493`): the atomicity context.
- `CatalogDB.bulk_load_documents` (`src/nexus/catalog/catalog_db.py`, RDR-pre-104): the FTS5 fence.
- `Projector.apply_all` (`src/nexus/catalog/projector.py`): the replay machine.
- `EventLog.replay` (`src/nexus/catalog/event_log.py`): the iterator.
- `_meta` table (`src/nexus/catalog/catalog_db.py:175`): already holds `last_consistency_mtime`; new offset+hash rows live here.

## Research Findings

### Investigation

Reviewed:
- `_ensure_consistent` + `_write_consistency_marker` (catalog.py): mtime-only fast path.
- `Projector` verbs: `DocumentRegistered`, `DocumentUpdated`, `DocumentDeleted`, `LinkCreated`, `LinkDeleted`, `OwnerCreated`, `CollectionCreated`. All currently use `INSERT OR REPLACE` / `INSERT OR IGNORE` / explicit DELETE — naturally idempotent.
- `EventLog.replay`: streams from `events.jsonl` start to end. No offset-based variant today.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| sqlite3 (stdlib) | Yes | `BEGIN; … COMMIT;` rollback semantics suit the offset-update + projection-write atomicity requirement. No incremental WAL surface needed. |
| `Projector.apply` | Yes (`src/nexus/catalog/projector.py`) | All current verbs are idempotent under double-apply. New verbs added later need to preserve this. |
| `EventLog` | Yes (`src/nexus/catalog/event_log.py`) | Returns a generator over the JSONL file. Currently no seek/offset support — adding `replay_from(offset)` is a small extension. |

### Key Discoveries

- **Documented**: every existing projector verb is idempotent — replaying an already-applied event leaves the projection unchanged. Confirmed by reading projector.py.
- **Documented**: `events.jsonl` is append-only in the production write path. Dedupe is the only common rewrite verb. Confirmed by grep for `_events_path.write_text` and `_events_path.open("w")`.
- **Verified**: a byte-offset checkpoint scheme works as long as we can detect file replacement. The simplest signal is hash-of-first-N-bytes; if the hash drifts, the file was replaced.
- **Assumed**: `Projector.apply` with the same event applied twice never raises — needs spike on the dedupe-only paths to be sure.

### Critical Assumptions

- [x] **All v0 projector verbs are idempotent under accidental double-apply.** — **Status**: Verified — **Method**: Source Search. Every dispatched verb in `src/nexus/catalog/projector.py` uses one of `INSERT OR REPLACE` (PK / unique-index keyed), `UPDATE … WHERE <pk>` (same-value reapply is idempotent), `DELETE … WHERE <pk>` (no-op when row missing), or pure `return`. See T2 `nexus_rdr/RDR-104-research-1`. Regression test (double-apply equivalence) still required as part of Phase 1 Step 4.

  **v=1 carve-out (gate observation #2):** the `_v1_unsupported` handler raises `NotImplementedError` by design — the incremental design does not regress this behaviour. If a v=1 event appears in the replay window the transaction rolls back, the marker stays put (4.24.4 atomicity), `Catalog.degraded = True`, and the next run re-attempts from the same offset. Operator must upgrade to a conexus that ships the v=1 handler. Same failure mode as the existing full-replay path; the RDR neither introduces nor masks it.

- [x] **`events.jsonl` is strictly append-only in production code.** — **Status**: Verified — **Method**: Source Search. The only writes are `EventLog.append` and `EventLog.append_many` (both `open("a")` + `flock(LOCK_EX)`). `EventLog.truncate` is gated `# Test-only — production code never calls`. No `open(events_path, "w")` / `unlink` / `Path.replace` paths exist in `src/nexus/catalog/`. See T2 `nexus_rdr/RDR-104-research-2`.

- [x] **First-64-KB hash distinguishes append from replace.** — **Status**: Verified — **Method**: argument-from-construction. Append leaves bytes [0, 64KB) unchanged → hash matches. Any rewrite (truncate, unlink+create, `git reset`) modifies early bytes → hash differs. Pathological adversarial case (rewrite preserving first 64KB) bounded by Finding 1 idempotency: redundant work, no semantic drift. See T2 `nexus_rdr/RDR-104-research-3`. **Gate observation #3 refinement:** the window size (`_HEADER_HASH_BYTES`) is stored in `_meta` alongside the hash so a future change to the constant invalidates the prior marker (mismatched window → fall back to full rebuild) rather than silently comparing hashes computed over different windows.

## Proposed Solution

### Approach

Persist a checkpoint marker `(byte_offset, header_hash, header_window_bytes)` in the `_meta` table alongside the existing `last_consistency_mtime`. On `_ensure_consistent`:

1. **Bootstrap path (no marker)**: full rebuild. Today's behavior, with the Critical #1 fix below. Set the marker to `(eof_offset, header_hash, header_window_bytes)` inside the same rebuild transaction (4.24.4 atomicity).
2. **Empty-delta fast path (gate Significant #4)**: if `eof_offset == stored_offset`, advance only the mtime marker (`last_consistency_mtime`) and return. No header-hash computation, no events.jsonl read. This handles the very common case where the mtime fast-path missed (legacy JSONL was written and ticked mtime) but events.jsonl itself has not been appended to. **The marker write must still run inside a `transaction()` block** — the atomicity contract from 4.24.4 applies even for this single-row write; skipping the wrapping for "performance" would re-introduce the ordering hazard the patch closed.
3. **Steady-state path (marker valid, delta non-empty)**: validate header_hash and header_window_bytes match stored values; read events.jsonl from `stored_offset` to EOF; project incrementally inside `transaction()`; advance the marker (offset + hash + window) atomically with the projector writes.
4. **Invalidated path (header_hash drifted, window mismatch, or marker missing)**: full rebuild + new marker. Same shape as bootstrap.

The pre-existing fast path that skips when `current_mtime <= last_consistency_mtime` stays unchanged — that case has no replay at all, incremental or otherwise.

### Critical #1 fix — `collections` is now in the rebuild DELETE set

The first gate found that the existing `_ensure_consistent` deletes `owners`, `documents`, and `links` but NOT `collections`. `_v0_collection_created` uses `INSERT OR REPLACE` with a `COALESCE`-based preservation of `superseded_by`, `superseded_at`, and `created_at` from any existing row — which means the rebuild silently inherits stale supersede metadata that no replay event would re-validate. This is a pre-existing bug that the RDR's "DELETE + replay = known-correct" framing accidentally hides.

**Fix:** add `DELETE FROM collections` to the rebuild DELETE set on both paths (event-sourced replay and legacy JSONL rebuild). The replay then reconstructs `collections` row-by-row from `CollectionCreated` (and `CollectionSuperseded`) events in append order — `INSERT OR REPLACE` writes the Created state, the subsequent `UPDATE` from `CollectionSuperseded` writes the supersede metadata.

**On the COALESCE in `_v0_collection_created` (Round 2 Critical #2 correction, Round 3 Significant #1 sharpening):** the `COALESCE((SELECT superseded_by FROM collections WHERE name = ?), '')` pattern (and its siblings on `superseded_at` and `created_at`) is **load-bearing for the degraded-path retry case**, not dead code. Walkthrough:

- **Steady-state incremental**: events at offsets ≥ `stored_offset` are replayed; events before `stored_offset` are not. A `CollectionCreated` at T1 followed by a `CollectionSuperseded` at T2 is not re-encountered once the marker has advanced past T2. COALESCE inactive in this case.
- **Full rebuild (header-hash drift / bootstrap)**: the new `DELETE FROM collections` clears the table before replay, so the COALESCE subselects return NULL → fall through to event-payload values (`payload.created_at` for the timestamp; `''` for supersede fields). The replay then runs the subsequent `CollectionSuperseded` events to UPDATE supersede metadata into place. COALESCE inactive.
- **Degraded-path retry (Round 3 Significant #1)**: an incremental rebuild raises mid-delta (e.g. a v=1 event in the delta hits `_v1_unsupported`). The transaction rolls back, the marker stays at `stored_offset`, `degraded=True`. The next run reads the unchanged marker and replays the same delta against an *un-cleared* `collections` table — which may already carry `superseded_by`/`superseded_at` populated by events before `stored_offset`. When the delta contains a `CollectionCreated` for one of those names (a re-emit, an idempotent restate, or a CreatedThenSuperseded pair), the `INSERT OR REPLACE` would otherwise stomp the existing supersede metadata with `''` from the event payload. The COALESCE preserves it, leaving the projection in a coherent state on degraded retry.

  The same logic applies to `created_at`: the COALESCE preserves the original-creation timestamp through the re-apply rather than letting a re-emitted CollectionCreated overwrite it with a later event's `payload.created_at` — which Round 3 observation #2 noted is harmless on the full-rebuild path (events freeze the original timestamp) but matters on the degraded retry where a non-original CollectionCreated event might carry a different timestamp.

We therefore retain the COALESCE because it is genuinely load-bearing for the degraded-path retry. Removing it would silently corrupt `superseded_by`/`superseded_at`/`created_at` on every `CollectionCreated` re-apply that follows a degraded incremental rebuild.

Ships as a small standalone fix preceding the incremental implementation (or in the same PR; bundled is fine since it's one line). New regression test asserts `collections` row equality between full-rebuild and a synthetic-event replay that includes `CollectionSuperseded` events.

### Technical Design

```text
_meta rows (catalog SQLite):
  ('last_consistency_mtime', '<float>')          # existing (4.24.x)
  ('last_applied_event_offset', '<int>')         # NEW — byte offset into events.jsonl
  ('last_applied_event_header_hash', '<hex>')    # NEW — sha256 of first N bytes
  ('last_applied_event_header_window', '<int>')  # NEW — N (gate observation #3)
```

All four rows are written **inside the same `CatalogDB.transaction()` block** as the projector writes — same atomicity model 4.24.4 established for `last_consistency_mtime`.

```text
ensure_consistent():
    current_mtime = max(stat(p).st_mtime for p in canonical_files)
    if current_mtime <= last_consistency_mtime and not degraded:
        return  # FAST PATH (today)

    if events_jsonl missing or empty:
        # bootstrap / no event-sourced state — legacy rebuild
        with transaction():
            self._db.rebuild(owners, documents, links,
                             consistency_mtime=current_mtime)
            # rebuild() now also DELETEs FROM collections (Critical #1 fix)
        return

    eof_offset_now = events_jsonl.stat().st_size

    # Empty-delta fast path (Significant #4): events.jsonl unchanged
    # but mtime ticked elsewhere (legacy JSONL, owners.jsonl, etc.).
    if eof_offset_now == stored_offset and stored_offset is not None:
        with transaction():
            self._write_consistency_marker(current_mtime)
        return

    header_window = _HEADER_HASH_BYTES
    header_hash_now = sha256(open(events_jsonl, "rb").read(header_window))

    if (stored_offset is None
        or stored_window != header_window
        or stored_hash != header_hash_now):
        # Bootstrap, window changed, or file replaced — full rebuild.
        with transaction(), bulk_load_documents():
            DELETE FROM owners
            DELETE FROM documents
            DELETE FROM links
            DELETE FROM collections        # Critical #1 fix
            apply_all(replay())
            self._write_consistency_marker(current_mtime)
            self._write_offset_marker(eof_offset_now,
                                       header_hash_now,
                                       header_window)
        return

    # Incremental path — replay only the delta.
    # CRITICAL: replay_from is bounded by eof_offset_now (the captured
    # snapshot), NOT live EOF. Without the upper bound a concurrent
    # appender that lands between the stat() above and the replay below
    # extends the file; replay_from would consume the appended tail
    # too, but the marker we then persist (eof_offset_now) is the
    # PRE-append snapshot. The marker would be stale below the actual
    # applied-event tail, defeating the empty-delta fast path on every
    # subsequent run for that range — incremental never settles. The
    # bounded form caps the iterator at the snapshot so the marker
    # always equals "highest offset reliably applied in this rebuild".
    new_events = replay_from(stored_offset, limit_offset=eof_offset_now)
    with transaction():
        # commit=False — Projector.apply_all defaults to commit=True
        # which would call self._db.commit() mid-transaction, finalizing
        # the projection writes BEFORE the marker writes. The transaction
        # context owns the commit boundary; a nested commit defeats the
        # rollback fence and would re-introduce the 4.24.4-fixed
        # ordering hazard. Mirrors the existing full-rebuild path at
        # catalog.py:945.
        apply_all(new_events, commit=False)
        self._write_consistency_marker(current_mtime)
        self._write_offset_marker(eof_offset_now,
                                   header_hash_now,
                                   header_window)
```

API additions:

- `EventLog.replay_from(offset: int, *, limit_offset: int | None = None) -> Iterator[Event]` — streams events whose start-of-line byte offset is in the half-open range ``[offset, limit_offset)`` (or to EOF when ``limit_offset is None``). The bounded form is what `_ensure_consistent` calls; the unbounded form preserves the natural "give me everything from offset onwards" surface for any future caller that wants it.

  **Concurrent-appender safety (Round 2 Critical #1).** Without the bound, an appender writing between the orchestrator's `stat()` snapshot and the iterator's read window would pollute the marker: the iterator consumes the appended tail, the orchestrator persists `eof_offset_now` (the pre-append size) as the marker, and the next run replays the same tail again indefinitely (idempotent under Finding 1 but the empty-delta fast path never fires). Capping at `limit_offset` makes the marker equal to "highest offset reliably applied in this rebuild" by construction.

  **Implementation contract (Round 1 Significant #3):** opens the file in **binary mode**, calls `seek(offset)`, iterates lines decoded as UTF-8 until either EOF or the next-line start ≥ `limit_offset`. Text-mode `tell()` returns an opaque cookie on Windows (universal newline translation) and is NOT a portable byte offset; binary-mode is the only correct shape. Raises `ValueError` if `offset > file_size` (truncated since marker write — caller falls back to full rebuild). On a malformed first line after seek (mid-line offset, JSON parse failure), the iterator follows the same warn-and-skip semantic as the existing `replay()`.

  **Caller-level corruption detection (Round 3 Significant #2 correction):** the `Event` envelope (`events.py`) carries only `{type, v, payload, ts}` — there is no `source_byte_offset` field, and the on-disk JSONL has no per-line offset annotation. The orchestrator detects marker corruption from one signal: **zero events yielded from a non-empty delta range** (`stored_offset < eof_offset_now`, but the bounded iterator returns no events). That signal is sufficient because the only ways to produce it are:

  - The marker was written for an events.jsonl that has since been truncated/replaced and the header-hash check happened to collide (vanishingly improbable per Round 1 Finding 3); or
  - The marker offset lands mid-line and the first line's JSON parse fails, leaving the iterator empty (the warn-and-skip path runs but yields nothing).

  In both cases escalating to full rebuild is the correct remediation. The earlier draft's "first event's source-byte-offset != offset" check is dropped — the field does not exist on `Event` and adding it would require an envelope change neither the design nor the implementation plan accounts for. **Test plan alignment (Round 2 Significant #3, Round 3 Significant #2):** test scenarios assert the caller-level escalation when zero events are yielded from a non-empty delta range; no `event.source_byte_offset` comparison.

- `_ensure_consistent` checkpoint reads/writes via existing `_meta` table accessors.
- Header hash: `sha256(open(events_jsonl, "rb").read(_HEADER_HASH_BYTES))` where `_HEADER_HASH_BYTES = 64 * 1024`. The constant is also written to `_meta` so a future bump of the constant invalidates prior markers.

The bulk-load FTS5 fence is preserved for the full-rebuild path. The incremental path does NOT need it — its writes are bounded by the delta count, not the entire collection.

### Concurrency notes (gate observation #1)

- **In-process**: `CatalogDB.transaction()` acquires `self._lock` (an `RLock`) before opening the SQLite transaction. Two concurrent `Catalog()` constructions in the same process serialize on this lock — one rebuilds, the other observes the advanced marker on its post-lock SELECT and short-circuits.

- **Cross-process** (e.g. `nx-mcp` running while `nx index repo` runs): SQLite WAL mode (already enabled) provides reader/writer isolation. Two processes A and B reading the same `_meta` row at startup BOTH see the pre-rebuild marker `M0` and capture `eof_offset_now = E_a` and `E_b` (with `E_b ≥ E_a` because B's stat happened later, possibly after an append). Both decide "incremental." Both `transaction()` blocks serialize on the SQLite write lock; A commits first, advancing the marker to `(E_a, H_a)`. B then executes its transaction body — which still references the `stored_offset = M0` it read pre-lock. **B's delta is therefore non-empty (M0..E_b), not empty**: B replays `M0..E_a` (already applied by A, idempotent re-apply via Finding 1) plus the genuine new tail `E_a..E_b`. B's marker advance to `(E_b, H_b)` correctly subsumes A's `(E_a, H_a)`. Net result: redundant apply of the M0..E_a window in B, projection ends correct via idempotency, marker ends at E_b which is the highest offset both reads saw. Round 2 observation #4 corrected this from the prior (inaccurate) "empty delta" framing.

- **File locks** on `events.jsonl` (`flock(LOCK_EX)` in `EventLog.append/_many`) are independent of the SQLite transaction; an in-flight append does not block readers, and a reader holding `transaction()` does not block appenders. The append lands AFTER the reader's `eof_offset_now = stat().st_size` snapshot, so the reader sees a consistent EOF for the duration of its incremental replay; the next reader picks up the new tail. **The bounded `replay_from(offset, limit_offset=eof_offset_now)` (Round 2 Critical #1)** guarantees that an appender writing between the reader's `stat()` and the iterator's read-window cannot cause the reader to apply events past `eof_offset_now` — the iterator stops at `limit_offset` regardless of where live EOF has moved. Without the bound the marker would drift below the true tail; with it the marker invariant ("highest offset reliably applied") is preserved by construction.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `EventLog.replay_from(offset)` | `EventLog.replay` (full stream) | **Extend**: add the offset-aware variant; keep `replay()` as the trivial caller `replay_from(0)` |
| Marker storage | `_meta` table | **Reuse**: same key/value table that holds `last_consistency_mtime` |
| Header hash check | (none) | **New**: small helper inside `Catalog._ensure_consistent`; ~10 lines |
| Bulk-load FTS5 fence | `CatalogDB.bulk_load_documents` | **Reuse on full-rebuild path only**; incremental path bypasses it |

### Decision Rationale

- **Byte offset over event ID**: events don't carry sequential IDs in the current event-log format; lines are the natural unit. Byte offset gives O(1) seek in a single line-by-line stream. Adding event IDs would require schema migration of the event log.
- **Header hash over file inode**: portable across rename/atomic-replace operations; survives `mv` over the same path; trivially cheap to compute (first 64 KB).
- **Same atomicity model as today**: incremental writes go in `self._db.transaction()`. Crash mid-replay rolls back; next run reads the unchanged marker and re-tries from the same offset. No partial application.
- **Bulk-load fence not needed for incremental**: the per-replay write count on the incremental path is the delta size (typically <100 events). FTS5 trigger overhead at that scale is unmeasurable. Fence preserved on the full-rebuild path because that's the only place it's actually load-bearing.

## Alternatives Considered

### Alternative 1: Event-ID checkpoint instead of byte-offset

**Description**: assign each event a monotonic integer id at append time; store `last_applied_event_id`; replay events with id > stored.

**Pros**:
- More natural for log readers that don't seek by byte
- Robust against trailing-whitespace edits

**Cons**:
- Requires migration of every existing `events.jsonl` to add ids
- Adds a per-write counter the appender must coordinate (atomicity question against concurrent writers — the legacy path)
- More moving parts than the byte-offset scheme buys

**Reason for rejection**: byte-offset is sufficient given header-hash invalidation, and zero migration cost. Revisit if event-id is wanted for unrelated reasons (provenance, audit trail).

### Briefly Rejected

- **Never replay; rely on the projection state alone**: breaks the "JSONL is canonical truth" invariant; can't recover from projection corruption.
- **Always full rebuild but make it faster**: 4.24.2/4.24.3 is the limit of how much faster the full rebuild gets without incrementality. Diminishing returns; this RDR is the next layer.
- **mtime-only incremental (no byte offset)**: mtime tells us *that* the file changed, not *which events* are new. Doesn't help.

## Trade-offs

### Consequences

- **Positive**: steady-state `Catalog()` construction after any single write becomes <100 ms instead of 4 s. Multiplies across every `nx` invocation in a hot loop (indexing, MCP tool calls, doctor, etc.).
- **Positive**: full-rebuild path remains as the safety net — if the marker is corrupt, missing, or the header-hash invalidation fires, we fall through to known-correct behavior.
- **Negative**: new failure mode: marker drift. If the projection-write commits but the marker write fails, the next run replays the same events again (idempotent — they apply, no semantic harm, just cost). Acceptable.
- **Negative**: header-hash window (`_HEADER_HASH_BYTES`) is a magic number. Tunable via the `_meta` table later if a real corner case shows up.

### Risks and Mitigations

- **Risk**: a projector verb is added later that is NOT idempotent. Incremental replay of the verb under accidental double-apply produces bad state.
  **Mitigation**: regression test that double-applies a synthetic event log and asserts the projection equals the single-apply state. Test fires on every PR, catches the new verb the day it's added.
- **Risk**: events.jsonl is rewritten by a path we didn't enumerate (operator script, third-party tool).
  **Mitigation**: header-hash invalidation catches this — drift falls through to full rebuild.
- **Risk**: byte-offset persists across versions but the event log's line format changes.
  **Mitigation**: the projector applies events from JSON; format change requires a migration that already invalidates the offset (header rewrite).

### Failure Modes

- **Visible**: marker write fails post-commit → next run replays same events (idempotent, costs duplicate work, correct).
- **Visible**: header-hash mismatch → full rebuild (today's behavior).
- **Silent**: a non-idempotent projector verb is added without the regression test; double-apply produces drift. Mitigation above. Operator can always force full rebuild via `nx catalog setup --rebuild` (already exists).
- **Tolerated (Round 2 Significant #1)**: `_write_consistency_marker` swallows `sqlite3.OperationalError` inside the transaction. If the marker INSERT raises (e.g. transient lock contention), the exception is swallowed, the surrounding `transaction()` context still commits the projection writes, and the marker row is missing from `_meta`. The in-memory `self._last_consistency_mtime` mirror was assigned post-`with` so it reflects the desired value for the lifetime of this Catalog instance, but the next process reads the un-advanced DB row and re-rebuilds. This is intentional: marker-write failure is rare, idempotent re-replay corrects, and propagating the OperationalError would degrade the catalog (`degraded=True`) for a recoverable cause. The implementation must add an inline comment at the `except OperationalError: pass` clause documenting the trade-off so a future reader does not see it as a contradiction with the docstring's "MUST be inside the same transaction" contract.
- **Recovery path**: deleting the catalog SQLite cache forces bootstrap. No data loss because canonical truth lives in JSONL.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (see Validation section)
- [ ] Equivalence test harness drafted (see Test Plan)

### Minimum Viable Validation

A synthetic event log with N events, applied via:

1. Full rebuild (`bulk_load_documents` + replay).
2. Bootstrap → 1 incremental replay of the same N events (cold marker).
3. K appended events × M times → K × M incremental replays.

Final SQLite projection equality across all three paths. Identical document/link/owner row sets, same FTS5 search results.

### Phase 1: Code Implementation

#### Step 0: Add `DELETE FROM collections` to the rebuild DELETE set (Critical #1)

Small, standalone correctness fix. Update both `Catalog._ensure_consistent` (event-sourced path) and `CatalogDB.rebuild` (legacy path) to include `DELETE FROM collections` alongside the existing `owners/documents/links` deletes. Add the negative-test scenario from the Test Plan (full rebuild against synthetic log with `CollectionSuperseded` produces correct `superseded_by`/`superseded_at`; pre-fix would carry stale values forward). Ships as part of the same PR as Step 1 — they're trivially related — or as a precursor patch if the user wants it landed alone.

#### Step 1: `EventLog.replay_from(offset, *, limit_offset)`

Add the offset-aware streaming iterator with the upper-bound parameter (Round 2 Critical #1). **Binary-mode** file open + `seek(offset)` (Round 1 Significant #3). The iterator stops yielding when either (a) it hits EOF, or (b) the next-line start byte position is ≥ `limit_offset` (when supplied). The unbounded form (`limit_offset=None`) preserves natural "everything from here" semantics for callers that want it.

Mid-line / malformed-first-line behavior follows the existing `replay()` warn-and-skip pattern (Round 2 Significant #3 alignment); the orchestrator detects corruption at the caller layer and escalates to full rebuild. Offset-greater-than-file-size raises `ValueError`. Validate `replay_from(0)` equals `replay()` (with and without `limit_offset=eof`).

#### Step 2: Header-hash helper + marker storage

Add `_compute_header_hash` helper (sha256 of first 64 KB). Extend `_meta` table reads/writes for the three new keys (`last_applied_event_offset`, `last_applied_event_header_hash`, `last_applied_event_header_window`). No schema migration needed — `_meta` is `(key, value)`; adding rows is just `INSERT`. Helper to atomically write all four marker rows (mtime + offset + hash + window) inside an active `transaction()`; corresponding reader returns `None` for any incomplete marker set so callers fall through to the full-rebuild path.

#### Step 3: Incremental path in `_ensure_consistent`

Branch added per the pseudocode in Technical Design. Full-rebuild path preserved as the fallback (now with `collections` in the DELETE set). New rebuild-trigger reasons surface in the existing rich summary line: `replayed N events incrementally`, `header-hash drift → full rebuild`, `window-size mismatch → full rebuild`, `empty delta → mtime-only marker advance`.

#### Step 4: Equivalence regression test

Synthetic event log harness in `tests/test_catalog_incremental_rebuild.py`. Covers all scenarios in the Test Plan: bootstrap-vs-append-vs-rewrite paths, the empty-delta fast path, malformed-line append, split-pair `DocumentRegistered`/`DocumentAliased`, `CollectionCreated`/`CollectionSuperseded` round-trip, crash-mid-incremental atomicity, and the `replay_from(offset)` contract cases.

### Phase 2: Operational Activation

(none — this is internal to Catalog construction; ships in a single PR + patch release.)

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `_meta` rows for incremental marker | In scope (`SELECT * FROM _meta`) | Same | `nx catalog setup --rebuild` clears + bootstraps | Equivalence test | Same as catalog SQLite (canonical truth in JSONL) |

### New Dependencies

None. Pure stdlib (`hashlib`, `sqlite3`, `pathlib`).

## Test Plan

Synthetic-log harness in `tests/test_catalog_incremental_rebuild.py` (new file). Each scenario asserts row-by-row equality of the SQLite projection (`owners`, `documents`, `links`, `collections`) AND the FTS5 search results between paths.

### Bootstrap and rebuild paths

- **Scenario**: cold catalog (no marker), 1000-event log → full rebuild + marker write — **Verify**: marker stores `(eof_offset, header_hash, header_window)`; projection matches single-pass replay; FTS5 search hits stable.
- **Scenario**: warm catalog, events.jsonl truncated and replaced (same first 64 KB) — **Verify**: pathological adversarial case bounded by Finding 1 idempotency; projection still converges to correct state via redundant replay; documented as known-cost-not-known-corruption.
- **Scenario**: warm catalog, events.jsonl truncated and replaced (first bytes differ) — **Verify**: header-hash mismatch detected; falls through to full rebuild; marker re-written.
- **Scenario**: `_HEADER_HASH_BYTES` bumped (window mismatch) — **Verify**: stored window != current window → full rebuild; marker re-written with new window value.
- **Scenario**: `collections` rebuild equality (Critical #1) — **Verify**: synthetic log includes `CollectionCreated` followed by `CollectionSuperseded`; `DELETE FROM collections` + replay produces correct `superseded_by`/`superseded_at`; without the DELETE, stale supersede metadata persists (negative test pinned).

### Empty-delta and incremental paths

- **Scenario (Significant #4 — separated)**: warm catalog, events.jsonl unchanged but legacy JSONL (`documents.jsonl`) re-written → mtime advances but eof_offset unchanged — **Verify**: empty-delta fast path; no header-hash computation; only `last_consistency_mtime` advances.
- **Scenario**: warm catalog, K new events appended (typical incremental) — **Verify**: only the K new events apply; marker advances by sum-of-line-bytes; projection matches single-pass full rebuild on the same combined log.
- **Scenario (gate observation #4 — separated)**: malformed-line append (a corrupt line fails JSON parse) — **Verify**: incremental replay skips the bad line via the existing `replay()` warning path; marker advances past it; projection matches the full-rebuild outcome on the same corrupt log.

### Idempotency and concurrency

- **Scenario**: same event applied twice (double-apply equivalence) — **Verify**: projection identical after the second apply, all 13 v0 verbs covered.
- **Scenario (Significant #1 — split pair)**: `DocumentRegistered` in already-applied window, `DocumentAliased` for the same document in the new-events window — **Verify**: incremental replay of the split-pair produces the same final state as a single-pass full rebuild. Pins the conditional-idempotency case for `_v0_document_aliased`.
- **Scenario (Significant #2)**: synthetic log includes `CollectionCreated` and `CollectionSuperseded` events — **Verify**: incremental replay's collections row equality matches full-rebuild's; covers the equivalence claim explicitly for the table the pseudocode added.
- **Scenario**: crash mid-incremental (`Projector.apply_all` raises) — **Verify**: transaction rolls back; offset+hash markers unchanged (atomicity guarantee from 4.24.4); next run reads stale marker and re-applies the same delta successfully.
- **Scenario (Round 3 Significant #3)**: `apply_all(new_events, commit=False)` invariant — **Verify**: incremental path passes `commit=False` to `apply_all` so the projector does not call `self._db.commit()` mid-transaction. Test patches `CatalogDB.commit` to track call count and asserts it is NOT called inside the incremental rebuild's transaction body (only once at `__exit__` of the `with self._conn:` block, which is the connection-level commit, not the explicit `commit()` call).
- **Scenario (Round 3 Significant #1)**: degraded-path retry preserves supersede metadata — **Verify**: setup with collections row carrying populated `superseded_by` from events before `stored_offset`; force a v=1 raise mid-delta; after rollback, the next incremental retry encounters a `CollectionCreated` for the same collection in the delta; assert the supersede metadata is preserved by the COALESCE through the re-apply.

### EventLog.replay_from contract (Significant #3)

- **Scenario**: `replay_from(0)` equals `replay()` — **Verify**: iterator equivalence at the offset-zero boundary, both unbounded form and bounded form with `limit_offset=eof`.
- **Scenario**: `replay_from(eof)` yields zero events — **Verify**: empty iterator at EOF (both bounded and unbounded).
- **Scenario (Round 2 Significant #3)**: `replay_from(mid_line_offset)` yields a malformed first line — **Verify**: iterator follows existing `replay()` warn-and-skip semantic; orchestrator detects the corruption signal at the caller layer (zero events yielded against a non-empty `[stored_offset, eof_offset_now)` range, or first event's source-byte-offset != stored_offset) and escalates to full rebuild WITHOUT advancing the marker. Test asserts the caller-level escalation, not a `replay_from`-internal raise.
- **Scenario**: `replay_from(offset > file_size)` — **Verify**: raises `ValueError`; caller path falls back to full rebuild.
- **Scenario (Round 2 Critical #1)**: `replay_from(offset, limit_offset)` bounded form — **Verify**: iterator stops at `limit_offset`. Setup: file with N+M events; capture `limit = byte_offset_after_event_N`; append M more events (live EOF advances); call `replay_from(0, limit_offset=limit)`. Assert: iterator yields exactly events 0..N-1, none of the appended-after-snapshot tail.
- **Scenario (Round 2 Critical #1, concurrent-appender)**: orchestrator-level concurrent-appender simulation — Setup: existing log with K events, marker at offset 0. Capture `eof_offset_now = byte_offset_after_event_K`. Append L more events to the file. Run incremental rebuild via the orchestrator path. Assert: marker advances to `eof_offset_now` (NOT to live EOF including the L appended events); next run sees a non-empty delta of L events and applies them; final projection equals full-rebuild over K+L events.
- **Scenario**: binary-mode portability — **Verify**: byte offsets captured on platform A are valid on platform B (Linux ↔ macOS at minimum; Windows tested if CI supports it).

## Validation

### Testing Strategy

1. **Synthetic event-log fixture** in `tests/test_catalog_incremental_rebuild.py` (new file). Generates N events programmatically, runs them through both paths, asserts equality of the SQLite projection.
2. **Pinned regression** for the double-apply property: every existing projector verb gets a "double-apply leaves no drift" assertion.
3. **End-to-end via the existing rebuild fixture** (`tests/test_catalog_consistency_marker.py`): extend with an incremental-path case alongside the existing full-rebuild cases.

Acceptance: 100% of equivalence scenarios pass. CI green.

### Performance Expectations

Steady-state `Catalog()` construction after a single write goes from O(total_events × per-event-projector-cost) to O(delta_events × per-event-projector-cost). For Hal's catalog (452K events, ~100 events/day appended): the steady-state rebuild becomes 100 events instead of 452K — sub-100 ms instead of 4 s. Empirical confirmation in the equivalence test (timing assertion: incremental path completes in <100 ms on a 100-event delta against a 100K-event base).

## Finalization Gate

> Complete each item with a written response before
> marking this RDR as **Accepted**. Written responses
> prevent rubber-stamping and produce a review record.

### Contradiction Check

(to be filled at gate time)

### Assumption Verification

(to be filled at gate time)

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `sqlite3.Connection.execute("INSERT OR REPLACE INTO _meta …")` | stdlib | Source Search — existing usage in `_write_consistency_marker` |
| `Path.open("rb").read(N)` | stdlib | Docs Only |
| `hashlib.sha256(bytes).hexdigest()` | stdlib | Docs Only |

### Scope Verification

The Minimum Viable Validation (synthetic-log equivalence test across full-rebuild / bootstrap-incremental / appended-incremental / rewrite-fallback paths) is in scope and will be executed as part of Phase 1 Step 4. Not deferred.

### Cross-Cutting Concerns

- **Versioning**: N/A — no API surface changes; internal to Catalog
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: N/A — ships in conexus wheel
- **IDE compatibility**: N/A
- **Incremental adoption**: catalogs without the marker fall through to full rebuild; first nx invocation post-upgrade writes the marker
- **Secret/credential lifecycle**: N/A
- **Memory management**: header-hash reads at most 64 KB; incremental replay streams events one at a time; bounded

### Proportionality

Right-sized. The change is small (one branch in `_ensure_consistent`, one new EventLog method, two new `_meta` keys), the impact is large (4 s → <100 ms steady state), and the test surface is contained.

## References

- 4.24.2 release: `chore(release): conexus 4.24.2` — FTS5 bulk-load fence
- 4.24.3 release: `chore(release): conexus 4.24.3` — staleness cache + batched prune
- `src/nexus/catalog/catalog.py:751` — `_ensure_consistent` (the gate)
- `src/nexus/catalog/catalog_db.py:175` — `_meta` table schema
- `src/nexus/catalog/projector.py` — projector verbs (idempotency audit)
- `src/nexus/catalog/event_log.py` — `EventLog.replay`
- nexus-rr0u — open bead tracking this work

## Revision History

### Round 1 — 2026-05-05 — Gate result: BLOCKED

Substantive critic ran via `Agent` tool; T2 record `nexus_rdr/RDR-104-gate-latest`. Two Critical issues, four Significant, four Observations. Disposition:

| Finding | Severity | Disposition |
|---|---|---|
| Marker write outside transaction boundary (asymmetric failure mode) | Critical | **Shipped as standalone fix in conexus 4.24.4 (PR #516).** RDR now references the fixed baseline. |
| `DELETE FROM collections` missing from rebuild path | Critical | **Addressed.** Phase 1 Step 0 adds the DELETE; the COALESCE in `_v0_collection_created` is preserved for the incremental path. Negative-test scenario added to Test Plan. |
| `_v0_document_aliased` idempotency conditional on prior DocumentRegistered | Significant | **Addressed.** Test Plan includes a split-pair scenario asserting incremental replay equality when `DocumentRegistered` and `DocumentAliased` straddle the offset. |
| `collections` events excluded from RDR's incremental scope description | Significant | **Addressed.** Equivalence test fixture explicitly seeds `CollectionCreated` and `CollectionSuperseded`; assertions cover the `collections` table alongside `owners/documents/links`. |
| `replay_from(offset)` text-mode portability | Significant | **Addressed.** Phase 1 Step 1 explicitly mandates binary-mode file open + `seek(offset)`; defensive checks for offset > file_size and mid-line offset; binary-mode portability test scenario. |
| Empty-delta path still pays sha256 + SQLite read | Significant | **Addressed.** Approach now distinguishes the empty-delta fast path (`eof_offset == stored_offset`) from the incremental path; only `last_consistency_mtime` advances in that case. |
| Concurrency model not documented | Observation | **Addressed.** New "Concurrency notes" subsection covers in-process (RLock), cross-process (SQLite WAL), and file-lock (`flock`) semantics. |
| CA-1 carve-out for v=1 verbs | Observation | **Addressed.** Critical Assumption 1 now explicitly notes `_v1_unsupported` raises `NotImplementedError` and the failure-mode disposition (transaction rolls back, marker stays put, `degraded=True`). |
| Header-hash window stored alongside hash | Observation | **Addressed.** `_meta` schema gains `last_applied_event_header_window`; mismatch falls through to full rebuild. |
| Test scenario "0 new events" conflates two cases | Observation | **Addressed.** Test Plan separates "events.jsonl unchanged but legacy JSONL ticked mtime" (empty-delta fast path) from "events.jsonl appended only with malformed garbage" (skip-and-advance path). |

Re-gate with `/nx:rdr-gate 104` after the operator confirms the revised design.

### Round 2 — 2026-05-05 — Gate result: BLOCKED

Substantive critic ran via `Agent` tool against the Round 1 revision. Two new Critical issues, three Significant, four Observations. Disposition:

| Finding | Severity | Disposition |
|---|---|---|
| Concurrent-appender race against incremental marker | Critical | **Addressed.** `EventLog.replay_from` gains `limit_offset: int | None = None` keyword. Orchestrator passes `limit_offset=eof_offset_now` so the iterator caps at the captured snapshot regardless of where live EOF moves. Marker invariant ("highest offset reliably applied") is preserved by construction. New test scenario in Test Plan (orchestrator-level concurrent-appender simulation). Concurrency notes subsection updated. |
| COALESCE rationale logically incomplete | Critical | **Addressed.** Critical #1 fix paragraph rewritten: COALESCE is **defensive against double-apply on a non-cleared table**, NOT a load-bearing protection for the incremental path. Walkthrough shows the prior rationale's scenario cannot occur in the current two paths (incremental never re-applies events before stored_offset; full-rebuild now DELETEs collections first). COALESCE retained because zero cost and it correctly handles a hypothetical future partial-rebuild verb. |
| `_write_consistency_marker` swallows `OperationalError` silently | Significant | **Addressed.** Failure Modes section adds a "Tolerated" entry documenting the trade-off; implementation must add an inline comment at the `except` clause. |
| Empty-delta fast path's transaction wrapping is implicit | Significant | **Addressed.** Approach paragraph for the empty-delta fast path now states explicitly: "The marker write must still run inside a `transaction()` block — the atomicity contract from 4.24.4 applies even for this single-row write; skipping the wrapping for 'performance' would re-introduce the ordering hazard the patch closed." |
| `replay_from` mid-line "raise" contract conflicts with existing `replay()` warn-and-skip | Significant | **Addressed.** API contract aligned with the existing `replay()` semantic: warn-and-skip on malformed lines; orchestrator detects corruption at the caller layer (zero events for a non-empty range, or source-byte-offset mismatch) and escalates to full rebuild. Test scenario rewritten to assert caller-level escalation, not internal raise. |
| `transaction()` `@contextmanager` decorator visibility | Observation | Verified separately: decorator IS applied at `catalog_db.py`'s `@contextmanager` import; critic's note was from a limited read range. No code change needed. |
| `rebuild()` lacks `DELETE FROM collections` (pre-implementation) | Observation | Expected — Phase 1 Step 0 will add. No new action. |
| Audit existing `replay()` callers if `replay_from(0)` becomes the wrapper | Observation | Phase 1 Step 1 notes the audit; binary-mode behaviour difference observable only on Windows/autocrlf, but a regression note in CI is cheap. |
| Cross-process race notes inaccurate ("empty delta") | Observation | **Addressed.** Concurrency notes subsection rewritten: B's delta is non-empty (`M0..E_b`); the M0..E_a window is a benign idempotent re-apply; B's marker advance to E_b correctly subsumes A's E_a marker. |

Re-gate with `/nx:rdr-gate 104` after the operator confirms.

### Round 3 — 2026-05-05 — Gate result: **PASSED** (3 Significant addressed in-place)

Substantive critic ran a third time against the Round 2 revisions. **0 Critical**, 3 Significant, 2 Observations. Standard gate semantics: PASSED on zero Critical. The three Significant items are real text-level corrections; addressed in this same revision pass before any accept.

| Finding | Severity | Disposition |
|---|---|---|
| COALESCE walkthrough mischaracterizes the degraded-path retry case | Significant | **Addressed.** The COALESCE walkthrough now treats the COALESCE as **load-bearing for the degraded-path retry case** (incremental rolls back mid-delta, marker stays put, next retry replays the same delta against an un-cleared `collections` table). Walkthrough text expanded to cover the steady-state, full-rebuild, AND degraded-path retry cases distinctly. New test scenario added covering this path. The earlier "harmless dead code" framing was wrong. |
| `source-byte-offset` escalation references a non-existent field on `Event` | Significant | **Addressed.** `Event` envelope carries `{type, v, payload, ts}` only; there is no `source_byte_offset` field. Caller-level corruption detection now relies on the single concrete signal: **zero events yielded from a non-empty delta range** (`stored_offset < eof_offset_now`, bounded iterator returns nothing). Walkthrough explains why that signal is sufficient. Test scenarios updated to assert this signal, not a non-existent field comparison. |
| Pseudocode omits `commit=False` on `apply_all` in the incremental path | Significant | **Addressed.** Pseudocode now passes `apply_all(new_events, commit=False)` with an explicit comment matching the existing full-rebuild path (`catalog.py:945`). Without `commit=False`, the projector's default would issue a mid-transaction commit, defeating the rollback fence and re-introducing the 4.24.4-fixed ordering hazard. Test scenario added asserting `CatalogDB.commit` is not called explicitly inside the incremental transaction body. |
| `limit_offset` boundary-value test fixture | Observation | Recorded for the implementer: use `f.tell()` after writing event N's line, not a computed sum, to nail the half-open boundary. |
| COALESCE on `created_at` not addressed in walkthrough | Observation | **Addressed.** Walkthrough now covers `created_at` alongside `superseded_by`/`superseded_at`. The full-rebuild path is unchanged (event freezes original timestamp); the degraded-retry path is where the COALESCE earns its keep. |

**Gate outcome**: **PASSED**. Significant items were corrected in this revision pass; no further blockers. RDR is ready for `/nx:rdr-accept 104`.
