# Engine reaper runbook (RDR-192 Step 9)

The engine runs a pass once an hour that moves ownerless T3 chunks into a quarantine collection, and deletes quarantine that is 14 days old. This page is for the person who runs an engine: what a pass does, how to see it, what the refusals mean, and what to expect at the first big cleanup. Bead nexus-2x9xa.

## What a pass does

A pass visits the default tenant and every tenant that has a row in `service_tokens`. For each tenant it:

1. Refuses the whole tenant unless the `rdr192-manifest-backfill` rung is recorded (`nx upgrade` writes it). Until then the reaper touches nothing for that tenant.
2. Expires that tenant's quarantine first: for each `quarantine-<collection>` whose origin collection is registered, `gc_expire_quarantine` deletes chunks stamped more than 14 days ago. It never deletes a chunk that a manifest row in the origin still names, and it applies its own floor (below). The reaper never forces it.
3. Visits each other collection (all four prefixes: `knowledge__`, `docs__`, `code__`, `rdr__`) that is registered and `live`, and moves at most 300 chunks that `nexus.chunk_is_reapable` selects into `quarantine-<collection>`. A chunk is reapable when no manifest row in its own collection names it, in any owner state, and 30 days have passed since it last had an owner or was last written. The grace is per chunk, and the reaper has no setting for it.

Before a move the pass counts what is reapable, then applies two gates. The floor: if the whole reapable set is at least `NX_REAPER_FLOOR_MIN_CHUNKS` (100) chunks and more than `NX_REAPER_FLOOR_FRACTION` (0.25) of the collection, the collection is refused and nothing moves. The census: the engine re-runs the manifest-less census for the collection, bounded by `NX_REAPER_CENSUS_TIMEOUT_SECONDS`, and refuses the collection if it reads any `legacy-unmanifested` or `unclassified` chunk, if it read a different number of chunks than the count it is judging, or if it timed out. The move statement then takes the exclusive sweep gate, re-checks the floor, and re-checks the grace in its own `DELETE`, so a client write that refreshes a chunk after it was chosen wins.

The first pass runs 60 seconds after the engine boots, then every `NX_REAPER_INTERVAL_SECONDS`.

## Settings

All are engine environment variables. A malformed value logs `event=reaper_setting_invalid` and uses the default.

| Variable | Default | Meaning |
|---|---|---|
| `NX_REAPER_ENABLED` | `true` | `false` (also `0`, `off`, `no`) turns the whole pass off, expiry included. `true`, `1`, `on`, `yes` leave it on. Any other value warns and leaves it on. |
| `NX_REAPER_INTERVAL_SECONDS` | `3600` | Seconds between passes. Values under 60 are raised to 60. |
| `NX_REAPER_BATCH_SIZE` | `300` | Most chunks moved from one collection per pass. 300 is the ceiling. |
| `NX_REAPER_FLOOR_FRACTION` | `0.25` | The floor, for the move and for the expiry. `1.0` turns it off, for every tenant and collection. |
| `NX_REAPER_FLOOR_MIN_CHUNKS` | `100` | The floor applies from this many reapable (or expiring) chunks up. |
| `NX_REAPER_WALL_CLOCK_BUDGET_SECONDS` | `600` | A whole run is cut at the next collection boundary once this is spent. |
| `NX_REAPER_CENSUS_TIMEOUT_SECONDS` | `60` | Statement bound for one collection's census. |

## Seeing what it did

Log lines, one set per pass: `event=reaper_run` (the whole run, `wall_clock_cut=` included), `event=reaper_pass` (one per tenant, `candidates=0` included), `event=reaper_collection_refused` and `event=reaper_tenant_refused` at WARN with `reason=` and a running `refused_total`, `event=reaper_collection_skipped` at INFO, `event=reaper_expired` and `event=reaper_expire_refused`.

A cloud operator has no engine log, so the durable record is `gc_audit`:

```bash
nx catalog gc-audit list --operation reaper_quarantine   # a move: the chashes, actor engine-reaper
nx catalog gc-audit list --operation reaper_refused      # a refusal: reason, counts, up to 5 title/source_path
nx catalog gc-audit list --operation gc_expire_quarantine # an expiry (written by the SQL function itself)
```

A refusal is written once per collection each time its reason changes, not every hour; a repeat of the same refusal writes nothing and only bumps `refused_total` in the log. The `sample` in a floor or census refusal names up to five chunks by title and source path, from the chunk's own metadata, so you can tell a stale index from a mass orphaning.

Two outcomes are not refusals and are never audited: `GATE_BUSY` (a manifest writer holds the collection's sweep gate; the exclusive acquire times out after 2 s) and `LOCK_TIMEOUT` (a row lock or the sibling registration timed out for 2 s). Both clear next pass and have their own counters.

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
| `EXPIRY_REFUSED` | `gc_expire_quarantine` deleted nothing from a quarantine collection: the floor tripped, or every past-cutoff chunk is still named by a manifest row. | See "The expiry floor". |

## The first big cleanup: deploy plus 30 days

`vectors-020` gave every existing chunk `last_written_at = now()` at migration time, so for 30 days after the engine carrying it is first deployed nothing that already exists is old enough, and the reaper moves nothing. At deploy plus 30 days every existing orphan ages at once.

Drain rate is 300 chunks per collection per pass, so a collection with `R` reapable chunks drains in `ceil(R / 300)` hourly passes (147 orphans: one pass; 10,000: 34 hours; 55,000: 7.6 days). Collections drain in parallel, each at its own 300 per hour.

The floor refuses a collection whose reapable set is a quarter or more of it, which is exactly the mostly-orphan collections, indefinitely, with one audit row and an hourly WARN. Preview before the date, week four after the deploy:

```bash
curl -s -X POST "$NX_SERVICE_URL/v1/vectors/reapable" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"collection": "<name>", "limit": 100}'
```

The route is read-only and uses the same predicate as the reaper. It lists chunks with `title` and `catalog_doc_id`; `next_after` is a keyset cursor (`after_chash`) for the next page. `grace_seconds` lowers the window for a preview of what will age in, and is advisory only: the reaper never uses it. If a collection is legitimately mostly garbage, the override is `NX_REAPER_FLOOR_FRACTION=1.0` in the engine environment and a restart. It is global (every tenant, every collection, expiry included) and stays until you set it back, so set it, wait one pass, and unset it.

## The expiry floor

Quarantine expiry has the same floor, judged against all chunks in that quarantine collection. The reaper refills a quarantine hourly, which dilutes the denominator, and the first large drain expires as a block: if 100 or more chunks and more than a quarter of the quarantine are past 14 days at once, `gc_expire_quarantine` refuses, nothing is deleted, and the reaper writes an `EXPIRY_REFUSED` row. The reaper never forces. Chunks stay in quarantine, out of every search surface, until you force the expiry once: `POST /v1/vectors/gc/expire-quarantine` with `quarantine_collection`, `origin_collection`, a `cutoff` (14 days ago, `YYYY-MM-DDTHH:MM:SSZ`) and `"force": true`. That hard-deletes the past-cutoff chunks it selects (never one a manifest row names). The indexer's own expiry step takes the same override as `NX_GC_FORCE=1 nx index repo`, for that repository's collections only.

## Getting a chunk back

A chunk moved to `quarantine-<collection>` keeps its text and embedding. If a later `nx index repo` (or a heal) names it in a manifest row of the origin collection, the indexer's restore pass moves it back. There is no operator verb that restores by chash; a restore route and `nx t3 quarantine restore` are a separate piece of work and are not part of this change. Until then, the `reaper_quarantine` audit row lists the chashes it moved, and the chunks are in the sibling for 14 days.

## Known limits

**The census can read a live document as `no-owner`.** It resolves a chunk's owner from the chunk's own metadata (`catalog_doc_id`, then `doc_id`) or a note-shaped reverse match. A `docs__` or `code__` chunk written after RDR-108 carries no document id, so a live document whose manifest rows are missing reads `no-owner`, not `legacy-unmanifested`, and the census passes. The backstops are the 30 day grace, the floor, and 14 days in quarantine. A collection under 100 reapable chunks is exempt from the floor and is emptied in one pass, so for small collections the grace and the quarantine are the whole protection.

**Pathological collections.** The census is a per-chunk join whose cost grows with the number of manifest rows of each chunk's owning document. Measured on PG17 under the application role at 80,000 chunks: 8,000 documents of 10 chunks each, 560 ms (the dry run, 240 ms); one document owning all 80,000 chunks, 393 seconds. The second shape reaches `CENSUS_TIMED_OUT` at the 60 s default every hour and is never reaped; the cost is 60 s of the shared sweep thread per pass for that collection.

**Tenants that drop out.** The pass enumerates `service_tokens`, because `nexus.chunks` is row-level secured and cannot be enumerated across tenants. A `scope=data` token row is deleted seven days after it expires, so a cloud tenant that is idle with no live token is not visited until it holds a token again. That is a liveness gap, not a deletion hazard.

**It is the safety net, not the fast path.** The post-commit sweep removes the replaced chunks of a re-indexed document inline; the reaper finds what that sweep left behind when it failed open (a gate timeout, a statement timeout) or when a multi-request run crashed. Debris from either is reaped 30 days later, not on the next pass.
