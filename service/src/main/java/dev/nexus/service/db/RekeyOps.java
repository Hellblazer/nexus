/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_1024;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_384;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_768;

/**
 * RDR-180 Item6, engine half (nexus-jxizy.6): the per-tenant full-digest
 * rekey. Executed INSIDE the freeze window by the client rung via
 * {@code POST /v1/remap/rekey}; runs as nexus_svc under
 * {@link TenantScope#withTenant} (per-tenant RLS — the reason this is an
 * endpoint and not Liquibase DML, which silently sees zero rows under
 * FORCE RLS as the non-BYPASSRLS owner).
 *
 * <p>PREDICATE (design amendment 2, T2 nexus_rdr/180-engine-cohort-design-
 * amendments): a row needs rekeying when {@code chash IS DISTINCT FROM
 * sha256(chunk_text)} — digest-mismatch, NOT width. A width predicate
 * would miss 32-ASCII-char legacy ids that converted to exactly 32 bytes.
 * Idempotent by construction: a second run finds every content row equal
 * to its digest and no-ops.
 *
 * <p>ORDER, one transaction per tenant (single atomic cutover — RDR-180
 * Failure Modes: no dual-width window within a tenant):
 * <ol>
 *   <li>Conflict pre-check: one recovered old_ref mapping to two distinct
 *       digests fails LOUD (realized 128-bit collision / corpus
 *       corruption — mirrors the client build_content_map refusal).</li>
 *   <li>chash_alias build: (old_ref per the reversibility lemma,
 *       old_bytes, new digest) for every mismatched content row.</li>
 *   <li>Item8 disposition for empty-text rows: reference-only rows
 *       resolve through the alias built from content siblings; orphans
 *       get the per-run policy (drop cascades their manifest
 *       pointers in the same transaction; synthesize mints the
 *       deterministic surrogate and stamps
 *       {@code metadata.chash_origin='synthetic'}).</li>
 *   <li>Two-phase chunk rekey per dim (RDR-185 PK-collision-under-
 *       collapse): keep one row per (collection, digest) — preferring a
 *       row already AT the digest key — delete the rest (all aliased),
 *       then UPDATE survivors.</li>
 *   <li>Cascade via the alias: manifest (plain — chash not in its PK),
 *       topic_assignments (TEXT doc_id via old_ref match, two-phase),
 *       frecency (GREATEST-merge on collapse), relevance_log (plain).</li>
 * </ol>
 * VALIDATE of the octet CHECKs is deliberately NOT here: the client rung
 * runs it via the local admin connection after count-verify (table owner,
 * RLS-exempt scan).
 */
public final class RekeyOps {

    private static final Logger log = LoggerFactory.getLogger(RekeyOps.class);

    // Shared with StagingPromoteOps via ChashSqlIdioms (nexus-jxizy.10.2):
    // the digest formula, lemma, collapse keeper, frecency merge and verify
    // scans are single-homed there so the two chash movers cannot drift.
    private static final List<String> CHUNK_TABLES = ChashSqlIdioms.CHUNK_TABLES;

    /** Reversibility-lemma rendering of a converted key's original string. */
    private static final String OLD_REF = ChashSqlIdioms.OLD_REF_LEMMA;

    private static final String DIGEST = ChashSqlIdioms.DIGEST;

    private final TenantScope tenantScope;

    public RekeyOps(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /** Thrown when one legacy id maps to two distinct content digests. */
    public static final class RekeyConflictException extends RuntimeException {
        public RekeyConflictException(String message) {
            super(message);
        }
    }

    /**
     * Run the full rekey for *tenant*. {@code synthesizeOrphans} selects the
     * Item8 policy for orphaned empty-text rows (default caller: drop).
     * Returns the disposition + per-table counts (the auditable envelope).
     */
    // SANCTIONED RAW (nexus-jxizy.6, RawSqlGateTest allowlist): the rekey is
    // deliberately server-side SQL — sha256() over chunk_text, the
    // ctid/array_agg keeper idiom for two-phase collapse, and the
    // reversibility-lemma CASE expressions have no jOOQ DSL form; these are
    // one-shot freeze-window migration statements, never serving-path
    // queries.
    public Map<String, Object> rekey(String tenant, boolean synthesizeOrphans) {
        Map<String, Object> out = tenantScope.withTenant(tenant, ctx -> {
            Map<String, Object> counts = new LinkedHashMap<>();

            // Cross-path mutual exclusion (critic-p1 High): this writer and
            // StagingPromoteOps touch the SAME tables (chash_alias, chunks_*,
            // pointer stores) — sharing the 'staging:'||tenant advisory-lock
            // namespace serializes a mis-sequenced concurrent rekey against
            // any in-flight promote/finalize instead of interleaving under
            // READ COMMITTED (the GH #1390 class via a cross-endpoint path).
            ctx.execute("SELECT pg_advisory_xact_lock(hashtext('staging:' || "
                + "current_setting('nexus.tenant', true)))");

            // (1) conflict pre-check across all dims: same old_ref, two digests.
            Integer conflicts = ctx.fetchOne(
                "SELECT count(*) FROM ("
                + "  SELECT old_ref FROM ("
                + unionAllContentRows()
                + "  ) u GROUP BY old_ref HAVING count(DISTINCT new_chash) > 1"
                + ") q").get(0, Integer.class);
            if (conflicts != null && conflicts > 0) {
                throw new RekeyConflictException(
                    conflicts + " legacy id(s) map to more than one content digest "
                    + "(realized 128-bit collision or corpus corruption) — refusing "
                    + "to pick silently (GH #1390: correct addresses only)");
            }

            // (2) alias facts for every mismatched CONTENT row (all dims).
            int aliased = ctx.execute(
                "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                + "SELECT current_setting('nexus.tenant', true), old_ref, old_bytes, new_chash, source "
                + "FROM (" + unionAllContentRows() + ") u "
                + "ON CONFLICT (tenant_id, old_ref) DO NOTHING");
            counts.put("alias_rows", aliased);

            // (2a) UN-BLIND THE PLANNER before anything joins the rows we just
            // wrote. F2, production 2026-07-20: autoanalyze fires the moment
            // tenant 1 commits and freezes chash_alias statistics at "100%
            // tenant 1" (most_common_vals={t1}, freqs=[1.0], n_distinct=1).
            // Tenant 2's alias rows are inserted INSIDE this transaction and
            // are therefore invisible to the planner, so `tenant_id = 't2'`
            // estimates ONE row and Postgres picks a triple nested loop
            // against ~134k x 466k actual: 101 MINUTES, versus 461 seconds
            // once the estimate is right. An in-transaction ANALYZE samples
            // this transaction's own uncommitted rows, which is exactly the
            // property needed and the reason this cannot be deferred to a
            // post-commit maintenance pass.
            //
            // Every step below (Item8 disposition, the two-phase rekey, and
            // above all the step-5 cascades) joins chash_alias, so this sits
            // immediately after the INSERT rather than just before the
            // cascades.
            //
            // Requires MAINTAIN on the table for nexus_svc (grants-nexus-svc,
            // PG17+): Postgres does NOT error when a non-owner analyzes — it
            // WARNs and SKIPS, so an ungranted ANALYZE is a silent no-op that
            // leaves the planner exactly as blind. The outcome therefore rides
            // the envelope instead of being assumed; RekeyOpsIntegrationTest
            // asserts the resulting statistics, never the statement.
            counts.put("alias_stats_refreshed", ChashSqlIdioms.refreshAliasStats(ctx));

            // (2b) nexus-t76bp REWORK (critic-p1 Critical, T2 nexus/critique-
            // t76bp-rekey-gate-2026-08-08 [21807]): round 1 of this bead
            // gated ONLY step 5's manifest UPDATE. The critic traced the
            // full method body and found steps 3 (Item8 disposition) and 4
            // (two-phase content rekey — the step that makes rekeyed
            // content physically live under its NEW digest) ran entirely
            // UNGATED, for a span the class javadoc itself documents as
            // potentially MINUTES at real scale (F2: 101min pre-fix). A
            // concurrent, unrelated document's ordinary sweep sharing that
            // exact (collection, chash) pair could delete the physical
            // content during that window; step 5's then-only gate-resolution
            // query ran AFTER the damage, correctly found nothing left to
            // protect, and its UPDATE repointed the manifest unconditionally
            // — a silent dangling reference. Fixed per the critic's
            // Option 1: acquire every target-collection gate HERE, before
            // step 3 begins, using the alias map step 2 just built, and hold
            // through step 5's commit (xact-scoped advisory locks release
            // only at commit/rollback — no explicit release needed).
            //
            // COST (code-review-expert T2 review-11gh6-gate-2026-08-08
            // [21797], T76BP REWORK DELTA D3): holding these gates SHARED
            // from here through commit means every concurrent sweep on a
            // touched collection fails open to sweep_skipped (39upx
            // accounting) for this rekey's FULL duration — potentially
            // minutes at real scale (F2: 101min pre-fix; 461s post-fix).
            // This is the deliberate tradeoff (finite writer-side stall,
            // sweeps simply retry later), not an accident.
            //
            // Resolved via `old_bytes`, not `new_chash`: at THIS point in
            // the method no physical row has been rekeyed yet, so a
            // `new_chash` join (the shape round 1 used, correct only
            // immediately before step 5) would find nothing. `old_bytes` is
            // every mismatched row's CURRENT physical chash by construction
            // of step 2's INSERT — and it is coextensive with what steps 3
            // (Item8's `reference_only_resolved`, which also joins on
            // `a.old_bytes = c.chash`) and 4 (`ChashSqlIdioms.
            // contentCollapseDelete`/`contentRekeyUpdate`, whose predicate
            // is exactly step 2's alias-generating predicate) are about to
            // touch: neither the collapse phase nor the rekey phase ever
            // changes a physical row's `collection` column, only its
            // `chash` — so "every collection currently holding a row at
            // `old_bytes`" IS "every collection steps 3-4 are about to
            // mutate," by construction, not by proxy. (A collapse-loser
            // that is already AT the digest key, not itself mismatched, is
            // never gate-relevant on its own: a collapse GROUP only forms
            // when count(*) > 1 for a (collection, digest) pair, which
            // requires at least one MISMATCHED sibling in that same group
            // — so the group's collection is always reachable via that
            // sibling's `old_bytes`, never missed.)
            //
            // NAMED RESIDUAL (not closed by this query — stating exactly
            // what remains open, matching the kl2z6/document_restore/
            // promoteCollection discipline rather than claiming closure):
            // step 3's ORPHAN-SYNTHESIZE sub-branch (below) mints alias
            // rows for rows that, by `orphanCond`'s own definition, have NO
            // alias fact yet at this point — so their collections are NOT
            // resolved by this query and are NOT gated by it. What
            // actually protects that sub-branch is a DIFFERENT mechanism:
            // the synthesized surrogate chash exists only on rows this
            // transaction itself UPDATEs, which stay row-locked through
            // commit — a racing sweep's DELETE blocks on the row lock and
            // then EvalPlanQual-rechecks its quals against the new row
            // version, matching zero rows. The step-6 abort-on-nonzero
            // below is the BACKSTOP, not the shield for this residual: it
            // detects pre-existing corruption and races committed before
            // its own snapshot, and structurally cannot see a sweep that
            // commits after this transaction (critic Option 2, deliberately
            // COMBINED with Option 1 here rather than substituted for it —
            // prevention where structural, detection where prevention is
            // impossible).
            // jOOQ DSL rendering (nexus-t76bp representation-only pass, Hal
            // directive 2026-08-08): identical to the prior raw SQL —
            // DISTINCT phys.collection, inner JOIN chash_alias to the
            // three-dim UNION ALL on phys.chash = a.old_bytes. No bind
            // values in this statement (no user input reaches it either
            // way); the rewrite is about typed columns and generated
            // tables, not injection risk.
            var phys = ctx.select(CHUNKS_384.COLLECTION, CHUNKS_384.CHASH).from(CHUNKS_384)
                .unionAll(ctx.select(CHUNKS_768.COLLECTION, CHUNKS_768.CHASH).from(CHUNKS_768))
                .unionAll(ctx.select(CHUNKS_1024.COLLECTION, CHUNKS_1024.CHASH).from(CHUNKS_1024))
                .asTable("phys");
            var targetCollections = ctx.selectDistinct(phys.field("collection", String.class))
                .from(CHASH_ALIAS)
                .join(phys).on(phys.field("chash", byte[].class).eq(CHASH_ALIAS.OLD_BYTES))
                .fetch();
            for (String c : targetCollections.stream()
                    .map(r -> r.get(0, String.class))
                    .filter(name -> name != null && !name.isBlank())
                    .sorted()
                    .distinct()
                    .toList()) {
                CatalogRepository.acquireSweepGateShared(ctx, tenant, c);
            }

            // (3) Item8: empty-text rows. Reference-only rows resolve through
            // the alias just built from content-bearing siblings; the rest are
            // orphans under the per-run policy.
            int refResolved = 0;
            int orphansDropped = 0;
            int orphansSynthesized = 0;
            for (String t : CHUNK_TABLES) {
                refResolved += ctx.execute(
                    // chunk_text_hash mirrors the resolved key (same RDR-086
                    // parity as contentRekeyUpdate — critic-1010).
                    "UPDATE " + t + " c SET chash = a.new_chash, "
                    + "metadata = coalesce(c.metadata, '{}'::jsonb) "
                    + "  || jsonb_build_object('chunk_text_hash', encode(a.new_chash, 'hex')) "
                    + "FROM nexus.chash_alias a "
                    + "WHERE c.chunk_text = '' "
                    + "  AND a.old_bytes = c.chash "
                    + "  AND c.chash IS DISTINCT FROM a.new_chash "
                    // two-phase guard: skip if the resolved key already exists
                    // in this collection (shared-content collapse — the row is
                    // a duplicate reference; delete instead below).
                    + "  AND NOT EXISTS (SELECT 1 FROM " + t + " k "
                    + "        WHERE k.collection = c.collection AND k.chash = a.new_chash)");
                // duplicate reference rows whose resolved key already exists
                ctx.execute(
                    "DELETE FROM " + t + " c USING nexus.chash_alias a "
                    + "WHERE c.chunk_text = '' AND a.old_bytes = c.chash "
                    + "  AND EXISTS (SELECT 1 FROM " + t + " k "
                    + "        WHERE k.collection = c.collection AND k.chash = a.new_chash)");
                // ORPHAN CRITERION (width-free — the same 32-byte-ASCII
                // blindspot fix as the rekey predicate): an empty-text row is
                // an orphan when NO alias fact covers its key AND no
                // content-bearing row anywhere shares that key (a same-key
                // content sibling makes it a legitimate reference — either
                // already-canonical, needing no change, or legacy, resolved
                // via the alias above).
                String orphanCond =
                    "c.chunk_text = '' "
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chash_alias a "
                    + "        WHERE a.old_bytes = c.chash) "
                    // a row already AT an aliased NEW key is a reference the
                    // step-3a resolve just produced (content rows still hold
                    // their OLD keys until step 4) — never an orphan.
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chash_alias a2 "
                    + "        WHERE a2.new_chash = c.chash) "
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_384 k "
                    + "        WHERE k.chash = c.chash AND k.chunk_text <> '') "
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_768 k "
                    + "        WHERE k.chash = c.chash AND k.chunk_text <> '') "
                    + "  AND NOT EXISTS (SELECT 1 FROM nexus.chunks_1024 k "
                    + "        WHERE k.chash = c.chash AND k.chunk_text <> '')";
                if (synthesizeOrphans) {
                    // Alias the surrogates FIRST so the step-5 cascade
                    // repoints their surviving pointers (RDR-180 Failure
                    // Modes: a preserved pointer must follow the surrogate,
                    // never dangle at the old key).
                    ctx.execute(
                        "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                        + "SELECT current_setting('nexus.tenant', true), "
                        + String.format(OLD_REF, "c.chash") + ", c.chash, "
                        + "  sha256(convert_to("
                        + "    'nexus:synthetic-chash:v1|' || current_setting('nexus.tenant', true) "
                        + "    || '|' || c.collection || '|' || " + String.format(OLD_REF, "c.chash")
                        + "    , 'UTF8')), '" + t + ":synthetic' "
                        + "FROM " + t + " c WHERE " + orphanCond + " "
                        + "ON CONFLICT (tenant_id, old_ref) DO NOTHING");
                    orphansSynthesized += ctx.execute(
                        "UPDATE " + t + " c SET "
                        + "  chash = a.new_chash, "
                        // chunk_text_hash mirrors the SURROGATE key (RDR-086
                        // parity, critic-1010 — same as the staging synthesize).
                        + "  metadata = coalesce(c.metadata, '{}'::jsonb) "
                        + "             || jsonb_build_object('chash_origin', 'synthetic', "
                        + "                  'chunk_text_hash', encode(a.new_chash, 'hex')) "
                        + "FROM nexus.chash_alias a "
                        + "WHERE c.chunk_text = '' AND a.old_bytes = c.chash "
                        + "  AND a.source = '" + t + ":synthetic' "
                        + "  AND c.chash IS DISTINCT FROM a.new_chash");
                } else {
                    // drop: cascade the manifest pointers FIRST
                    // (same transaction — RDR-180 Failure Modes: dangling
                    // manifest pointer), then the orphan rows.
                    ctx.execute(
                        "DELETE FROM nexus.catalog_document_chunks m USING " + t + " c "
                        + "WHERE m.chash = c.chash AND " + orphanCond);
                    // (chash_index cascade RETIRED — RDR-187/nexus-piwya.9:
                    // the router table is dropped at boot, before any rung
                    // runs; there is no router row left to cascade.)
                    orphansDropped += ctx.execute(
                        "DELETE FROM " + t + " c WHERE " + orphanCond);
                }
            }
            counts.put("reference_only_resolved", refResolved);
            counts.put("orphans_dropped", orphansDropped);
            counts.put("orphans_synthesized", orphansSynthesized);

            // (4) two-phase content rekey per dim.
            int collapsed = 0;
            int rekeyed = 0;
            for (String t : CHUNK_TABLES) {
                // phase A: delete collapse-losers. Keeper per (collection,
                // digest): a row already AT the digest key wins, else min ctid.
                collapsed += ctx.execute(ChashSqlIdioms.contentCollapseDelete(t));
                // phase B: rekey survivors whose key mismatches their digest.
                rekeyed += ctx.execute(ChashSqlIdioms.contentRekeyUpdate(t));
            }
            counts.put("collapsed_duplicates", collapsed);
            counts.put("rehashed", rekeyed);

            // (5) cascades via the alias map.
            // manifest: chash not in its PK — plain rewrite.
            //
            // nexus-t76bp (RekeyOps UPDATE-verb catalog_document_chunks
            // writes were outside the sweep gate — same defect class as
            // nexus-11gh6, different SQL verb; REWORKED per critic-p1
            // Critical, T2 nexus/critique-t76bp-rekey-gate-2026-08-08
            // [21807]): every target collection's sweep gate was already
            // acquired at step (2b) above, BEFORE steps 3-4 ran, and is
            // held continuously through this statement's commit (xact-
            // scoped advisory locks release only at commit/rollback). See
            // step (2b)'s comment for the freeze-window investigation, why
            // the resolution moved there and switched to an `old_bytes`
            // join, and the named orphan-synthesize residual that a wider
            // gate does NOT reach: that sub-branch is shielded by
            // row-locks-through-commit + EvalPlanQual (see step (2b)'s
            // comment), with the step-6 abort below serving only as the
            // BACKSTOP for pre-existing corruption and pre-snapshot races,
            // not as the mechanism that covers it.
            counts.put("manifest_repointed", ctx.execute(
                "UPDATE nexus.catalog_document_chunks m SET chash = a.new_chash "
                + "FROM nexus.chash_alias a "
                + "WHERE m.chash = a.old_bytes AND m.chash IS DISTINCT FROM a.new_chash"));
            // (chash_index two-phase repoint RETIRED — RDR-187/nexus-piwya.9:
            // the router table is dropped at boot, before any rung runs.
            // The two-collapse-direction idiom it pioneered — the
            // RekeyOpsIntegrationTest 3c catch — lives on in the
            // topic_assignments block below.)
            // topic_assignments: TEXT doc_id matches old_ref; PK
            // (tenant, doc_id, topic_id) — two-phase in both collapse
            // directions (the RekeyOpsIntegrationTest 3c idiom).
            ctx.execute(
                "DELETE FROM nexus.topic_assignments ta "
                + "USING nexus.topic_assignments tb, nexus.chash_alias aa, nexus.chash_alias ab "
                + "WHERE aa.old_ref = ta.doc_id AND ab.old_ref = tb.doc_id "
                + "  AND aa.new_chash = ab.new_chash "
                + "  AND ta.topic_id = tb.topic_id "
                + "  AND tb.ctid < ta.ctid");
            ctx.execute(
                "DELETE FROM nexus.topic_assignments ta USING nexus.chash_alias a "
                + "WHERE ta.doc_id = a.old_ref "
                + "  AND EXISTS (SELECT 1 FROM nexus.topic_assignments k "
                + "        WHERE k.topic_id = ta.topic_id "
                + "          AND k.doc_id = encode(a.new_chash, 'hex'))");
            counts.put("topic_assignments_repointed", ctx.execute(
                "UPDATE nexus.topic_assignments ta SET doc_id = encode(a.new_chash, 'hex') "
                + "FROM nexus.chash_alias a WHERE ta.doc_id = a.old_ref"));
            // frecency: PK (tenant, chunk_id) — GREATEST-merge on collapse
            // (the RDR-185 _FRECENCY_MERGE_SQL semantics, PG port), covering
            // BOTH collapse directions via a per-target group aggregate over
            // every matching old row (3c catch: two olds, no target row).
            // keeper keyed by min(chunk_id), NOT ctid: an UPDATE rewrites
            // the row and changes its ctid, so ctid-based keeper selection
            // goes stale across statements (the 3c "expected 5 was 2" catch).
            String frecencyAgg = ChashSqlIdioms.frecencyAliasAggregate();
            // (i) an existing row AT the target absorbs the whole group.
            ctx.execute(
                "UPDATE nexus.frecency t SET "
                + "  frecency_score = GREATEST(t.frecency_score, g.fs), "
                + "  miss_count     = GREATEST(t.miss_count,     g.mc), "
                + "  last_hit_at    = GREATEST(t.last_hit_at,    g.lh), "
                + "  embedded_at    = GREATEST(t.embedded_at,    g.ea), "
                + "  ttl_days       = GREATEST(t.ttl_days,       g.td) "
                + "FROM " + frecencyAgg + " "
                + "WHERE t.chunk_id = encode(g.new_chash, 'hex')");
            ctx.execute(
                "DELETE FROM nexus.frecency f USING nexus.chash_alias a "
                + "WHERE f.chunk_id = a.old_ref "
                + "  AND EXISTS (SELECT 1 FROM nexus.frecency k "
                + "        WHERE k.chunk_id = encode(a.new_chash, 'hex'))");
            // (ii) no target row: the min-ctid keeper absorbs the group,
            // the other olds are deleted (the keeper is renamed below).
            ctx.execute(
                "UPDATE nexus.frecency f SET "
                + "  frecency_score = g.fs, miss_count = g.mc, "
                + "  last_hit_at = g.lh, embedded_at = g.ea, ttl_days = g.td "
                + "FROM " + frecencyAgg + ", nexus.chash_alias a2 "
                + "WHERE a2.old_ref = f.chunk_id AND a2.new_chash = g.new_chash "
                + "  AND f.chunk_id = g.keep_id");
            ctx.execute(
                "DELETE FROM nexus.frecency f "
                + "USING " + frecencyAgg + ", nexus.chash_alias a2 "
                + "WHERE a2.old_ref = f.chunk_id AND a2.new_chash = g.new_chash "
                + "  AND f.chunk_id <> g.keep_id");
            counts.put("frecency_repointed", ctx.execute(
                "UPDATE nexus.frecency f SET chunk_id = encode(a.new_chash, 'hex') "
                + "FROM nexus.chash_alias a WHERE f.chunk_id = a.old_ref"));
            counts.put("relevance_log_repointed", ctx.execute(
                "UPDATE nexus.relevance_log r SET chunk_id = encode(a.new_chash, 'hex') "
                + "FROM nexus.chash_alias a WHERE r.chunk_id = a.old_ref"));

            // (6) verification scans, same transaction: residual mismatched
            // content rows and dangling pointers — MUST all be zero.
            int residual = 0;
            for (String t : CHUNK_TABLES) {
                residual += ctx.fetchOne(
                    ChashSqlIdioms.residualMismatchCount(t)).get(0, Integer.class);
            }
            counts.put("residual_mismatched", residual);
            Integer danglingManifest = ChashSqlIdioms.danglingManifestCountDsl(ctx);
            counts.put("dangling_manifest", danglingManifest);
            // nexus-t76bp REWORK, critic Option 2 (T2 nexus/critique-t76bp-
            // rekey-gate-2026-08-08 [21807]): both counts were COMPUTED here
            // but never ENFORCED — the class's own javadoc said "MUST all be
            // zero" while the code merely returned them in the envelope for
            // "the client rung" to check post-hoc. StagingPromoteOps.
            // finalizeTenant, the sibling chash mover, already throws loud
            // on the identical counts (see
            // preExistingDanglingManifestRow_abortsFinalizeLoud). Mirrored
            // here as the DETECTION half of the combined fix.
            //
            // ACTUAL COVERAGE (corrected per critic round 2, T2 same doc,
            // "residual-naming adjudication" — the prior wording here
            // overclaimed "catches ANY race in a window the gate does not
            // cover", which is false): this check runs INSIDE rekey's own
            // transaction, so it can only ever see state COMMITTED before
            // its own statement's snapshot. It catches pre-existing
            // corruption (a dangling/mismatched row that already existed
            // before this rekey began) and any race that committed before
            // this point in this same transaction's view. A sweep that
            // blocks on rekey's OWN row locks necessarily commits AFTER
            // rekey does — permanently invisible to this check, not caught
            // by it.
            //
            // What actually protects step (2b)'s named residual — the
            // orphan-synthesize sub-branch — is a DIFFERENT mechanism, not
            // this scan: the surrogate chash exists only on the physical
            // row rekey itself just UPDATEd, and that row stays
            // exclusively locked from the UPDATE through this
            // transaction's commit. A concurrent sweep's DELETE targeting
            // the same row blocks on that row lock; once rekey commits,
            // the sweep's statement re-evaluates its own quals against the
            // new row version (EvalPlanQual) and finds it no longer
            // matches (chash/metadata changed under it), so the DELETE
            // affects zero rows instead of removing a now-referenced
            // surrogate. This check remains a genuine backstop for every
            // OTHER gap, present or future ("no silent fallbacks for
            // data-correctness problems — FAIL LOUD") — it is just not the
            // mechanism protecting the synthesize path specifically.
            if (residual != 0) {
                throw new IllegalStateException(
                    "rekey left " + residual + " digest-mismatched content row(s) — aborting");
            }
            if (danglingManifest != null && danglingManifest != 0) {
                throw new IllegalStateException(
                    "rekey found " + danglingManifest + " dangling manifest row(s) "
                    + "(manifest chash with no content row in any dim) — aborting");
            }
            // Census (nexus-jxizy.10.5): REPORTED here, fatal only on the
            // staging-finalize path — the shipped in-store rekey keeps its
            // contract while the envelope gains the every-column visibility
            // (critic-C3: the old verify saw 2 of ~6 surfaces).
            Map<String, Integer> census = ChashCensus.scan(ctx);
            counts.put("census_residue_columns", census.size());
            if (!census.isEmpty()) {
                counts.put("census_residue", census.toString());
                log.warn("event=rekey_census_residue tenant-scope residue={}", census);
            }
            return counts;
        });
        log.info("event=rekey_complete tenant={} counts={}", tenant, out);
        return out;
    }

    /** UNION ALL of mismatched content rows across dims with recovered old_ref. */
    private static String unionAllContentRows() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < CHUNK_TABLES.size(); i++) {
            if (i > 0) sb.append(" UNION ALL ");
            String t = CHUNK_TABLES.get(i);
            sb.append("SELECT ")
              .append(String.format(OLD_REF, "chash")).append(" AS old_ref, ")
              .append("chash AS old_bytes, ")
              .append(DIGEST).append(" AS new_chash, ")
              .append("'").append(t).append("' AS source ")
              .append("FROM ").append(t)
              .append(" WHERE chunk_text <> '' AND chash IS DISTINCT FROM ").append(DIGEST);
        }
        return sb.toString();
    }
}
