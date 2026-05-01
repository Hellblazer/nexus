# RDR-101 Phase 3 — sandbox e2e migration validation report

Run date: 2026-05-01
Bead: `nexus-o6aa.9.10`
Harness: `scripts/validate/rdr-101-migration-e2e.sh`
Sandbox: 3-document catalog under `NEXUS_CONFIG_DIR=$(mktemp -d)` + embedded local Chroma via `NX_LOCAL=1`.

## Result

**18 of 18 assertions pass.** No defects surface that block PR D (operator-polish migration verb + docs + TTY prompt).

## What the harness exercises

| Step | Scenario | Outcome |
|---|---|---|
| 1 | Sandbox + `nx catalog init` | catalog dir created |
| 2 | Bootstrap legacy state under `NEXUS_EVENT_SOURCED=0` (index 2 docs) | `documents.jsonl` populated, `events.jsonl` empty |
| 3a | Doctor with empty `events.jsonl` (ES default-on) | synthesizer path PASSes; **no bootstrap-fallback fires** — correct, this is a freshly-upgraded catalog with no ES mutations yet |
| 3b | One ES mutation under default-on → sparse `events.jsonl` (1 line) vs `documents.jsonl` (3 lines) | bootstrap-fallback warning fires with `synthesize-log --force` remediation hint; doctor exits non-zero |
| 4 | `nx catalog synthesize-log --force` | `events.jsonl` populated to 4 lines |
| 5 | Doctor post-migration | PASS, no bootstrap-fallback warning |
| 6 | Mutate post-migration (index 1 more doc) | doctor still PASS |
| 7 | Rollback: `NEXUS_EVENT_SOURCED=0 nx catalog list` | rows present, legacy mode operative |
| 8 | Parallel doctor + index | both succeed; `events.jsonl` JSON-clean (no torn writes) |
| 9 | Performance: doctor wall time | median 0.34s on the sandbox-sized catalog |

## Findings

### Semantic clarification (bead description was sloppy)

The original bead description said step 3 should expect bootstrap-fallback to fire on empty `events.jsonl` after the ζ default flip. **That was wrong.** `_ensure_consistent`'s logic is:

```python
use_event_log = (
    self._event_sourced_enabled
    and self._events_path.exists()
    and self._events_path.stat().st_size > 0
)
if use_event_log and not self._event_log_covers_legacy():
    # bootstrap-fallback path
```

When `events.jsonl` is empty or absent, `use_event_log = False` at the size gate before `_event_log_covers_legacy` ever runs. The catalog falls through to legacy-rebuild silently — and that is correct: a freshly-upgraded catalog with no ES mutations yet is not in a desync state, it's just running on legacy reads.

Bootstrap-fallback only fires when the operator has done **at least one ES-mode mutation** that produced an event in `events.jsonl`, while `documents.jsonl` still has materially more rows than the event log carries `DocumentRegistered` events. The harness now exercises that specific state by triggering one ES-mode index call before the doctor check.

### Doctor synthesizer path is the right behaviour for empty event log

`doctor --replay-equality` with empty `events.jsonl` synthesizes events on the fly from the legacy JSONL and compares to live SQLite. PASS in this configuration is correct — the catalog is consistent with itself, just not yet on the ES write path. PR D's `nx catalog migrate` verb should not block on this state; instead it should be informational ("nothing to migrate").

### `_check_bootstrap_status` performance is fine

3 doctor runs at 0.342–0.365s, median 0.344s. Pure file inspection adds no measurable overhead on the 3-doc sandbox; the 0.34s baseline is dominated by Python interpreter startup + module import. On a large catalog the file-walk would scale linearly with `documents.jsonl` line count, but that's the same scale `_event_log_covers_legacy` itself walks — no new asymptote.

### Concurrent writes do not corrupt `events.jsonl`

Parallel `nx catalog doctor` + `nx index md` both succeed; every line in `events.jsonl` parses as JSON. The flock acquired by `apply_remove_plan` (PR #449) and the per-call flock in `EventLog.append` work as designed.

### Rollback works

`NEXUS_EVENT_SOURCED=0 nx catalog list` returns all sandbox docs. Legacy JSONL stays canonical when explicitly opted-out of ES.

## Implications for PR D

The harness validates that the surfaced state, the remediation, and the rollback all work end-to-end. PR D can proceed with these refinements:

1. **`nx catalog migrate` verb** should pre-check `bootstrap_fallback_active`. If false (catalog is fully migrated OR has no ES events yet AND no sparse condition), report "nothing to do" and exit 0 without running `synthesize-log` (which would unnecessarily regenerate the event log).
2. **Migration doc** should explain the empty-events-jsonl synthesizer-PASS case so operators don't think the absence of bootstrap-fallback warning means migration is already complete — they may still benefit from running `synthesize-log` to populate `events.jsonl` proactively.
3. **TTY prompt** logic stays as designed — fires on `bootstrap_fallback_active = True`, suppresses otherwise. No change.

## Rerun the harness

```bash
./scripts/validate/rdr-101-migration-e2e.sh
```

Self-cleaning sandbox (`/tmp/nexus-rdr101-e2e-XXXXXX`) on success; preserved on failure with transcript at `$SANDBOX/.cache/transcript.log`.

---

## Extended validation (nexus-o6aa.9.11)

Two of the four marketplace-grade gaps from the original assessment have been closed. The remaining two (real Chroma Cloud T3 backfill, MCP smoke test) are filed as separate beads pending credentials / harness scaffolding.

### Scaled soak — `scripts/validate/rdr-101-migration-e2e-scaled.sh`

Heavyweight bash harness running the same walk at N=200 docs (configurable via `N=...` env). 9/9 pass at N=200. Wall-clock observations:

| Stage | N=200 wall | Per-doc |
|---|---|---|
| Doc generation | 343ms | ~2ms |
| Legacy index | 4m 26s | 1.3s |
| Doctor (synthesizer path, empty events) | 378ms | flat |
| `synthesize-log --force` | 365ms | 1ms |
| Doctor (post-migration replay) | 377ms | flat |

Python interpreter startup dominates the per-`nx` cost; actual catalog work is sub-second even at 200 docs. The doctor wall-clock is **flat** from N=3 to N=200 (344ms → 378ms), confirming that `_check_bootstrap_status` and `_event_log_covers_legacy` scale linearly with no hidden quadratic.

### Partial-failure injection — `tests/test_catalog_t3_backfill_partial_failure.py`

Two tests cover the recovery story documented in this guide:

* `test_first_update_per_collection_fails_then_recovers` — fault injector raises `RuntimeError("simulated 503")` on the first `col.update` call. Verb exits 1 (errors reported in JSON), zero chunks updated, original chunks unchanged. Re-running without the injector recovers cleanly: exit 0, all chunks now carry `doc_id`.
* `test_partial_completion_some_collections_succeed` — fault trips on the first update across all collections. One collection's batch lands cleanly, the other fails. Re-run flags the previously-good chunk as `chunks_already_correct` (idempotency) and updates the failed one.

Both pass. The recovery claim ("Re-run `nx catalog t3-backfill-doc-id`. Idempotent — already-backfilled chunks are no-ops.") is verified end-to-end.

### Still open (filed for follow-up)

* **Real Chroma Cloud T3 backfill** — needs throwaway tenant credentials. Separate bead.
* **MCP smoke test** — needs MCP server harness scaffolding. Separate bead.
