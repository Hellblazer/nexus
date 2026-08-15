# RDR-191 Phase 5 Cloud FK — Operator Runbook

**HISTORICAL — Phase 5 EXECUTED (deployed engine-service-v0.1.76, both FKs
convalidated=true, dangling=0 all tenants) and RDR-191 Phase 6 (bead
nexus-o8dil.33, THE SUBTRACTION, 2026-08-15) has since RETIRED the
`manifest-verify` apparatus this runbook's verify instrument
(`collections_checked` / `nx catalog manifest-verify_all`) depended on —
`nexus.manifest_orphans(dim)`, `nexus.manifest_verify_all()`, and
`nexus.manifest_backfill()` are DROPPED (`catalog-030-retire-manifest-
verify.xml`); the `_manifest_verify_list` CLI (`nx catalog manifest-verify
--list`) referenced below no longer exists. This document is preserved
AS-IS for the historical record of the executed deploy — do not follow its
verify-instrument steps against a current install. `nexus.manifest_
verify(text)` (the single-document, non-corpus-wide form) is the ONE
function this runbook references that was NOT dropped — it remains live
server-side for `CatalogRepository.completeIndexRun`'s internal use only.**

Status: **PREP, not yet executable.** Drafted 2026-08-14 against bead
`nexus-o8dil.31` ("RDR-191 P5: add the FK on CLOUD"). This bead cannot close
until `nexus-o8dil.29` (the local FK changeset) lands and GATE-5-CLOUD
(`nexus-o8dil.30`) is satisfied — see "Blocking dependencies" below. This
document is the turnkey procedure so the cloud leg is a read-and-execute job
once the Phase 5 engine tag ships, not a re-derivation.

Scope: `docs/rdr/rdr-191-unify-chunk-tables-enable-manifest-fk.md` Phase 5
(the `catalog_document_chunks -> nexus.chunks` FK), amendments (x) and (xi),
findings F12d/F15/F16/F16b/F17. Decision of record for the three-step shape:
T2 `nexus/rdr-191-validate-placement-decision` [22557].

## What Phase 5 actually ships (read this before running anything)

Per amendment (xi) / the o8dil.23 decision, the FK is **not** a bare
`ADD CONSTRAINT ... VALIDATE`. It is a three-step Liquibase sequence, all
three run unconditionally at engine boot, in order:

1. `ADD CONSTRAINT ... NOT VALID` — cheap, catalog-only (F9: 0.8 ms measured
   over 280,695 LOCAL manifest rows).
2. **Anti-join remediation changeset**: `DELETE FROM nexus.catalog_document_chunks`
   for rows whose `(tenant_id, collection, chash)` has no matching row in
   `nexus.chunks`. This is the F17 shape — an anti-join against the chunk
   store, never a row-count comparison (`nx catalog reconcile` cannot fix
   this population; it reasons about counts, and this damage is
   count-preserving by construction). The changeset MUST log the deleted row
   count to engine logs (structured, greppable) — this is the auditable
   record of an otherwise-silent, unattended DELETE.
3. `VALIDATE CONSTRAINT` — F9: 333 ms measured over 280,695 LOCAL manifest
   rows. Cloud's manifest population is larger (cloud carries more
   collections than the local dev corpus — 95 per the v0.1.75 client-path
   gate) so expect this to scale up, but it remains sub-second to
   low-single-digit-seconds order of magnitude; there is no index build on
   this path.

Because remediation is now IN the changelog (step 2), every upgrader — the
first deployment or a later one with its own accumulated dangling
population — is remediated in the same boot that validates. The **manual
census below is no longer the gating mechanism**; it is demoted to an
operator-side pre-deletion measurement and record (amendment xi, explicit:
"a box that skips them no longer bricks; it loses only the pre-deletion
measurement"). Skipping it does not endanger the deploy. Skipping it means
you have no record of what the changeset is about to (or did) delete before
it happens, which this runbook exists to prevent.

## Blocking dependencies (why this bead cannot close yet)

- `nexus-o8dil.29` — the LOCAL FK changeset (same three-step shape), IN
  PROGRESS at draft time. The Liquibase changelog this runbook's Section 2
  refers to does not exist until `.29` authors it.
- `nexus-o8dil.21` — GATE-4 co-release. **Already CLOSED** (engine-service-v0.1.75
  shipped 2026-08-14, Phase 4 migration only — the chunk-table unification,
  NOT the manifest FK). The Phase 5 FK changesets ship in a **later, separate**
  engine tag; `.21`'s closure does not mean Phase 5 is deployable. Track the
  Phase 5 tag version separately — see Placeholder 1 below.
- `nexus-o8dil.24` — adds `collections_checked` to `_manifest_verify_list`'s
  JSON payloads (both clean and damaged paths). IN PROGRESS at draft time.
  Section 1 below depends on this field existing; until it lands, the
  pre-deploy census can only report the human-readable string, which cannot
  be gated non-vacuously (F16b — a zero-collections-compared clean census
  exits 0 and looks identical to a genuinely clean one).
- `nexus-o8dil.30` — GATE-5-CLOUD itself, which this runbook's Section 3
  discharges. Depends on `.24`, `.27` (cloud dangling-manifest remediation —
  now largely subsumed by the in-changelog remediation per amendment xi, but
  its acceptance criteria, "run the census, remediate to zero, verify clean,"
  are exactly this runbook's Sections 1 and 3), and `.22` (Phase 4 cloud DDL,
  already executed).

**Placeholder 1 — the Phase 5 engine tag.** Unknown at draft time. The tag
that carries the FK changesets (`.29`'s local work, retargeted through the
same co-release discipline `.21` used for Phase 4) has not been cut. Every
`engine-service-vX.Y.Z` reference below is a placeholder until that tag
exists. Do not assume it is the immediate successor of v0.1.75 — Phase 6
(subtraction) and Phase 7 (`collection` NOT NULL) may or may not ship in the
same tag; check the epic (`nexus-o8dil`) at execution time.

## Section 1 — PRE-DEPLOY CENSUS

Run from a cloud-mode box, BEFORE the Phase 5 tag deploys. This is the
manual measurement amendment (xi) demotes to operator-side record; it is not
what makes the deploy safe (the changelog's own remediation does that), it
is what tells you, in advance, roughly how much the changelog is about to
delete — so the post-deploy logged-delete-count (Section 2) can be checked
against something instead of trusted blind.

1. **Run the verify and record BOTH the verdict and the count of collections
   compared:**

   ```bash
   nx catalog manifest-verify --list --json
   ```

   Record `collections_checked` from the JSON payload (lands with `.24`;
   until then, fall back to the human-readable "N collection(s) checked"
   string from the non-`--json` form and note in the record that the field
   was unavailable). A `collections_checked == 0` result is NOT a clean
   census — it is a census that checked nothing, per F16b; treat it as a
   FAILED pre-deploy step and re-run, do not proceed to Section 2 on it.

2. **Get the denominator from an INDEPENDENT source — never the census's own
   output** (this is the circularity R3/plan-audit note on `.24`/`.28`
   explicitly guards against). Named source, per the plan-audit resolution
   on `.28`:

   ```bash
   nx collection list
   ```

   Count the rows. This reads T3's collection registry directly
   (`_t3().list_collections()`), a different code path from
   `manifest_verify_all()`'s catalog-manifest enumeration — the two sources
   are structurally independent, not just two calls to the same function.
   Record this count as `known_collection_count`.

   **RLS caveat (see Section 5): this counts collections visible to the
   connecting credential's tenant only.** If cloud carries multiple tenants
   with catalog data (see Section 5), this step must be repeated once per
   tenant and the counts summed, or the comparison in step 3 below is
   silently scoped to one tenant while the FK changeset (which runs at
   engine boot with full DB access, not RLS-scoped) touches all of them.

3. **Compare.** `collections_checked` should equal `known_collection_count`
   (or the summed multi-tenant total). A mismatch is itself a finding — it
   means the census enumeration missed collections (unroutable model
   token, row-cap truncation reported in `incomplete_collections`) and the
   pre-deploy measurement is a LOWER BOUND, not a complete count. A third
   benign cause: a T3 collection with zero manifest rows is absent from
   `collections_checked` by construction (the census walks manifest rows).
   Remedy is operational, not code (nexus-61mkk, closed by directive
   2026-08-14): identify the stray collection, reindex or delete it,
   re-run. Record the mismatch explicitly; do not silently round it to
   "close enough."

4. **Record the damaged population** — this is the number Section 2's
   post-deploy logged-DELETE-count gets checked against. From the same
   `--json` output when the census is NOT clean: `total_rows` (dangling
   manifest row count) and the per-collection breakdown. Historical
   reference point, NOT a substitute for a fresh run: F15 measured this
   repo's cloud corpus on 2026-08-10 at 37 manifest rows / 36 distinct
   chashes / 4 documents across 4 collections
   (`code__1-6__voyage-code-3__v1`, `code__1-9__voyage-code-3__v1`,
   `docs__1-6__voyage-context-3__v1`, `knowledge__knowledge__...__v1`). That
   number is four days stale relative to this draft and predates the Phase 4
   chunk-table unification (engine-service-v0.1.75, 2026-08-14) — the
   unification copies existing chunks into `nexus.chunks`, it does not
   create the chunks these dangling rows are missing, so the same damage
   class should still be present, but re-run and do not assume the count is
   unchanged. Per the RDR: "This is a per-deployment gate, not a one-time
   fix... Cloud carries its own [population], unmeasured [here] — it must be
   RUN there, not inferred from here."

5. **Write the record to T2** before proceeding to the deploy:

   ```
   memory_put(project="nexus", title="rdr-191-p5-cloud-pre-deploy-census-<date>",
              content="<verdict, collections_checked, known_collection_count,
                        total_rows, per-collection breakdown, timestamp>")
   ```

## Section 2 — THE DEPLOY

**This instance does not drive the managed deployment.** Per standing
directive, the cross-instance deploy is surfaced as an explicit relay to
Hal, never framed as autonomous. The relay must name the exact tag, restate
the pre-deploy census result from Section 1, and ask for the deploy plus a
post-deploy re-gate — mirroring the [22485] `conexus-to-nexus` relay pattern
for the v0.1.75 Phase 4 deploy (T2 `conexus/conexus-to-nexus-engine-service-v0.1.75-DEPLOYED-phase4-migration-committed-2026-08-14`),
which is the house precedent for what a cloud migration relay looks like:
pre-step verified not assumed, structural verification of what actually
changed (not just "it booted"), row-count invariants stated exactly, and
explicit correction of any measurement error found along the way rather than
quietly fixing it.

**Relay content checklist (fill in before sending):**

- Exact tag: `engine-service-vX.Y.Z` (Placeholder 1, resolve before sending).
- Co-release contents: confirm this tag's `git log <prev-tag>..<candidate>`
  carries the FK changesets from `.29`'s retargeted work — paste, don't
  attest, per the GATE-4 acceptance discipline this RDR has already used
  twice.
- Section 1's census result (verdict, `collections_checked`,
  `known_collection_count`, `total_rows` if damaged) so the deploy relay
  carries the pre-deletion measurement, not just an instruction to deploy.
- Any pre-deploy superuser/GRANT steps the Phase 5 tag's own changesets
  require — check the tag's grants changesets the way v0.1.75's tag required
  `GRANT pg_monitor TO nexus_admin WITH ADMIN OPTION` before deploy; do not
  assume none exist without checking.
- Explicit statement that this is an **irreversible data migration** on the
  managed deployment (Section 4 below) — the same framing v0.1.75's relay
  used ("The irreversible one is done").
- Ask for the post-deploy artifacts needed for Section 3: the engine log
  excerpt showing the remediation changeset's logged delete-row-count, and
  confirmation the three changesets executed (by id) in the boot log.

**What happens at boot (informational, for interpreting the relay's
response):** Liquibase runs the three-step sequence unconditionally.
Step 2's DELETE fires against cloud's own dangling population (whatever
Section 1 measured, possibly drifted). Step 3's VALIDATE then scans a
population that step 2 just cleaned, so it should pass by construction —
this is the entire point of amendment (xi)'s design. If VALIDATE still
fails post-remediation, see Section 4 ("what a failed VALIDATE means").

**Harvest after the relay returns:** extract the remediation changeset's
logged delete-row-count from the engine logs the relay provides. Compare
against Section 1's `total_rows`. Record both in T2 alongside the deploy
outcome (title suggestion:
`rdr-191-p5-cloud-deploy-outcome-<date>`). A large discrepancy (deploy
deleted materially more or fewer rows than the pre-deploy census measured)
is a finding to file, not a footnote — the same standard `.22`'s acceptance
criteria applied to freeze-window duration misses.

## Section 3 — POST-DEPLOY VERIFICATION (discharges GATE-5-CLOUD)

Run from a cloud-mode box, immediately after the deploy relay confirms the
boot completed.

1. **FK exists and is validated.** Query the constraint's `convalidated`
   flag directly against the target table (RLS caveat: this is catalog
   metadata, `pg_constraint`, not tenant-scoped application data, so a plain
   read is fine here — do not conflate this with Section 5's per-tenant
   requirement for row-count queries).

   ```sql
   SELECT conname, convalidated
   FROM pg_constraint
   WHERE conrelid = 'nexus.catalog_document_chunks'::regclass
     AND contype = 'f';
   ```

   Expect `convalidated = true` for the new FK. If `false`, VALIDATE either
   did not run or failed — treat as a Section 4 event, do not proceed.

2. **Dangling count is 0.** Re-run the Section 1 anti-join measurement (or
   `nx catalog manifest-verify --list --json` again) and confirm `clean:
   true` with `collections_checked > 0`. Per Section 5, if cloud is
   multi-tenant this must be re-checked per tenant, not assumed from a
   single-tenant run.

3. **Manifest-verify re-run is clean AND non-vacuous.** This is GATE-5-CLOUD's
   literal acceptance text: `collections_checked` equal to the
   independently-sourced collection count from `nx collection list`
   (Section 1 step 2, re-run fresh — collection counts can have drifted
   since the pre-deploy census, especially across a deploy window).

4. **Client-visibility gate, engine-direct gates are not enough.** Run:

   ```bash
   tests/e2e/cloud-client-path-gate.sh
   ```

   from a cloud-mode box. Per nexus-bwulw precedent (three client features
   shipped green through every engine-direct gate while the public edge
   silently broke them — stubbed `/version`, auth-gated `/health`), a green
   engine-side VALIDATE proves nothing about what a real client sees through
   the public edge. This must be green before declaring GATE-5-CLOUD passed.

5. **Actual durations vs. estimate.** Record actual wall-clock time for the
   three-step sequence (from the engine boot log timestamps between the
   first and last of the three changeset ids) against the F9 baseline
   (0.8 ms NOT VALID + 333 ms VALIDATE, measured on 280,695 LOCAL manifest
   rows — a smaller population than cloud's). NOTE: F9's numbers are FK-cost
   only; they do not include step 2's remediation DELETE, which has no
   pre-existing timing estimate in the RDR at all — this is a genuine gap,
   record whatever step 2 actually takes as a fresh data point, not a
   deviation from an estimate that was never made for it. Separately, note
   the distinction from bead `.21`'s already-discharged actual-vs-estimate
   comparison (Phase 4's chunk-table migration: ~9m30s actual against a
   5-19 minute estimate, T2 `nexus/rdr-191-cloud-measurements` Part 2 /
   bead `.21` notes 2026-08-14) — that comparison belongs to a DIFFERENT
   operation (the chunk unification) and must not be conflated with this
   FK-add window's own timing.

6. Write the full verification record to T2:
   `memory_put(project="nexus", title="rdr-191-p5-cloud-post-deploy-verify-<date>")`.

## Section 4 — ROLLBACK PLAN

**Once the remediation DELETE (step 2) has executed, it is NOT reversible.**
State this plainly to whoever is authorizing the deploy: the deleted
manifest rows are gone from `catalog_document_chunks`. There is no
compensating INSERT, because the chunks those rows pointed at do not exist —
there is nothing to reconstruct them from. **Section 1's pre-deploy census
is the only record of what was deleted.** If a document's manifest looked
correct before the deploy and is missing rows after, Section 1's per-document
breakdown (`report["collections"][coll][doc_id]`) is the sole surviving
evidence of what those rows were (chash, position) — there is no database
backup step in this runbook; if cloud's standard backup/PITR posture covers
this window, that is the actual recovery path, not anything this runbook
performs.

This mirrors bead `.22`'s framing for the Phase 4 DDL: "CIC, no free
rollback" — this FK deploy has the same shape for a different reason. Phase
4's irreversibility came from `CREATE INDEX CONCURRENTLY` forfeiting
transactional rollback; this deploy's irreversibility comes from the
remediation DELETE being a genuine, intentional data deletion that the
Liquibase changeset does not (and per the o8dil.23 decision, should not)
wrap in a compensating rollback — deleting dangling manifest bookkeeping
rows is the fix, not a side effect to undo.

**What IS reversible:** the `ADD CONSTRAINT ... NOT VALID` step (step 1) and
`VALIDATE CONSTRAINT` (step 3) are ordinary constraint DDL — if the Phase 5
changelog as a whole needs to roll back before it ships (e.g. Section 3
verification fails hard), standard Liquibase rollback of steps 1 and 3
applies normally. It is specifically step 2's DELETE that is a one-way door,
and only once it has actually executed against the target database.

**What a failed VALIDATE means, given remediation precedes it (per amendment
xi, this should not happen by construction — treat any occurrence as a
finding, not an expected outcome):**

- The remediation changeset (step 2) is scoped to the exact damage class F17
  describes: dangling `catalog_document_chunks` rows with no matching
  `nexus.chunks` row, matched by the FK's own key `(tenant_id, collection,
  chash)`. If VALIDATE still fails after step 2, the surviving violation is
  a shape the anti-join did NOT catch — check first whether it is a
  `MATCH SIMPLE` NULL-collection row misclassified as damage (should be
  exempt, per F6/F12a) or a race: a NEW dangling row created between step 2's
  DELETE and step 3's VALIDATE by a still-live producer Phase 2 was supposed
  to have eliminated. The latter would mean GATE-2's "zero live producers"
  exit criterion was not actually met on cloud — treat as a P0 finding
  against Phase 2, not a Phase 5 bug.
- Under `FORCE ROW LEVEL SECURITY` with the non-owner `svc` role, F10d
  documents that PostgreSQL redacts the violation detail to "Key is still
  referenced from table..." with NO key values — the boot failure will not
  name the blocking row. Do not expect the raw Postgres error to be
  actionable on cloud. The diagnostic IS this query — the catalog-029-1
  anti-join as a SELECT (no product tooling exists or is needed; run it
  read-only via the `nexus_diag` path, per-tenant under the GUC per
  Section 5's RLS discipline):

  ```sql
  SELECT c.tenant_id, c.collection, encode(c.chash, 'hex') AS chash,
         c.doc_id, c.position
    FROM nexus.catalog_document_chunks c
   WHERE NOT EXISTS (
           SELECT 1 FROM nexus.chunks v
            WHERE v.tenant_id  = c.tenant_id
              AND v.collection = c.collection
              AND v.chash      = c.chash);
  ```

  Rows returned are the blocking population VALIDATE will name-redact.
  (Match this query's join against the shipped catalog-029-1 DELETE if the
  changeset has been amended since — the SELECT must mirror the DELETE's
  anti-join exactly or it diagnoses the wrong population.)
- Because step 3 failing bricks the engine boot (this is exactly the
  boot-brick amendment (xi) exists to prevent, so its occurrence means the
  prevention had a gap), the practical remediation is: fix the surviving
  producer or the anti-join's scope, re-tag, re-deploy. There is no
  in-place hotfix path — Liquibase changesets are immutable once released
  (checksum-pinned), so a fix means a NEW changeset in a NEW tag, not editing
  this one.

## Section 5 — RLS POSTURE

**`nexus_svc` runs `NOBYPASSRLS`.** Any verification SQL or CLI call in this
runbook that reads or counts rows in a tenant-scoped table
(`catalog_document_chunks`, `nexus.chunks`) is subject to row-level security
and will see ONLY the connecting session's current tenant, not the whole
deployment — this is the lesson recorded against `nexus-uuurl` (T2
`conexus` [22554]): a query written expecting an all-tenant answer, run once
under `nexus_svc`, silently returns a single-tenant answer with no error and
no visible truncation. The correct pattern, per that lesson, is a **per-tenant
loop under the appropriate tenant-scoping GUC, summed** — never a single
unscoped query presented as the deployment total.

**Where this bites in this runbook:**

- Section 1 step 1 (`nx catalog manifest-verify --list`) and step 2
  (`nx collection list`) both go through `HttpCatalogClient` /
  `HttpVectorClient`, authenticated as a specific tenant. If cloud carries
  more than one tenant with live catalog/collection data — confirmed
  multi-tenant per the `uuurl` finding: at least four tenants exist (`nexus`,
  `gate-xr789`, `smoke-2026-06-10`, `smoke-2nx`) — a single run from one
  operator credential covers only ONE of them. "The deployment's known
  collection count" (GATE-5-CLOUD's literal acceptance text) is a
  cross-tenant concept; a single-tenant run under-reports it, which would
  make Section 1 step 3's comparison pass vacuously (both numbers
  undercounting by the same missing tenants) rather than catch a real gap.
- **Open item, not resolved by this draft**: whether GATE-5-CLOUD's
  "deployment's known collection count" is meant to mean the `nexus`
  production tenant only (the other three read as test/gate corpora, per
  the `uuurl` close note: "gate-xr789's chunks... are the STEP-6 deploy-gate
  corpus") or the sum across all tenants. This runbook does not adjudicate
  that; whoever executes Section 1 must either confirm scope is
  single-tenant-by-design with Hal, or loop per tenant and sum, before
  treating step 3's comparison as meaningful. Flagging this explicitly
  rather than picking one silently, per the standing directive against
  unstated assumptions in a gate.
- Section 3 step 1's `pg_constraint` query is NOT subject to this caveat —
  catalog metadata (`pg_constraint`, `pg_class`) is not RLS-protected
  application data; a single connection sees the whole schema regardless of
  tenant. Only the row-level counts (dangling manifest rows, chunk rows,
  collection membership) need the per-tenant treatment.
- The remediation DELETE itself (Section 2, step 2 of the Liquibase
  sequence) runs as the migration role at boot, which is NOT RLS-scoped the
  way an application query is — Liquibase migrations run with elevated
  privilege by design. So the changeset correctly touches every tenant's
  dangling rows in one pass; it is only the OPERATOR-SIDE verification
  queries in Sections 1 and 3 that need the per-tenant discipline, not the
  changeset itself.

## Findings surfaced while drafting this runbook

1. **The Phase 5 engine tag does not exist yet** and GATE-4's closure
   (v0.1.75) is Phase 4 only — a reader skimming bead `.21`'s CLOSED status
   could mistakenly believe Phase 5 is also deployable. No RDR or bead text
   states this ambiguity explicitly; recorded here (Section "Blocking
   dependencies") and in the T2 write-back below.
2. **Section 2's remediation-DELETE step has no duration estimate anywhere
   in the RDR or the plan.** F9 prices `ADD CONSTRAINT NOT VALID` and
   `VALIDATE` only; the anti-join DELETE (amendment xi's step 2, the newest
   piece of the design) was never costed. Not a blocker — the row count is
   small (Section 1's historical reference: 37 rows) — but worth a bead or
   at minimum a note if a future deployment's dangling population turns out
   to be large enough that the DELETE itself becomes the dominant cost.
3. **Multi-tenant scope of "the deployment's known collection count" is
   undecided** (Section 5, open item). GATE-5-CLOUD's acceptance criterion
   as written does not disambiguate single-tenant vs. all-tenant, and the
   `uuurl` finding proves cloud genuinely has multiple tenants with catalog
   data. This should be resolved before Section 1 is executed for real, not
   discovered mid-census.
4. **F15's cloud measurement (37 rows, 2026-08-10) is the only real cloud
   number on file for this damage class, and it predates the Phase 4
   migration (v0.1.75, 2026-08-14).** It is unlikely the migration changed
   this population (it copies existing rows, does not fabricate missing
   chunks), but nothing in the RDR or bead trail explicitly re-confirms that
   assumption post-migration. Section 1 treats it as a stale reference point
   only, never a substitute for a fresh run — flagging the assumption here
   so it is not silently load-bearing.

## Cross-references

- RDR: `docs/rdr/rdr-191-unify-chunk-tables-enable-manifest-fk.md` — Phase 5
  (amendments x, xi), F12d, F15, F16, F16b, F17.
- Decision: T2 `nexus/rdr-191-validate-placement-decision` [22557].
- Plan: T2 `nexus/plan-rdr-191-phases-2-7` [22217].
- Cloud measurements (Phase 3/4): T2 `nexus/rdr-191-cloud-measurements` [22263].
- Deploy relay precedent: T2 `conexus/conexus-to-nexus-engine-service-v0.1.75-DEPLOYED-phase4-migration-committed-2026-08-14` [22485].
- RLS/per-tenant lesson: bead `nexus-uuurl` (CLOSED), T2 `conexus` [22554].
- Beads: `nexus-o8dil.29` (local FK), `.30` (GATE-5-CLOUD), `.31` (this
  runbook's bead), `.24` (`collections_checked`), `.27` (cloud remediation,
  largely subsumed by the in-changelog design), `.21` (GATE-4, CLOSED,
  Phase 4 only), `.22` (Phase 4 cloud DDL, CLOSED).
