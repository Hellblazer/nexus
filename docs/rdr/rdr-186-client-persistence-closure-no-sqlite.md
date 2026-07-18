---
title: "Client-Persistence Closure: PG in Every Mode — Retire Every SQLite Substrate Outside RDR-158's Seven-Domain Scope, and End Self-Granted Exemptions"
id: RDR-186
type: Architecture
status: accepted
accepted_date: 2026-07-18
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-07-18
related_issues: [nexus-146xx, nexus-tidtd, nexus-83ld0, nexus-tcqah, nexus-ixl85, nexus-gmiaf]
related: [RDR-152, RDR-153, RDR-155, RDR-156, RDR-157, RDR-158, RDR-185]
---

# RDR-186: Client-Persistence Closure — PG in Every Mode

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-158 (accepted 2026-06-12) settled the direction: PG-service is the only T2
path; SQLite is retired. But its scope enumerates the **seven-domain T2
backend** (nine tripwire pairs) — and that enumeration became the hole.
Everything created since sits *outside* it and self-granted an exemption:

> `# epsilon-allow: ladder-local completion records (ladder.db) own their
> substrate — deliberately outside T2Database/apply_pending ... RDR-158-exempt`

Two brand-new client SQLite databases were created in July 2026 **inside the
migration epic itself** (RDR-185: `ladder.db`, `chash_remap.db`), each blessed
by a comment citing exemption from an *accepted* RDR whose whole point is
SQLite retirement. The RDR-120 storage-boundary lint accepts any `>=8`-char
`# epsilon-allow:` reason, so the exemption mechanism is self-service. A third
near-miss (`leg_convergence`, bead nexus-tidtd, 2026-07-18) was designed,
implemented, and reverted the same night — caught by a design critic, not by
any mechanism.

Hal directive of record (2026-07-18, T2
`nexus/directive-no-sqlite-pg-everywhere`, AGENTS.md hot rule, verbatim
intent): **"We are MIGRATING from SQLite TO PG. IN EVERY MODE. THERE IS NO
SQLITE HYBRID MODE. None."** SQLite is a migration source only, never a
destination. Exemptions are Hal's decisions, never code comments.

This RDR closes the gap between RDR-158's enumerated scope and the
directive's totality.

#### Gap 1: Scope hole with no mechanical resistance — anything outside RDR-158's enumeration can claim exemption

(Named "the scope-hole gap" in citations — never "Gap 4"; that token
belongs to RDR-185's two-mechanism pin, cited throughout this document.)

Until 2026-07-18 nothing mechanically resisted new SQLite.
`tests/test_no_new_sqlite.py` (commit `54f7bd65`) now freezes per-file
counts of inline SQLite DDL (15 files) and `# epsilon-allow:` overrides
(43 files); growth fails. The freeze stops accretion; this RDR supplies
the *ratchet to zero*. Known scanner limitations, tracked not blocking:
the DDL regex cannot see schema growth via `ALTER TABLE`, nor a new
`sqlite3.connect` call in an already-censused file with no new
`CREATE TABLE` (the boundary lint's separate connect-count ratchet
partially covers the latter); P0 hardens or accepts these explicitly.

RDR-158 governs the seven-domain stores. Any store
  outside that list (ladder, chash-remap, pipeline buffer, catalog local
  file, ad-hoc tables in `nexus.db`) can claim it is not covered. The
  directive admits no such reading; this RDR is where the *complete*
  inventory gets adjudicated.
#### Gap 2: Bootstrap ordering — the one honest argument in the exemption comments

The upgrade ladder's completion store
  exists "before the t2-schema rung it records"; migration artifacts must
  survive engine absence and mid-install crashes. Retiring these is a design
  problem (what persists when the engine is down?), not a sed. This is the
  *one* honest argument the exemption comments contain, and it deserves a
  real answer instead of a comment.
#### Gap 3: Stray data outside every migration path

`aspect_promotion_log`
  (`aspect_promotion.py:290`, created lazily "so we avoid yet another T2
  migration entry") and `_nexus_t3_steps` (`commands/upgrade.py:687`, created
  inline mid-command) live in `nexus.db` but are registered in **no**
  migration registry and **no** ETL. RDR-158 P4 deleting the SQLite schema
  would silently drop their data.
### Evidence

- `src/nexus/upgrade_ladder/completion.py:73`, `src/nexus/migration/wire_reid.py:151`,
  `src/nexus/pipeline_buffer.py:102` — own-substrate `sqlite3.connect` sites,
  each with a self-granted `# epsilon-allow` citing RDR-158-exempt or the
  "own substrate" shape.
- `tests/test_no_new_sqlite.py` — the frozen 2026-07-18 census (the complete
  inventory this RDR adjudicates).
- RDR-157 + directive `feedback_always_install_pg_bundle_no_fallback`: every
  install ships PG17+pgvector. **The PG substrate exists in every mode
  today** — local mode's endpoint is the bundled local PG via the engine,
  the same shape as service mode. "Local means SQLite" has been false since
  RDR-157 shipped; the remaining SQLite is inertia, not necessity.
- Bead nexus-tidtd + T2 `nexus/critique-tidtd-design`: the near-miss that
  proved the accretion pattern is alive, plus a live design (the per-leg
  delivery/convergence fact) currently homeless because its natural SQLite
  home is banned.
- RDR-185 .16 design notes: the PG `chash_remap` table (Liquibase,
  tenant/RLS) + Java bulk-remap endpoint are already-planned engine-side
  work — the PG twin of `chash_remap.db` exists on paper.

## Decision (draft — options to resolve in research)

**D1 — Totality.** End state: the only durable client-side files are (i)
configuration, (ii) logs, (iii) artifacts explicitly adjudicated in THIS
RDR's inventory table with Hal's decision recorded. Zero SQLite databases,
zero inline SQLite DDL, zero `# epsilon-allow:` SQLite overrides. The
tripwire censuses ratchet monotonically to empty and then assert empty.

**D2 — Migration artifacts go to PG via the already-planned surfaces; the
convergence question is answered by LIVE computation, never a stored
verdict.** (Revised per RF-186-1 — the original draft proposed a
`leg_convergence` relation and is NO-GO: the Gap-4 pin is behavioral and
substrate-agnostic, so a stored "converged at T" row consulted by detect()
collides with it in PG exactly as it did in SQLite.)

`chash_remap.db` → the RDR-185 .16 PG `chash_remap` table (Liquibase,
tenant/RLS), with the local file demoted to migration *source* and
read-only. The nexus-tidtd convergence question is then answered
engine-side by a **live membership computation** over the target chunk
tables joined to `chash_remap` (RDR-154 leverage style — an indexed SQL
function computing "how many of this leg's transformed source ids are
present", called by rung detect()/verify() each time). No verdict is ever
persisted: the map rows are raw facts a live computation interprets, the
sanctioned shape `_default_unreflected` already uses. Rollback CLEARS the
leg's `chash_remap` rows, so delivered-then-rolled-back and
never-delivered collapse to the same live state (nothing owed) — the
resurrection hazard is resolved at the fact layer, no verdict lifecycle
needed. This simultaneously replaces the broken count-equality test (the
real nexus-tidtd fix) and resolves nexus-ixl85's probe-cost concern for
this rung (one indexed SQL call per leg). The Gap-4 pin stands
**unamended**; its docstring gains one documenting line recording
live-membership-computation as the sanctioned cheap detect().

**The rollback-clear is a change to `rollback_collections`, with a hard
ordering constraint** (gate Critical, 2026-07-18): today
`rollback_collections` (`vector_etl.py:1677`) does NOT touch the map —
that is precisely what RF-186-1 fixes — and the map's documented
"PERMANENT retention" (`wire_reid.py:143`) is load-bearing for
rollback's own retry idempotency: the function resolves
`entries_with_targets()` up front and pages through deletes, so a
crash/retry mid-rollback must find the translation table intact or a
cross-model retry silently probes the wrong target. Therefore the leg's
map rows are cleared ONLY after the leg's full rollback verifiably
completes (strictly after the existing `target_after` count
verification), never eagerly, never per-page — the same
clear-only-after-proof shape `clear_convergence` was reviewed to in the
reverted marker design. Rolling back a leg also reverts the downstream
remap-cascade targets keyed by the map (the RDR-185 .13 audit set:
manifest, chash_index, topic_assignments, frecency/relevance_log,
aspects; `CHASH_BEARING_TABLES` + nexus-z5j0t extension) before or
atomically with the map-row clear — a leg is not "rolled back" while
local stores still point at its new chashes. At P1 both docstrings'
"permanent" language (`wire_reid.py:143`, `vector_etl.py:1698`) narrows
to the carve-out that actually holds: permanent across every failure
and retry, cleared only by a leg rollback that verifiably completed —
out-of-band references to OLD ids remain resolvable forever because the
old ids return to being the live ids when the migrated rows are
removed. ("Full rollback verifiably completes" means the WHOLE-LEG
scope — every collection the leg touched has passed its `target_after`
verification, i.e. the whole-function return of `rollback_collections`,
not any single collection's per-loop check — re-gate residual,
2026-07-18.)

**D3 — Ladder state: derive-first, record-late.** The completion store's
bootstrap-ordering argument is real: the ladder runs when the engine may be
absent (its own rungs install/converge the engine). Candidate resolution,
leaning on RDR-142's own philosophy (verify re-reads the world; completion
records are position bookkeeping, not truth): rungs are already
verify-before-record and re-derivable from world state, so pre-engine ladder
state can be held in-process and flushed to a PG `ladder_completions`
relation once the engine is up. Crash inside the pre-engine window costs a
re-derivation (idempotent by the RDR-142 contract), not correctness. If
research falsifies this (a rung whose completion is genuinely
non-re-derivable), the fallback is a **flat file** (append-only JSONL, no
query surface, no DDL) — never SQLite.

(CONFIRMED by RF-186-2, no falsification: the completion store is pure
bookkeeping — the runner's only read of `verified_rungs()` skips a
redundant verify() when detect() already re-derived; losing the store
costs one extra cheap read-only verify() per rung, and both loss
scenarios are already mechanically pinned in `test_ladder_runner.py`.
`nx doctor` never opens the store; `nx init` never touches the ladder;
`_converge_preconditions()` brings the engine up before `_run_ladder()`
on the normal path. P2 design obligations from the research: the
in-process holder must serve the `verified_rungs()` check for later
rungs within the SAME walk while the engine is down; the
`verified_at`/`package_version` audit metadata is observability-only and
may be accepted as lossy across the transition.)

**D4 — Strays are adjudicated, not inherited.** `aspect_promotion_log` is an
observability log → PG telemetry surface (RDR-177 territory) or explicit
deletion with Hal's sign-off. `_nexus_t3_steps` is upgrade bookkeeping →
subsumed by the ladder mechanism (D3). Neither survives as-is; both get a
data-carry decision before RDR-158 P4 deletes the schema under them.

**D5 — Catalog local file rides RDR-158's gates.** Local-mode `catalog.db`
(RDR-146/168 lineage) has a service drop-in (`HttpCatalogClient`, RDR-156
catalog tables). Its retirement is sequenced with RDR-158 P3 (service-only
path), not independently — one deprecation window, not three.

**D6 — Enforcement ratchet.** Each phase that retires a store lowers the
frozen census in the same commit (the tripwire's shrink-side assertion forces
this). End state flips the tripwire from "frozen census" to "assert empty",
and the storage-boundary lint's SQLite arm + `# epsilon-allow:` token are
retired with the last site. Any *increase* along the way requires editing the
census file — one reviewed surface — with a bead reference recording Hal's
decision.

**Out of scope, explicitly:** T1's local Chroma default (RDR-155 P4b
territory, beads nexus-g37fr/nexus-19svb — DO NOT START per the standing
boundary); the seven-domain T2 store deletion itself (RDR-158 P3/P4 owns it;
this RDR feeds its inventory and rides the same window); test-fixture SQLite
(`tmp_path` fixtures against existing stores — slides until the store it
tests is deleted, per Hal 2026-07-18).

## Approach (phased, draft)

1. **P0 — Inventory adjudication (no code).** Every entry in the two frozen
   censuses binned into {migrate-to-PG, delete-with-feature, flat-file
   demotion, rides-RDR-158-P3/P4, not-actually-debt}, each row carrying its
   data-carry plan and its gate. (The fifth bin exists for entries like
   `storage_boundary_lint.py`'s 10 self-referential token-definition
   matches — census noise, not persistence debt; binning them explicitly
   beats silently ignoring them.) P0 also resolves Q3 (FTS5 audit outside
   the seven domains) and Q6 (cold-start state audit), and decides
   harden-vs-accept for the tripwire's known scanner limitations (Gap 1).
   Deliverable: the adjudication table appended to this RDR, gated +
   accepted — this is where "exceptions are Hal's decisions" becomes
   literal.
2. **P1 — chash_remap to PG (engine-side, second release lifecycle).** The
   .16 Liquibase changeset + bulk-remap endpoint; `wire_reid` writes through
   the engine; the local file becomes read-only source. Includes the D2
   live-membership SQL function (no persisted relation — the real
   nexus-tidtd fix), the `rollback_collections` change with the
   clear-only-after-verified-completion ordering + downstream cascade
   revert (D2), and the narrowed-retention docstring updates in
   `wire_reid.py` and `vector_etl.py`.
3. **P2 — Ladder state derive-first (D3).** Prove or falsify pre-engine
   re-derivability per rung; implement the PG flush; retire `ladder.db` or
   demote to flat file per the research outcome.
4. **P3 — Strays (D4) + pipeline buffer.** Liquibase changesets + data carry
   for what survives; explicit deletion records for what does not.
   `pipeline_buffer.py` adjudicated in P0 (**RATIFIED 2026-07-18:
   engine-host** — RDR-048 design lineage via the RDR-173-style
   engine-worker hosting pattern; retire is off the table).
5. **P4 — Zero.** Rides RDR-158 P3/P4 and the RDR-155 P4b window (one N+1
   release, per RF-158-3): census ratchets to empty, tripwire flips to
   assert-empty, epsilon-allow SQLite arm retired. Inverse-grep clean.

## Alternatives considered

- **Fold all of this into RDR-158.** Rejected: 158 is accepted with a
  precise seven-domain scope and is *correct* within it; retrofitting would
  reopen an accepted RDR and blur its P3/P4 gates. This RDR references and
  rides 158's gates instead — and exists precisely because out-of-scope
  accretion needs its own adjudication surface.
- **A standing "bootstrap exemption" for migration/ladder artifacts.**
  Rejected as a *class*: it is the self-service exemption pattern with
  better paperwork. Individual artifacts may still end as flat files via
  P0 adjudication — but each is a named, Hal-decided row, not a category
  new code can join.
- **Ratchet by policy (docs only), no test.** Rejected: RDR-158 was accepted
  policy for a month while two new SQLite substrates shipped citing it as
  their exemption. A docs rule alone degrades to hope (the
  `test_lifecycle_gate.py` lesson, verbatim).

## Consequences

- One persistence substrate, every mode, including mid-upgrade: the engine's
  PG (bundled locally, managed in cloud). Client Python keeps zero query
  surfaces of its own.
- The upgrade ladder gets a harder job (pre-engine window without its own
  db) — paid for by the RDR-142 derive-first contract it already claims to
  honor. If that contract is false anywhere, this RDR forces the discovery.
- The tidtd convergence fix lands on a sanctioned surface with tenancy and
  RLS for free, instead of a client-side sqlite file per install.
- Review load concentrates on one census file for any SQLite delta —
  the "infection spread" question becomes `git log tests/test_no_new_sqlite.py`.

## Open Questions

- ~~**Q1 (P2-blocking):** Is every rung's completion genuinely re-derivable
  with the engine down?~~ **RESOLVED by RF-186-2 — yes, unconditionally;
  ladder.db retirable per D3 as specced.** Both suspects cleared
  (`apply_attempted` is an in-process flag; cascade-repair never reads
  ladder.db). The watermark file is ALREADY flat JSON (the D3-blessed
  shape), not SQLite, needs no census adjudication; it is where the real
  replay cost lives (~900s/90k-chunk worst case if lost).
- ~~**Q2 (P1-blocking):** Does the delivery-fact-in-PG design collide with
  the Gap-4 two-mechanism pin?~~ **RESOLVED by RF-186-1 — yes, it
  collides; D2 revised to live membership computation with no stored
  verdict.** The pin is behavioral and substrate-agnostic; the principled
  line is "raw facts a live computation interprets: fine; persisted
  verdicts consulted instead of re-deriving: banned". Pin stands
  unamended (one documenting docstring line only).
- ~~**Q7 (product fork, Hal's decision at gate — from RF-186-1):** Is a
  deliberately rolled-back leg "converged" or "pending"?~~ **RATIFIED by
  Hal 2026-07-18: rolled-back means PENDING.** Rollback clears the leg's
  `chash_remap` rows; the system reads "nothing owed → pending again if
  the source still classifies"; a rolled-back install that re-runs
  `nx upgrade` re-plans the leg behind the cost-consent gate.
- **Q3 (resolved at P0):** FTS5 in local mode — the seven-domain retirement inherits RDR-152's
  locked FTS5→tsvector parity contract; confirm nothing outside those
  domains grew an FTS5 dependency (memory_store FTS is in-scope-158; anything
  else?).
- ~~**Q4:** `pipeline_buffer.py` — is the streaming-PDF resume feature (RDR-048
  lineage) still load-bearing post-RDR-173, or retirable outright?~~
  **DECIDED by Hal 2026-07-18 (P0 adjudication): engine-host the feature.
  Design lineage is RDR-048 (which created the pipeline buffer); the
  hosting shape borrows RDR-173's engine-worker pattern; resume state
  moves to an engine-side relation. Drives `.16`.**
- **Q5:** Does the engine expose (or need) a generic small-state KV surface
  for client bookkeeping, or does each artifact get a first-class relation?
  (Bias per RDR-154: first-class relations with real schemas; a KV bucket is
  the SQLite hybrid wearing a PG costume.)
  **RELAXED by Hal 2026-07-18:** the strict first-class-relations-only bias
  is softened — a generic small-state KV facility is permissible,
  especially as a *transitional* mechanism (use, then discard once the
  artifact gets its first-class home or retires). Implementing beads
  (`.10`/`.14`/`.16`) may choose KV-first where it shortens the path,
  provided the facility itself is engine-side PG (never a client
  substrate) and its retirement is recorded on the bead that adopts it.
- **Q6 (resolved at P0):** Windows/cold-start: `nx` invoked before any engine has EVER been
  installed (fresh machine, first run) — what is the precise set of state
  the CLI may need before the first engine start, and is all of it config
  (yaml) rather than data?

## P0 Inventory Adjudication (nexus-146xx.1)

> Status: RATIFIED by Hal 2026-07-18. Settled rows transcribe D2–D5 (no
> re-adjudication); the three open forks (aspect_promotion_log,
> pipeline_buffer, scanner harden-vs-accept) were decided by Hal
> 2026-07-18 and are recorded inline. This table is the decision record;
> each surviving artifact is a named, Hal-decided row.

### DDL census (15 files, 63 statements — `DDL_CENSUS`)

| File | N | Artifact | Bin | Data carry | Gate |
|---|---|---|---|---|---|
| `aspect_promotion.py` | 1 | `aspect_promotion_log` stray | migrate-to-PG (**Hal 2026-07-18**: PG telemetry surface, RDR-177 territory) | log rows carried into PG telemetry | `.14` (P3); ordering gate: waits on RDR-177 acceptance OR lands a minimal relation / transitional facility (Q5 relaxation) that does not presuppose RDR-177's design |
| `commands/upgrade.py` | 1 | `_nexus_t3_steps` stray | delete-with-feature — subsumed by ladder mechanism (D3/D4) | none: upgrade bookkeeping, re-derivable | `.15` (P3) |
| `db/migrations.py` | 25 | seven-domain T2 registry | rides-RDR-158-P3/P4 | RDR-158 ETL | nexus-i711w |
| `db/t2/aspect_extraction_queue.py` | 3 | T2 domain store | rides-RDR-158-P3/P4 | RDR-158 ETL | nexus-i711w |
| `db/t2/catalog.py` | 9 | catalog store (D5) | rides-RDR-158-P3/P4 | `HttpCatalogClient` drop-in (RDR-156 tables) | nexus-i711w; NO bead here (D5) |
| `db/t2/catalog_taxonomy.py` | 4 | T2 domain store | rides-RDR-158-P3/P4 | RDR-158 ETL | nexus-i711w |
| `db/t2/chash_index.py` | 2 | T2 domain store | rides-RDR-158-P3/P4 | RDR-158 ETL | nexus-i711w |
| `db/t2/document_aspects.py` | 1 | T2 domain store | rides-RDR-158-P3/P4 | RDR-158 ETL | nexus-i711w |
| `db/t2/document_highlights.py` | 1 | T2 domain store | rides-RDR-158-P3/P4 | RDR-158 ETL | nexus-i711w |
| `db/t2/memory_store.py` | 3 | T2 domain store (FTS5) | rides-RDR-158-P3/P4 | FTS5→tsvector parity (RDR-152, locked) | nexus-i711w |
| `db/t2/plan_library.py` | 3 | T2 domain store (FTS5) | rides-RDR-158-P3/P4 | RDR-158 ETL | nexus-i711w |
| `db/t2/telemetry.py` | 4 | T2 domain store | rides-RDR-158-P3/P4 | RDR-158 ETL | nexus-i711w |
| `migration/wire_reid.py` | 1 | `chash_remap.db` | migrate-to-PG (D2) | engine bulk-remap; local file demoted read-only source | `.3`/`.6` (P1) |
| `pipeline_buffer.py` | 3 | `pipeline.db` | migrate-to-PG (**Hal 2026-07-18, resolves Q4**: engine-host the streaming-PDF resume feature — RDR-048 design lineage, hosted via the RDR-173-style engine-worker pattern) | resume state moves to an engine-side relation | `.16` (P3) |
| `upgrade_ladder/completion.py` | 2 | `ladder.db` | delete-with-feature (D3 derive-first; RF-186-2: re-derivable, no falsification) | none — in-process holder + PG flush | `.11`/`.12` (P2) |

### Epsilon-allow census (43 files, 118 overrides — `EPSILON_CENSUS`)

Overrides fall into six classes; every censused file is listed with its
dominant class. Mixed files carry a note.

**Class R — raw/read-only access to seven-domain T2 stores (RDR-128 P3
lineage: daemon-offline reads, guarded raw cursors, service-mode-guarded
branches). Bin: rides-RDR-158-P3/P4** — the call sites are rewritten or
die when T2 is PG-only; no data carry of their own (they touch stores
carried by RDR-158's ETL): `_session_end_launcher.py` (1),
`aspect_promotion.py` (6), `catalog/catalog_owners.py` (1, D5),
`collection_audit.py` (3; note: `:370` is `.catalog.db`, D5),
`collection_health.py` (3), `commands/_helpers.py` (1),
`commands/aspects.py` (7; note: `:349`/`:495` are pre-migration repair
verbs — die with the migration window), `commands/catalog.py` (1),
`commands/catalog_cmds/backfill.py` (3, D5),
`commands/catalog_cmds/report.py` (3), `commands/daemon.py` (1),
`commands/doc.py` (3), `commands/doctor.py` (6; note: `:1354` is help
text mentioning the token — census noise), `commands/enrich.py` (9;
note: `:1837` also writes `aspect_promotion_log` — follows `.14`),
`commands/index.py` (3; note: `:1295` is a chroma `EphemeralClient`
dry-run — non-SQLite, out-of-scope-155), `commands/plan.py` (2),
`commands/rdr.py` (1), `commands/search_cmd.py` (1),
`commands/taxonomy_cmd.py` (17), `commands/tier_status.py` (1),
`commands/upgrade.py` (3, chicken-and-egg bootstrap — dies with SQLite
T2), `console/routes/health.py` (1), `context.py` (1), `health.py` (2),
`mcp_infra.py` (4), `merge_candidates.py` (2),
`operators/aspect_sql.py` (6), `taxonomy.py` (1),
`upgrade_ladder/rungs/t2_schema.py` (1, the t2-schema rung's bootstrap —
retires with SQLite T2 at RDR-158 P4).

**Class M — migration-source machinery (ETL source reads, frozen-source
probes, chroma read legs). Bin: delete-with-feature** — deleted when the
RDR-155 P4b / migration-module window closes; reads sources, never a
destination: `db/t2/chash_etl.py` (1), `commands/storage_cmd.py` (1),
`migration/chroma_read.py` (2, the P4a.1-contract survivors),
`migration/guided_upgrade.py` (1), `migration/orchestrator.py` (1),
`migration/remap_cascade.py` (1), `migration/vector_etl.py` (1).

**Class O — own-substrate connects (the incident class). Bin: per the
DDL rows above**: `migration/wire_reid.py` (1 → D2, P1),
`upgrade_ladder/completion.py` (1 → D3, P2), `pipeline_buffer.py`
(1 → engine-host per Hal 2026-07-18, P3).

**Class V — legacy non-service Voyage embed paths (non-SQLite; the
epsilon token covers other storage-boundary axes too). Bin:
delete-with-feature** (self-declared "Phase-4 deletion target"):
`doc_indexer.py` (1), `indexer.py` (1), `commands/collection.py` (1).
These block the D6 token retirement until deleted — tracked, not SQLite
debt.

**Class N — census noise. Bin: not-actually-debt**:
`storage_boundary_lint.py` (10, self-referential token definitions).

### Q3 resolution (FTS5 outside the seven domains)

Audited 2026-07-18 (`grep -rn "fts5|FTS5|CREATE VIRTUAL"` over
`src/nexus` excluding `db/t2/`): **no FTS5 dependency grew outside the
seven domains.** The only FTS5 DDL lives in `db/migrations.py`
(`memory_fts`, `plans_fts`) and `db/t2/catalog.py` (catalog FTS, D5) —
all in-scope-158. Every out-of-domain reference (health integrity probe,
doctor checks, plan matcher fallback, T1 overlap detection, MCP search)
is a read-side CONSUMER of those in-scope tables and converts with them
under RDR-152's locked FTS5→tsvector parity contract. Q3 CLOSED.

### Q6 resolution (cold-start state audit)

Traced 2026-07-18 (`commands/init.py` + RF-186-2's doctor/init trace):
the precise pre-first-engine-start state set on a fresh machine is
(i) `config.yaml` + `.env` credentials — config; (ii) downloaded
artifacts: PG bundle binaries, embedding models — re-downloadable
caches, not data; (iii) logs. `nx init` provisions the PG cluster
itself (that is engine bring-up, not client data); it never touches the
ladder, and `nx doctor` never opens the completion store
(`health.py:2878-2914`, detect-only). **All pre-engine state is config
or re-downloadable artifact — no client data store is needed before the
first engine start.** Post-retirement, any pre-engine T2 access becomes
engine-gated, which `_converge_preconditions()` already enforces on the
normal path. Q6 CLOSED.

### Gap 1 scanner blind spots — harden-vs-accept

**DECIDED by Hal 2026-07-18** (activates bead `.2`):

- **HARDEN the `ALTER TABLE` blindness** — add an `ALTER_CENSUS`
  (per-file counts; live 2026-07-18: 59 statements across 11 files, the
  largest `db/migrations.py` at 36). Cheap (same regex-census shape) and
  closes the exact incident class: schema growth on an existing censused
  store is precisely how the reverted `leg_convergence` near-miss would
  have landed on a second attempt.
- **ACCEPT the bare-`sqlite3.connect` blindness** — already covered on a
  different axis: `storage_boundary_lint.py` ratchets the count of
  epsilon-allow'd connect sites, and the epsilon census above freezes
  the override population; a new connect needs a new override, which
  fails `test_no_new_epsilon_allows`.

## Research Findings

### RF-186-1 (VERIFIED, agent analyst-186-q2): the Gap-4 pin is behavioral and substrate-agnostic — D2's stored delivery fact is NO-GO in PG too; live membership computation is the compliant design

A stored `leg_convergence` "converged at T" row consulted by rung detect()
to skip the live probe collides with RDR-185's Gap-4 pin
(`test_gap4_two_mechanisms.py::test_rung_convergence_is_re_derived_live_never_cached`)
regardless of substrate: SQLite→PG changes storage, not epistemics. The
principled line, adjudicated with evidence: **raw facts a rung's detect()
interprets against the live world are fine** (the chash_remap map already
passes — consulted at `substrate_etl.py:1093`/`:1258` via
`_default_unreflected:776` as an inert input to a live computation);
**persisted verdicts consulted instead of re-deriving are banned**
(discriminator: the pinned test's "answer follows the world in BOTH
directions"). The "verdict wearing a fact costume" steelman is confirmed
for the marker (co-resident partial rollback → reads converged forever).

Recommended and adopted (D2 revised): engine-side live membership
computation over target chunk tables ⋈ PG `chash_remap` (RDR-154 style,
indexed SQL, no stored verdict), with rollback clearing the leg's
`chash_remap` rows so delivered-then-rolled-back and never-delivered
collapse to the same live state. Simultaneously: pin-compliant, NO-SQLITE
compliant, resolves nexus-ixl85's probe cost for this rung, and is the
real nexus-tidtd fix. Rejected alternatives scored in T2: amend-pin
(reopens the slammed door), probe-always-fact-as-witness (reopens ixl85 in
full), fold-into-completion-store (detect() never reads completion
records; wiring it would invert the level-triggered mechanism). Full
analysis: T2 `nexus/research-rdr186-q2-gap4-reconciliation`; registered as
`nexus_rdr/186-research-1`.

### RF-186-2 (VERIFIED, agent analyst-186-q1): every rung's completion is re-derivable engine-down — ladder.db is retirable per D3, no falsification found

Per-rung: t2-schema needs no engine at all (raw local `sqlite3` against
`memory.db`; refuses service mode) — re-derivable unconditionally.
substrate-etl needs the engine only to converge; engine-down its
detect()/verify() honestly report not-converged (never a false positive)
and no completion record is written in that window. The completion store
is pure bookkeeping: the runner's only production read
(`runner.py:221` `verified_rungs()`) skips a redundant verify() when
detect() already re-derived converged — losing the store costs one extra
cheap read-only verify() per rung, never a re-converge, and BOTH loss
scenarios are already mechanically pinned
(`test_ladder_runner.py::test_crash_between_converge_and_record_heals_on_next_run`,
`::test_recorded_rung_that_goes_pending_again_reconverges`). `nx doctor`
never opens the store (`health.py:2878-2914`, detect-only); `nx init`
never touches the ladder. Suspects cleared: `apply_attempted` is an
in-process constructor flag (`upgrade.py:108`), never read from
ladder.db; cascade-repair's `_unreflected()` reads
chash_remap/catalog/memory, never ladder.db. The watermark store
(`verify_fill_watermarks.json`) is ALREADY flat JSON — the D3-blessed
fallback shape, not SQLite, no census adjudication needed; it holds the
one real replay cost (~900s/90k-chunk collection if lost). Bootstrap:
`_converge_preconditions()` runs before `_run_ladder()` in the same
invocation, so the engine is normally up before any flush; the
engine-defer window degrades to redundant cheap verify() calls, never
data loss. P2 obligations: the in-process holder must serve
`verified_rungs()` for later rungs within the same walk;
`verified_at`/`package_version` audit metadata is observability-only,
accepted lossy. Full analysis: T2
`nexus/research-rdr186-q1-rung-rederivability`; registered as
`nexus_rdr/186-research-2`.
