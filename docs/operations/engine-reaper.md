# Engine reaper runbook (RDR-192 Step 9)

The engine runs a pass once an hour that moves ownerless T3 chunks into a quarantine collection, tags them, and deletes the ones it moved when they are 14 days old. This page is for the person who runs an engine: what a pass does, how to see it, what the refusals mean, and what to expect at the first big cleanup. Bead nexus-2x9xa.

## What a pass does

A pass visits the default tenant and every tenant that has a row in `service_tokens`. For each tenant it:

1. Refuses the whole tenant unless the `rdr192-manifest-backfill` rung is recorded (`nx upgrade` writes it). Until then the reaper touches nothing for that tenant.
2. Expires that tenant's quarantine first, and only the part the reaper itself filled. Every chunk the reaper moves carries the metadata tag `quarantined_by: engine-reaper`. For each `quarantine-<collection>`, `reaper_expire_quarantine` deletes the tagged chunks stamped more than `NX_REAPER_QUARANTINE_RETENTION_DAYS` (14) days ago, at most 5,000 per origin per pass. The origin of a chunk is read from the chunk's own `origin_collection` tag, and it must be a registered collection. It never deletes a chunk that a manifest row in the origin still names. There is no fraction floor on expiry (see "Expiry").
3. Visits each other collection (all four prefixes: `knowledge__`, `docs__`, `code__`, `rdr__`) that is registered and `live`, and moves at most 300 chunks that `nexus.chunk_is_reapable` selects into `quarantine-<collection>`. A chunk is reapable when no manifest row in its own collection names it, in any owner state, and 30 days have passed since it last had an owner or was last written. The grace is per chunk, and the reaper has no setting for it.

**Each side expires only what it moved.** `nx index repo` and `nx t3 gc` move chunks into the same `quarantine-<collection>` collection, with no tag. The engine never deletes those, whatever their age. The client expires them on its own run, after `NX_GC_QUARANTINE_DAYS` (default 14), under its own floor. Setting `NX_GC_QUARANTINE_DAYS` to 60 keeps those chunks for 60 days; the engine's 14 days apply to the chunks the reaper moved and nothing else. A repository nobody has indexed for months keeps its quarantine until someone indexes it again. The converse holds too (engine changelog `vectors-026`): the client's expiry (`gc_expire_quarantine`, the call `nx index repo` makes, and `POST /v1/vectors/gc/expire-quarantine` with or without `force`) skips every chunk the reaper tagged, and its floor counts only the untagged rows. A client with `NX_GC_QUARANTINE_DAYS` set below 14 cannot shorten the retention of a chunk the reaper moved. A chunk counts as the reaper's only while its two stamps agree (`reaper_quarantined_at` equals `quarantined_at`); a chunk a client moved again later has a newer `quarantined_at`, so it is the client's.

Before a move the pass counts what is reapable, then applies two gates. The floor: if the whole reapable set is at least `NX_REAPER_FLOOR_MIN_CHUNKS` (100) chunks and more than `NX_REAPER_FLOOR_FRACTION` (0.25) of the collection, the collection is refused and nothing moves. The census: the engine re-runs the manifest-less census for the collection, bounded by `NX_REAPER_CENSUS_TIMEOUT_SECONDS`, and refuses the collection if it reads any `legacy-unmanifested` or `unclassified` chunk, if it read a different number of chunks than the count it is judging, or if it timed out. The move statement then takes the exclusive sweep gate, re-checks the floor, and re-checks the grace in its own `DELETE`, so a client write that refreshes a chunk after it was chosen wins.

The first pass runs 60 seconds after the engine boots, then every `NX_REAPER_INTERVAL_SECONDS`. A pass that spends its wall-clock budget resumes, on the next pass, at the tenant and collection where it stopped, so a slow collection cannot keep the later ones from ever being visited.

## Settings

All are engine environment variables. A malformed value logs `event=reaper_setting_invalid` and uses the default. The settings in force are logged at boot on `event=reaper_scheduled`.

| Variable | Default | Meaning |
|---|---|---|
| `NX_REAPER_ENABLED` | `true` | `false` (also `0`, `off`, `no`) turns the whole pass off, expiry included. `true`, `1`, `on`, `yes` leave it on. Any other value warns and leaves it on. |
| `NX_REAPER_INTERVAL_SECONDS` | `3600` | Seconds between passes. Values under 60 are raised to 60. |
| `NX_REAPER_BATCH_SIZE` | `300` | Most chunks moved from one collection per pass. 300 is the ceiling. |
| `NX_REAPER_FLOOR_FRACTION` | `0.25` | The floor on the MOVE (expiry has none). `1.0` turns it off, for every tenant and collection. To drain one collection, use `NX_REAPER_FLOOR_EXEMPT_COLLECTIONS` instead. |
| `NX_REAPER_FLOOR_MIN_CHUNKS` | `100` | The floor applies from this many reapable chunks up. |
| `NX_REAPER_FLOOR_EXEMPT_COLLECTIONS` | empty | Comma-separated entries that are exempt from the MOVE floor and from nothing else. Write `tenant/collection` (for example `default/code__1-1__voyage-code-3__v1`) to exempt that tenant's collection only. A bare collection name exempts it in EVERY tenant that has a collection of that name, and logs `event=reaper_floor_exempt_all_tenants` at boot, because collection names repeat across tenants. See "The first big cleanup". Names starting with `quarantine-` are ignored with a warning. |
| `NX_REAPER_QUARANTINE_RETENTION_DAYS` | `14` | Days a chunk the reaper moved stays in quarantine before the engine may delete it. 1 to 3650. Does not touch quarantine a client filled. |
| `NX_REAPER_WALL_CLOCK_BUDGET_SECONDS` | `600` | A whole run is cut at the next collection boundary once this is spent, and the next run resumes there. |
| `NX_REAPER_CENSUS_TIMEOUT_SECONDS` | `60` | Statement bound for one collection's census, 1 to 3600. See "Pathological collections". |

## Seeing what it did

Log lines, one set per pass: `event=reaper_run` (the whole run, `wall_clock_cut=`, `refused_total=`, `census_timed_out_total=` and `statement_timed_out_total=` included), `event=reaper_pass` (one per tenant, `candidates=0` included, and `expiry_protected=`), `event=reaper_collection_refused` and `event=reaper_tenant_refused` at WARN with `reason=` and a running `refused_total`, `event=reaper_collection_skipped` at INFO, `event=reaper_floor_exempt` at WARN, `event=reaper_expired`, and `event=reaper_expire_refused` (only when an expiry statement timed out; there is no floor to refuse it).

A cloud operator has no engine log, so the durable record is `gc_audit`. Add `--json` to every line: the text form prints id, time, operation, actor and the chash count, and leaves out the reason, the counts and the sample, which live in `details`.

```bash
nx catalog gc-audit list --operation reaper_quarantine --json          # a move: the chashes, actor engine-reaper
nx catalog gc-audit list --operation reaper_refused --json             # a refusal: reason, counts, up to 5 title/source_path
nx catalog gc-audit list --operation reaper_expire_quarantine --json   # an expiry of chunks the reaper moved
```

A refusal is written once per collection each time its reason changes, not every hour; a repeat of the same refusal writes nothing and only bumps `refused_total` in the log. A move or an expiry in between counts as a change. The `sample` in a floor or census refusal names up to five chunks by title and source path, from the chunk's own metadata, so you can tell a stale index from a mass orphaning. **A refusal of the expiry is filed under the quarantine collection's name** (`quarantine-<collection>`), not the origin's: pass `--collection quarantine-<name>` to read it. The expiry's own delete is audited as `reaper_expire_quarantine`, with every deleted chash (the first 5,000 of a call).

Four outcomes are not refusals and are never audited: `GATE_BUSY` (a manifest writer holds the collection's sweep gate; the exclusive acquire times out after 2 s), `LOCK_TIMEOUT` (a row lock or the sibling registration timed out for 2 s; this includes the expiry's own deletes), `CENSUS_BACKOFF` (a collection whose census keeps timing out is resting) and `STATEMENT_BACKOFF` (the same for a dry run, move or expiry that keeps timing out). All clear on their own and have their own counters (`gate_busy_total`, `lock_timeout_total`, `census_backoff_total`, `statement_backoff_total`).

## The refusals

| Reason | Meaning | What to do |
|---|---|---|
| `BACKFILL_INCOMPLETE` | The tenant has no verified backfill record. | Run `nx upgrade` against the tenant. |
| `COLLECTION_NOT_LIVE` | The collection is registered in a state other than `live`. | Nothing; it is deliberately left alone. |
| `FLOOR_EXCEEDED` | The reapable set is at least 100 chunks and more than a quarter of the collection. | Read the sample. If it is real garbage, see "The first big cleanup". |
| `CENSUS_LEGACY_UNMANIFESTED` | The census found a live legacy note with no manifest row. | `nx t3 census-manifest-less --collection <c>`, then re-put the notes (`docs/migration-runbook.md`). |
| `CENSUS_TIMED_OUT` | The census statement exceeded its bound; it was not read. | See "Pathological collections". |
| `CENSUS_SCOPE_MISMATCH` | The census read a different chunk count than the dry run: chunks changed between the two reads, or the census read the wrong scope. Retried next pass. | Persistent: report it. |
| `CENSUS_UNCLASSIFIED` | The census reports a chunk it could not classify. Unreachable in the SQL today. | Report it. |
| `STATEMENT_TIMED_OUT` | A statement other than the census (the dry run, the move, or one sibling's expiry) hit its 25 s bound (SQLSTATE 57014). Nothing was moved or deleted by it. After three in a row it rests, like the census. | See "Statement timeouts". |

`expiry_protected` is not in this table because it is not a refusal. It counts chunks past the retention window that a manifest row of the origin still names again. They are never deleted, nothing is audited, `refused_total` does not count them and no WARN is logged. For a `knowledge__` collection this is what a re-put of a note leaves behind: the re-put embeds a fresh chunk in the origin, so the quarantine copy lingers, protected, and there is nothing to do. For a repository collection a later `nx index repo` or heal moves such a chunk back.

## The first big cleanup: deploy plus 30 days

`vectors-020` gave every existing chunk `last_written_at = now()` at migration time, so for 30 days after the engine carrying it is first deployed nothing that already exists is old enough, and the reaper moves nothing. At deploy plus 30 days every existing orphan ages at once.

Drain rate is 300 chunks per collection per pass, so a collection with `R` reapable chunks drains in `ceil(R / 300)` hourly passes (147 orphans: one pass; 10,000: 34 hours; 55,000: 7.6 days). Collections drain in parallel, each at its own 300 per hour.

**Preview before the date.** The floor refuses a collection whose reapable set is 100 or more chunks and more than a quarter of it, with one audit row and an hourly WARN. To see which collections that will be, ask the engine what is reapable under a shorter grace. On day `d` after the engine carrying `vectors-020` is deployed, a chunk has been ownerless at most `d` days; those that are ownerless now and still ownerless at day 30 are exactly the ones a grace of `d` days selects today. So send `grace_seconds = d * 86400`, and divide by the collection's chunk count:

```bash
# Day 19 after deploy: grace_seconds = 19 * 86400 = 1641600.
# Page the listing (limit <= 300) until next_after is null and add up "returned":
curl -s -X POST "$NX_SERVICE_URL/v1/vectors/reapable" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"collection": "<name>", "grace_seconds": 1641600, "limit": 300}'
# ...then the same call with "after_chash": "<next_after>" for the next page. The total is the sum of the pages.
# The denominator:
curl -s -H "Authorization: Bearer $TOKEN" "$NX_SERVICE_URL/v1/vectors/count?collection=<name>"
```

With R pages-worth of chunks that is `ceil(R / 300)` calls. If `reapable / count` is more than 0.25 and the reapable total is at least 100, the floor will refuse that collection at day 30. After day 30 the first `reaper_refused` row for it answers the same question durably: its `details` carry `candidates` and `total`. The route is read-only and uses the same predicate as the reaper; `grace_seconds` is advisory and the reaper never uses it. Run `tests/e2e/cloud-client-path-gate.sh` first if you reach the engine through a public edge, to be sure the edge passes these routes.

**Draining a collection the floor refuses.** If a collection is legitimately mostly garbage (you have read the sample), exempt that one collection from the MOVE floor:

1. Set `NX_REAPER_FLOOR_EXEMPT_COLLECTIONS=<tenant>/<collection>` in the engine environment and restart the engine. The restart is the audit point: the exemption is logged at boot on `event=reaper_scheduled` as `floor_exempt_collections=`, logged again at WARN on `event=reaper_floor_exempt` on every pass that uses it (with `would_have_refused=`), and each move's `gc_audit` row records `floor_fraction` 1.0 instead of 0.25.
2. Leave it set for `ceil(R / 300)` hours. The floor is judged on the collection's whole reapable set on every pass, so it must stay off until the set is gone: setting it, waiting one pass and unsetting it moves 300 chunks and the collection is refused again. 55,000 chunks: 184 hours, about 7.6 days.
3. Unset it and restart. Nothing else was affected: the census, the grace, the 300 per pass, and the floors of every other collection all applied throughout.

`NX_REAPER_FLOOR_FRACTION=1.0` still exists. It is not a drain procedure: it turns the move floor off for every tenant and collection for as long as it is set.

A drain does not stall at expiry. Fourteen days after each pass, the 300 chunks (or fewer) that pass moved are deleted by the engine's expiry, in one call per origin of up to 5,000, so a drain's moves age out at the rate they were made and the last pass's chunks expire 14 days after it. A burst of moves that ages out together, such as the one-shot cliff at deploy plus 30 days, expires as one block.

## Expiry

There is no fraction floor on the engine's expiry (Sam, 2026-10-01). A floor judged on the chunks the reaper moved wedged every drain: any burst of 100 or more moved chunks ages past the retention window together, is all of the tagged rows, and would be refused every hour for ever. The floor on the move already bounds what one pass quarantines. What protects a chunk that was moved wrongly is the manifest recheck at expiry (a chunk a manifest row of the origin names is never deleted), the 14 day window, the `reaper_expire_quarantine` audit row that names every deleted chash, and the restore verb (nexus-wbfpw.49). Nothing in the engine forces an expiry and there is no override to reach for: the one setting is `NX_REAPER_QUARANTINE_RETENTION_DAYS`. The client's `gc/expire-quarantine` route, with `force`, cannot reach a chunk the reaper moved (see above), so do not use it for that.

To stop the engine deleting anything while you look at what it moved, set `NX_REAPER_ENABLED=false` (expiry stops with the moves) or raise `NX_REAPER_QUARANTINE_RETENTION_DAYS` and restart.

## Getting a chunk back

A chunk moved to `quarantine-<collection>` keeps its text and embedding. If a later `nx index repo` (or a heal) names it in a manifest row of the origin collection, the indexer's restore pass moves it back; for a `knowledge__` collection nothing does, and a manifest row that names the chunk only protects the quarantine copy from expiry. There is no operator verb that restores by chash in this change: a restore route and `nx t3 quarantine restore` are a separate piece of work (finished on a branch, not yet landed; it must be on `develop` and in the deployed engine before the first drain at deploy plus 30 days, and bead nexus-wbfpw.49 blocks the RDR-192 Phase 3 gate on it). Until it lands, a chunk the reaper moved can be found by `reaper_quarantine` audit row (its `chashes`, in `details`'s quarantine collection) and by `store_get_many` with `collection=quarantine-<name>`, which returns its text and metadata; the chunks are in the sibling for the retention window.

## Known limits

**The census can read a live document as `no-owner`.** It resolves a chunk's owner from the chunk's own metadata (`catalog_doc_id`, then `doc_id`) or a note-shaped reverse match. A `docs__` or `code__` chunk written after RDR-108 carries no document id, so a live document whose manifest rows are missing reads `no-owner`, not `legacy-unmanifested`, and the census passes. The backstops are the 30 day grace, the floor, and the retention window in quarantine. A collection under 100 reapable chunks is exempt from the floor and is emptied in one pass, so for small collections the grace and the quarantine are the whole protection.

**Pathological collections.** The census is a per-chunk join whose cost grows with the number of manifest rows of each chunk's owning document. Measured on PG17 under the application role at 80,000 chunks: 8,000 documents of 10 chunks each, 560 ms (the dry run, 240 ms); one document owning all 80,000 chunks, 393 seconds. The second shape reaches `CENSUS_TIMED_OUT` at the 60 s default. After three passes in a row, the census for that collection rests for 2, then 4, 8, 16 passes and so on, at most 24 hours, and is retried after each rest; any completed census ends the streak. The first timeout in a streak writes the one durable `reaper_refused` row, and `census_timed_out_total` counts every timeout. While it rests the collection logs `reaper_collection_skipped reason=CENSUS_BACKOFF`. The collection is never moved while its census times out. To let it through, raise `NX_REAPER_CENSUS_TIMEOUT_SECONDS` (up to 3600; 600 clears the measured 393 s case) in the engine environment and restart; the streak is forgotten at restart. The census runs on the shared sweep thread, so the longer bound is paid there once per pass, bounded by the 600 s wall-clock budget.

**Statement timeouts.** The enumeration, the dry run, the move and each sibling's expiry run under a 25 s statement bound. One that hits it (SQLSTATE 57014) is a counted `STATEMENT_TIMED_OUT` refusal with one durable `reaper_refused` row (the expiry's is filed under the `quarantine-` name), not a stack trace every hour; `statement_timed_out_total` counts each. After three in a row for the same collection and statement it rests for 2, 4, 8 passes and so on, at most 24 hours (`STATEMENT_BACKOFF`, `statement_backoff_total`), and a completed statement ends the streak. Streaks live in memory and are forgotten at restart.

**Protected quarantine copies are kept for ever.** A chunk past the retention window that a manifest row of its origin names is never deleted, and nothing removes it later. A manifest row can only name a chunk that exists in the origin collection (a foreign key), so the quarantine copy is a redundant duplicate and the cost is storage, not data. It shows as `expiry_protected` every pass.

**Tenants that drop out.** The pass enumerates `service_tokens`, because `nexus.chunks` is row-level secured and cannot be enumerated across tenants. A `scope=data` token row is deleted seven days after it expires, so a cloud tenant that is idle with no live token is not visited until it holds a token again. That is a liveness gap, not a deletion hazard.

**It is the safety net, not the fast path.** The post-commit sweep removes the replaced chunks of a re-indexed document inline; the reaper finds what that sweep left behind when it failed open (a gate timeout, a statement timeout) or when a multi-request run crashed. Debris from either is reaped 30 days later, not on the next pass.
