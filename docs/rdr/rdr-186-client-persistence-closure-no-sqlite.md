---
title: "Client-Persistence Closure: PG in Every Mode — Retire Every SQLite Substrate Outside RDR-158's Seven-Domain Scope, and End Self-Granted Exemptions"
id: RDR-186
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: pending
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

### The gaps, named

- **Gap A — scope hole.** RDR-158 governs the seven-domain stores. Any store
  outside that list (ladder, chash-remap, pipeline buffer, catalog local
  file, ad-hoc tables in `nexus.db`) can claim it is not covered. The
  directive admits no such reading; this RDR is where the *complete*
  inventory gets adjudicated.
- **Gap B — bootstrap ordering.** The upgrade ladder's completion store
  exists "before the t2-schema rung it records"; migration artifacts must
  survive engine absence and mid-install crashes. Retiring these is a design
  problem (what persists when the engine is down?), not a sed. This is the
  *one* honest argument the exemption comments contain, and it deserves a
  real answer instead of a comment.
- **Gap C — stray data outside every migration path.** `aspect_promotion_log`
  (`aspect_promotion.py:290`, created lazily "so we avoid yet another T2
  migration entry") and `_nexus_t3_steps` (`commands/upgrade.py:687`, created
  inline mid-command) live in `nexus.db` but are registered in **no**
  migration registry and **no** ETL. RDR-158 P4 deleting the SQLite schema
  would silently drop their data.
- **Gap D — enforcement.** Until 2026-07-18 nothing mechanically resisted new
  SQLite. `tests/test_no_new_sqlite.py` (commit `54f7bd65`) now freezes
  per-file counts of inline SQLite DDL (15 files) and `# epsilon-allow:`
  overrides (43 files); growth fails. The freeze stops accretion; this RDR
  supplies the *ratchet to zero*.

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

**D2 — Migration artifacts go to PG via the already-planned surfaces.**
`chash_remap.db` → the RDR-185 .16 PG `chash_remap` table (Liquibase,
tenant/RLS), with the local file demoted to migration *source* and read-only.
The nexus-tidtd per-leg delivery/convergence fact lands in the same Liquibase
surface (a `leg_convergence` relation or columns on `chash_remap`'s
aggregate) — resolving the tidtd design fork *inside* the sanctioned
mechanism instead of a new client table. Note the Gap-4 two-mechanism pin
(RDR-185, `test_gap4_two_mechanisms.py`) constrains where the *verdict*
lives; research must reconcile "delivery fact in PG" with that pin
explicitly, in writing.

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
   demotion, rides-RDR-158-P3/P4}, each row carrying its data-carry plan and
   its gate. Deliverable: the adjudication table appended to this RDR,
   gated + accepted — this is where "exceptions are Hal's decisions" becomes
   literal.
2. **P1 — chash_remap to PG (engine-side, second release lifecycle).** The
   .16 Liquibase changeset + bulk-remap endpoint; `wire_reid` writes through
   the engine; the local file becomes read-only source. Includes the
   nexus-tidtd delivery-fact relation (D2) and unblocks the real tidtd fix
   (membership-probe convergence with a PG-persisted proof).
3. **P2 — Ladder state derive-first (D3).** Prove or falsify pre-engine
   re-derivability per rung; implement the PG flush; retire `ladder.db` or
   demote to flat file per the research outcome.
4. **P3 — Strays (D4) + pipeline buffer.** Liquibase changesets + data carry
   for what survives; explicit deletion records for what does not.
   `pipeline_buffer.py` adjudicated in P0 (its PDF-pipeline feature may be
   engine-hosted per RDR-173 lineage or retired).
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

- **Q1 (P2-blocking):** Is every rung's completion genuinely re-derivable
  with the engine down? Enumerate rungs; the t2-schema rung's
  `apply_attempted` report-mode and the substrate-etl rung's cascade-repair
  path are the suspects. What is the worst-case re-derivation cost on the
  resume path (the RDR-178 watermark interplay)?
- **Q2 (P1-blocking):** Does the delivery-fact-in-PG design collide with the
  Gap-4 two-mechanism pin the way the SQLite marker did, or does living
  inside the engine's substrate (queried by rung detect(), not cached beside
  it) satisfy the pin's "no freestanding verdict" language? Needs an explicit
  written reconciliation + pinned-test docstring update if scope is amended.
- **Q3:** FTS5 in local mode — the seven-domain retirement inherits RDR-152's
  locked FTS5→tsvector parity contract; confirm nothing outside those
  domains grew an FTS5 dependency (memory_store FTS is in-scope-158; anything
  else?).
- **Q4:** `pipeline_buffer.py` — is the streaming-PDF resume feature (RDR-048
  lineage) still load-bearing post-RDR-173, or retirable outright?
- **Q5:** Does the engine expose (or need) a generic small-state KV surface
  for client bookkeeping, or does each artifact get a first-class relation?
  (Bias per RDR-154: first-class relations with real schemas; a KV bucket is
  the SQLite hybrid wearing a PG costume.)
- **Q6:** Windows/cold-start: `nx` invoked before any engine has EVER been
  installed (fresh machine, first run) — what is the precise set of state
  the CLI may need before the first engine start, and is all of it config
  (yaml) rather than data?

## Research Findings

(pending — populate via /conexus:rdr-research)
