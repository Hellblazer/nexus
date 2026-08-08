/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
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
 * RDR-180 LAND-THEN-TRANSFORM promote (nexus-jxizy.10.3): the in-DB,
 * transactional re-id from the width-free {@code staging} schema into the
 * strict {@code nexus} schema.
 *
 * <p>Design of record: T2 {@code nexus_rdr/180-land-transform-design} +
 * {@code -reconciliation}. Sibling of {@link RekeyOps} composing the SAME
 * {@link ChashSqlIdioms} fragments in the INSERT-into-possibly-populated-
 * target shape (different collision surface — reconciliation R5).
 *
 * <p>TWO operations, both under {@link TenantScope#withTenant} (nexus_svc,
 * RLS-scoped by construction):
 *
 * <h3>{@link #promoteCollection} — one txn per (tenant, collection)</h3>
 * <ol>
 *   <li>Dim precheck: staged dims must be one of the three tables AND agree
 *       with the collection name's implied dim (reconciliation H1 — the
 *       caller passes the name-implied dim from the same dispatch serving
 *       uses; belt + braces against mislabeled sources).</li>
 *   <li>C1 GUARD (tenant-wide, against COMMITTED state): any staged ref
 *       whose already-committed {@code chash_alias.new_chash} differs from
 *       this batch's computed digest fails LOUD with both digests — never
 *       a silent {@code DO NOTHING} keep. Same-ref-same-digest is the
 *       idempotent-resume case and passes.</li>
 *   <li>Alias build for genuinely-legacy refs ({@code old_bytes} via the
 *       shared {@code nexus.chash_old_bytes} function).</li>
 *   <li>Content INSERT per dim: {@code DISTINCT ON (digest)} with the M1
 *       deterministic tiebreak (already-canonical ref first, else min
 *       legacy_ref), {@code ON CONFLICT DO NOTHING} against live rows.
 *       Vectors are copied verbatim (the landing client only stages a
 *       vector when reuse is legal — staged NULL embeddings must be
 *       embed-filled in staging BEFORE promote; this op REFUSES content
 *       rows with NULL embeddings, counting them loud).</li>
 *   <li>(RETIRED, RDR-187/nexus-piwya.7) the chash_index promote leg —
 *       the chunks promote above IS the chash registration. The
 *       staging.chash_index landing twin itself dropped at nexus-piwya.11
 *       (rdr187-002); old clients' landing attempts answer 400.</li>
 * </ol>
 *
 * <h3>{@link #finalizeTenant} — IDEMPOTENT, RE-RUNNABLE (reconciliation C2)</h3>
 * Runs after EVERY promote wave; a late-landed collection is handled by
 * promote + finalize again. One tenant txn:
 * <ol>
 *   <li>Manifest promote (doc-scoped, so it lives here where the alias is
 *       complete — deviation from the design memo's per-collection manifest
 *       placement, recorded in the bead close): staged manifest rows insert
 *       through the alias join (or a direct 64-hex decode), canonical at
 *       INSERT.</li>
 *   <li>Pointer stores: topic_assignments (alias-repointed where
 *       chash-shaped, verbatim where a memory title — the mixed identity
 *       space stays TEXT debt), frecency (GREATEST-merge, staging-sourced
 *       twin of the RekeyOps aggregate), relevance_log (anti-join dedupe —
 *       BIGSERIAL target has no natural key), document_aspects +
 *       aspect_extraction_queue (anti-join on (collection, source_path)).</li>
 *   <li>Item8 disposition for staged empty-text rows with TENANT-WIDE
 *       visibility (reconciliation C4): reference-only rows (alias
 *       resolves) count as resolved; orphans get the per-run policy
 *       (drop = never promoted, counted; synthesize = deterministic
 *       surrogate + {@code chash_origin='synthetic'}).</li>
 *   <li>In-txn verify: residual digest-mismatch and dangling-manifest
 *       counts MUST be zero (abort otherwise); the census extension rides
 *       nexus-jxizy.10.5.</li>
 * </ol>
 */
// SANCTIONED RAW (nexus-jxizy.10.3, RawSqlGateTest allowlist): one-shot
// migration statements composed from the ChashSqlIdioms fragments —
// sha256() digests, DISTINCT ON keeper selection, alias joins and
// GREATEST-merge aggregates have no jOOQ DSL form; never serving-path.
public final class StagingPromoteOps {

    private static final Logger log = LoggerFactory.getLogger(StagingPromoteOps.class);

    /** Digest over the STAGED text column (alias {@code s}). */
    private static final String S_DIGEST = "sha256(convert_to(s.chunk_text, 'UTF8'))";

    private final TenantScope tenantScope;

    public StagingPromoteOps(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /** Thrown on the C1 guard: a staged ref contradicts a committed alias. */
    public static final class PromoteConflictException extends RuntimeException {
        public PromoteConflictException(String message) {
            super(message);
        }
    }

    /** Thrown when staged rows cannot promote (dim mismatch, NULL vectors). */
    public static final class PromotePreconditionException extends RuntimeException {
        public PromotePreconditionException(String message) {
            super(message);
        }
    }

    /**
     * Promote one landed collection. {@code impliedDim} is the collection
     * NAME's dim per the same dispatch serving uses (reconciliation H1).
     * Returns the auditable counts envelope.
     */
    public Map<String, Object> promoteCollection(String tenant, String collection, int impliedDim) {
        if (impliedDim != 384 && impliedDim != 768 && impliedDim != 1024) {
            throw new PromotePreconditionException(
                "impliedDim must be one of 384/768/1024, got " + impliedDim);
        }
        Map<String, Object> out = tenantScope.withTenant(tenant, ctx -> {
            Map<String, Object> counts = new LinkedHashMap<>();

            // Per-tenant serialization (review P1 High: TOCTOU between the
            // C1 guard SELECT and the alias INSERT under READ COMMITTED —
            // a concurrent promote could commit a conflicting alias in the
            // gap and the ON CONFLICT DO NOTHING would keep it SILENTLY).
            // The xact-scoped advisory lock serializes every promote AND
            // finalize for one tenant; released automatically at txn end.
            ctx.execute("SELECT pg_advisory_xact_lock(hashtext('staging:' || "
                + "current_setting('nexus.tenant', true)))");

            // (1) dim precheck — every staged content row must carry the
            // name-implied dim (land-time classification already renamed
            // mislabeled collections to their honest target).
            Integer badDim = ctx.fetchOne(
                "SELECT count(*) FROM staging.chunks s "
                + "WHERE s.collection = ? AND s.dim <> ?",
                collection, impliedDim).get(0, Integer.class);
            if (badDim != null && badDim > 0) {
                throw new PromotePreconditionException(
                    badDim + " staged row(s) in '" + collection + "' carry a dim "
                    + "differing from the collection name's implied " + impliedDim
                    + " — the land-time classification must rename mislabeled "
                    + "sources to their honest target (nexus-nb7hr), never "
                    + "promote a name/dim disagreement");
            }
            Integer nullVec = ctx.fetchOne(
                "SELECT count(*) FROM staging.chunks s "
                + "WHERE s.collection = ? AND s.chunk_text <> '' AND s.embedding IS NULL",
                collection).get(0, Integer.class);
            if (nullVec != null && nullVec > 0) {
                throw new PromotePreconditionException(
                    nullVec + " staged content row(s) in '" + collection + "' have "
                    + "no embedding — embed-fill staging before promote (reuse "
                    + "was not legal for these rows)");
            }

            // (2) C1 GUARD: staged refs vs COMMITTED alias state, tenant-wide.
            var conflict = ctx.resultQuery(
                "SELECT s.legacy_ref, encode(a.new_chash, 'hex') AS committed, "
                + "       encode(" + S_DIGEST + ", 'hex') AS computed, a.source "
                + "FROM staging.chunks s JOIN nexus.chash_alias a ON a.old_ref = s.legacy_ref "
                + "WHERE s.collection = ? AND s.chunk_text <> '' "
                + "  AND a.new_chash IS DISTINCT FROM " + S_DIGEST,
                collection).fetchAny();
            if (conflict != null) {
                throw new PromoteConflictException(
                    "staged ref '" + conflict.get(0, String.class) + "' in '" + collection
                    + "' computes digest " + conflict.get(2, String.class)
                    + " but chash_alias already maps it to " + conflict.get(1, String.class)
                    + " (source: " + conflict.get(3, String.class) + ") — the same legacy id "
                    + "denotes different content across collections; refusing to pick "
                    + "silently (GH #1390: correct addresses only)");
            }

            // (3) alias facts for genuinely-legacy staged refs.
            counts.put("alias_rows", ctx.execute(
                "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                + "SELECT current_setting('nexus.tenant', true), s.legacy_ref, "
                + "       " + ChashSqlIdioms.chashOldBytes("s.legacy_ref") + ", "
                + "       " + S_DIGEST + ", 'staging:' || s.collection "
                + "FROM staging.chunks s "
                + "WHERE s.collection = ? AND s.chunk_text <> '' "
                + "  AND s.legacy_ref <> encode(" + S_DIGEST + ", 'hex') "
                + "ON CONFLICT (tenant_id, old_ref) DO NOTHING",
                collection));

            // (3a) Un-blind the planner before the promote/collapse joins read
            // the rows just written — the same F2 exposure RekeyOps carries
            // (production 2026-07-20): alias rows inserted in THIS transaction
            // are invisible to a planner working from statistics autoanalyze
            // froze at the previous tenant's distribution, which turns the
            // alias joins below into nested loops over the full pointer tables.
            // Silent no-op without MAINTAIN (grants-nexus-svc, PG17+), so the
            // outcome rides the envelope rather than being assumed.
            counts.put("alias_stats_refreshed", ChashSqlIdioms.refreshAliasStats(ctx));

            // (4) collection registration stub — the chunks tables carry a
            // (tenant, collection) FK to catalog_collections (RDR-156
            // schema-enforced integrity: the FK that mechanically catches a
            // missed landing leg). Same ON-CONFLICT-DO-NOTHING shape as the
            // serving path's auto-stub; the catalog ETL's fuller row wins
            // when it already exists.
            String contentType = collection.contains("__")
                ? collection.substring(0, collection.indexOf("__")) : "knowledge";
            ctx.execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type) "
                + "VALUES (current_setting('nexus.tenant', true), ?, ?) "
                + "ON CONFLICT (tenant_id, name) DO NOTHING",
                collection, contentType);

            // nexus-11gh6 round 3 CRITICAL (T2 nexus/critique-11gh6-gate-
            // impl-2026-08-08 [21798] REWORK DELTA): this content INSERT
            // lands rows into chunks_<dim> for `collection` -- a SHARED
            // chash this INSERT lands fresh can ALREADY be referenced by a
            // live, unrelated document D in the SAME collection (RDR-180
            // migrates content that "very often already exists live
            // elsewhere" -- shared boilerplate, license headers). If D's
            // own ordinary write concurrently drops that reference and
            // triggers its sweep before this transaction commits, the
            // sweep's guard (evaluated tenant-wide, no awareness of this
            // migration) can see NOTHING referencing the chash yet -- D
            // just dropped it, finalizeTenant hasn't manifested it -- and
            // delete the very row this INSERT is landing, silently, before
            // finalizeTenant's canonExists re-check ever runs (it would
            // then correctly, but uselessly, report the pointer as
            // manifest_unresolved: DATA LOSS for the migration, not merely
            // a dangling reference). Gate SHARED for `collection` BEFORE
            // this INSERT so a concurrent sweep's EXCLUSIVE acquire cannot
            // be granted until this transaction commits -- unlike
            // finalizeTenant this is a single, direct-parameter collection,
            // no multi-collection resolution needed.
            //
            // RESIDUAL, honestly named (NOT closed by this gate, matching
            // the discipline kl2z6/document_restore already established):
            // this only narrows the window to "concurrent with THIS
            // transaction". The content this INSERT lands has NO manifest
            // reference of its own until finalizeTenant runs, which can be
            // arbitrarily later ("runs after EVERY promote wave" -- no
            // bound on the gap). Once this transaction COMMITS and releases
            // the gate, the freshly-landed-but-still-unmanifested chash is
            // AGAIN a legitimate sweep target for any unrelated doc that
            // happens to share it, exactly like the client-side upload-
            // window axis (nexus-kl2z6) one level up: the engine cannot
            // gate an intent (finalizeTenant's eventual manifest write)
            // that has not arrived yet. Closing THAT gap would mean
            // merging promote and finalize into one transaction or holding
            // the gate across both, which is a structural change to
            // RDR-180's "one txn per collection, tenant-wide finalize
            // later" design -- out of scope for a targeted gate fix.
            CatalogRepository.acquireSweepGateShared(ctx, tenant, collection);

            // (5) content INSERT for the collection's dim table. DISTINCT ON
            // digest with the M1 deterministic tiebreak; DO NOTHING against
            // live rows (idempotent resume; populated-target legal).
            // Metadata gains the RDR-086 chunk_text_hash stamp (--guided
            // gate run 3 catch, nexus-jxizy.10.10): serving-path writes
            // stamp it client-side and the citation resolver's final hop
            // (/v1/vectors/get where-filter) reads it — a verbatim
            // chunk_meta copy left every migrated chunk invisible to
            // citations. Promoted rows must be indistinguishable from
            // serving-path writes; the digest is computed here anyway.
            String chunkTable = "nexus.chunks_" + impliedDim;
            int promoted = ctx.execute(
                "INSERT INTO " + chunkTable + " (tenant_id, collection, chash, chunk_text, embedding, metadata) "
                + "SELECT DISTINCT ON (" + S_DIGEST + ") "
                + "       current_setting('nexus.tenant', true), s.collection, "
                + "       " + S_DIGEST + ", s.chunk_text, "
                + "       s.embedding::vector(" + impliedDim + "), "
                + "       coalesce(s.chunk_meta, '{}'::jsonb) "
                + "         || jsonb_build_object('chunk_text_hash', encode(" + S_DIGEST + ", 'hex')) "
                + "FROM staging.chunks s "
                + "WHERE s.collection = ? AND s.chunk_text <> '' "
                + "ORDER BY " + S_DIGEST + ", "
                + "         (s.legacy_ref = encode(" + S_DIGEST + ", 'hex')) DESC, "
                + "         s.legacy_ref "
                + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING",
                collection);
            counts.put("promoted", promoted);
            Integer stagedContent = ctx.fetchOne(
                "SELECT count(*) FROM staging.chunks s WHERE s.collection = ? AND s.chunk_text <> ''",
                collection).get(0, Integer.class);
            counts.put("staged_content", stagedContent);

            // (5) RETIRED — the chash_index promote leg (RDR-187, nexus-piwya.7;
            // gate Finding 1: this removal had to precede the .9 DROP, or the
            // INSERT above would have hard-crashed every guided-upgrade promote
            // on a missing relation). The one-release LANDED-DEAD-SINK window
            // closed at nexus-piwya.11: staging.chash_index is dropped
            // (rdr187-002) and StagingHandler.STORES lost the entry, so an old
            // client's landing attempt answers 400 unknown-store. The chunks
            // promote (4) IS the chash registration.

            return counts;
        });
        log.info("event=staging_promote_collection tenant={} collection={} counts={}",
            tenant, collection, out);
        return out;
    }

    /**
     * The idempotent tenant finalize (reconciliation C2/C4): manifest +
     * pointer-store promotion through the (cumulative) alias, Item8
     * disposition with tenant-wide visibility, in-txn verify. Re-runnable
     * after every promote wave.
     */
    public Map<String, Object> finalizeTenant(String tenant, boolean synthesizeOrphans) {
        Map<String, Object> out = tenantScope.withTenant(tenant, ctx -> {
            Map<String, Object> counts = new LinkedHashMap<>();

            // Same per-tenant serialization as promoteCollection: finalize
            // must never run concurrently with an in-flight promote (review
            // P1 Medium: Item8's tenant-wide visibility would miss an
            // uncommitted promote's aliases and could drop a resolvable
            // reference).
            ctx.execute("SELECT pg_advisory_xact_lock(hashtext('staging:' || "
                + "current_setting('nexus.tenant', true)))");

            // (1) Item8, tenant-wide (C4): staged empty-text rows.
            //     reference-only = the ref resolves through the alias (content
            //     landed in ANY collection) or is already-canonical for a live
            //     chunk. Orphans get the policy.
            String orphanCond =
                "s.chunk_text = '' "
                + "AND NOT EXISTS (SELECT 1 FROM nexus.chash_alias a WHERE a.old_ref = s.legacy_ref) "
                + "AND NOT EXISTS (SELECT 1 FROM staging.chunks c2 "
                + "      WHERE c2.legacy_ref = s.legacy_ref AND c2.chunk_text <> '')";
            counts.put("reference_only_resolved", ctx.fetchOne(
                "SELECT count(*) FROM staging.chunks s WHERE s.chunk_text = '' "
                + "AND EXISTS (SELECT 1 FROM nexus.chash_alias a WHERE a.old_ref = s.legacy_ref)")
                .get(0, Integer.class));
            if (synthesizeOrphans) {
                // Alias the surrogates FIRST so pointer promotion below
                // repoints them (the RekeyOps ordering, verbatim rationale).
                ctx.execute(
                    "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                    + "SELECT current_setting('nexus.tenant', true), s.legacy_ref, "
                    + "       " + ChashSqlIdioms.chashOldBytes("s.legacy_ref") + ", "
                    + "       sha256(convert_to("
                    + "         'nexus:synthetic-chash:v1|' || current_setting('nexus.tenant', true) "
                    + "         || '|' || s.collection || '|' || s.legacy_ref, 'UTF8')), "
                    + "       'staging:synthetic' "
                    + "FROM staging.chunks s WHERE " + orphanCond + " "
                    + "ON CONFLICT (tenant_id, old_ref) DO NOTHING");
                counts.put("orphans_synthesized", ctx.execute(
                    "INSERT INTO nexus.chunks_768 (tenant_id, collection, chash, chunk_text, embedding, metadata) "
                    + "SELECT current_setting('nexus.tenant', true), s.collection, a.new_chash, '', "
                    + "       s.embedding::vector(768), "
                    // chunk_text_hash mirrors the SURROGATE chash (RDR-086
                    // metadata parity, same rationale as the content INSERT).
                    + "       coalesce(s.chunk_meta, '{}'::jsonb) || jsonb_build_object("
                    + "         'chash_origin', 'synthetic', "
                    + "         'chunk_text_hash', encode(a.new_chash, 'hex')) "
                    + "FROM staging.chunks s JOIN nexus.chash_alias a "
                    + "  ON a.old_ref = s.legacy_ref AND a.source = 'staging:synthetic' "
                    + "WHERE s.chunk_text = '' AND s.dim = 768 AND s.embedding IS NOT NULL "
                    + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING"));
                counts.put("orphans_dropped", 0);
            } else {
                counts.put("orphans_synthesized", 0);
                counts.put("orphans_dropped", ctx.fetchOne(
                    "SELECT count(*) FROM staging.chunks s WHERE " + orphanCond)
                    .get(0, Integer.class));
            }

            // (2) manifest promote through the alias (doc-scoped => finalize;
            //     canonical at INSERT so the octet CHECK holds by construction).
            // RESOLVABLE-ONLY, review P1 Critical: the alias arm implies
            // content exists (alias + content insert share one promote txn);
            // the direct 64-hex arm must PROVE existence — a canonical-shaped
            // staged pointer whose content was orphan-dropped or has not
            // promoted yet stays STAGED (a later finalize converges it) so a
            // dangling manifest row can never be CREATED here, which is what
            // makes the fatal dangling gate below coherent mid-migration.
            String canonExists =
                "EXISTS (SELECT 1 FROM nexus.chunks_384 c WHERE c.chash = decode(s.chash, 'hex')) "
                + "OR EXISTS (SELECT 1 FROM nexus.chunks_768 c WHERE c.chash = decode(s.chash, 'hex')) "
                + "OR EXISTS (SELECT 1 FROM nexus.chunks_1024 c WHERE c.chash = decode(s.chash, 'hex'))";

            // nexus-11gh6 (post-review Critical, T2 nexus/critique-11gh6-
            // gate-impl-2026-08-08 [21798]): the manifest INSERT below
            // creates LIVE catalog_document_chunks references -- the exact
            // hazard class CatalogRepository.acquireSweepGateShared exists
            // to serialize against a concurrent sweep=true writer's guard.
            // Unlike every other gated site this one INSERT statement spans
            // EVERY collection with staged docs in this tenant at once (and
            // never stamps a per-row `collection` column at all -- see the
            // INSERT's column list below), so there is no single `coll` to
            // gate.
            //
            // nexus-11gh6 round 3 SIGNIFICANT fix (T2 nexus/critique-11gh6-
            // gate-impl-2026-08-08 [21798] REWORK DELTA): round 2 resolved
            // target collections from the referencing DOCUMENT's own
            // `physical_collection`. That can DIVERGE from where the
            // candidate chash's content actually lives: chunks_<dim>'s PK
            // is (tenant_id, collection, chash) -- content is duplicated
            // PER COLLECTION, not globally deduped -- and `canonExists`
            // below is deliberately collection-agnostic (checks all three
            // dim tables with NO collection filter, mirroring the sweep's
            // own shared-chash-union guard philosophy). Gating
            // `physical_collection` alone could therefore gate the WRONG
            // collection relative to what `canonExists` actually leans on,
            // leaving the real content's collection unprotected. Fixed by
            // resolving target collections from the SAME source
            // `canonExists` reads from -- for every chash this INSERT's own
            // WHERE clause could select (alias-resolved OR direct 64-hex),
            // find every `(chunks_384|768|1024).collection` row that
            // actually carries it. By construction this cannot diverge from
            // `canonExists`: whatever collection satisfies `canonExists` for
            // a candidate row is a `(collection, chash)` pair in one of the
            // three tables, hence a row this query also finds and gates.
            // Sorted for the same correctness-neutral future-proofing every
            // other multi-gate site uses (§5.1 deadlock analysis: sweepers
            // are pure sinks, writers never escalate SHARED to EXCLUSIVE, so
            // no acquisition order can create a cycle regardless of N).
            // jOOQ DSL rendering (nexus-t76bp representation-only pass, Hal
            // directive 2026-08-08): identical semantics to the prior raw
            // SQL. `staging.document_chunks` has no generated jOOQ class
            // (the codegen config does not cover the `staging` schema —
            // it is a landing area, never a serving-path table), so its
            // table/column are referenced via DSL.table/DSL.name (the
            // same house pattern CatalogRepository already uses for
            // caller-supplied relation strings) while every `nexus.*`
            // table below uses the generated Tables constants. The
            // `decode(s.chash, 'hex')` format literal binds via DSL.val
            // rather than string interpolation.
            var sdc = DSL.table(DSL.name("staging", "document_chunks")).as("s");
            // sdc carries NO generated column metadata (DSL.table(Name) is an
            // opaque table reference), so sdc.field("chash", ...) resolves to
            // null -- the field must be built directly against the "s" alias,
            // the same house pattern CatalogRepository uses for other plain-
            // table columns (e.g. F_DOC_META = DSL.field(DSL.name(
            // "catalog_documents", "metadata"), String.class)).
            Field<String> sChash = DSL.field(DSL.name("s", "chash"), String.class);
            Field<byte[]> sChashDecoded = DSL.function("decode", byte[].class, sChash, DSL.val("hex"));
            var cand = ctx.selectDistinct(DSL.coalesce(CHASH_ALIAS.NEW_CHASH, sChashDecoded).as("chash"))
                .from(sdc)
                .leftJoin(CHASH_ALIAS).on(CHASH_ALIAS.OLD_REF.eq(sChash))
                .where(CHASH_ALIAS.NEW_CHASH.isNotNull().or(sChash.likeRegex("^[0-9a-f]{64}$")))
                .asTable("cand");
            var phys = ctx.select(CHUNKS_384.COLLECTION, CHUNKS_384.CHASH).from(CHUNKS_384)
                .unionAll(ctx.select(CHUNKS_768.COLLECTION, CHUNKS_768.CHASH).from(CHUNKS_768))
                .unionAll(ctx.select(CHUNKS_1024.COLLECTION, CHUNKS_1024.CHASH).from(CHUNKS_1024))
                .asTable("phys");
            var targetCollections = ctx.selectDistinct(phys.field("collection", String.class))
                .from(cand)
                .join(phys).on(phys.field("chash", byte[].class).eq(cand.field("chash", byte[].class)))
                .fetch();
            for (String c : targetCollections.stream()
                    .map(r -> r.get(0, String.class))
                    .filter(name -> name != null && !name.isBlank())
                    .sorted()
                    .distinct()
                    .toList()) {
                CatalogRepository.acquireSweepGateShared(ctx, tenant, c);
            }

            counts.put("manifest_promoted", ctx.execute(
                "INSERT INTO nexus.catalog_document_chunks "
                + "  (tenant_id, doc_id, position, chash, chunk_index, line_start, line_end, char_start, char_end) "
                + "SELECT current_setting('nexus.tenant', true), s.doc_id, s.position, "
                + "       COALESCE(a.new_chash, decode(s.chash, 'hex')), "
                + "       s.chunk_index, s.line_start, s.line_end, s.char_start, s.char_end "
                + "FROM staging.document_chunks s "
                + "LEFT JOIN nexus.chash_alias a ON a.old_ref = s.chash "
                + "WHERE a.new_chash IS NOT NULL "
                + "   OR (s.chash ~ '^[0-9a-f]{64}$' AND (" + canonExists + ")) "
                + "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING"));
            counts.put("manifest_unresolved", ctx.fetchOne(
                "SELECT count(*) FROM staging.document_chunks s "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.chash_alias a WHERE a.old_ref = s.chash) "
                + "  AND NOT (s.chash ~ '^[0-9a-f]{64}$' AND (" + canonExists + "))")
                .get(0, Integer.class));

            // (2b) nexus-b6enc F3: the promote above is RESOLVABLE-ONLY, so
            // the verbatim-imported documents.chunk_count can claim more
            // chunks than actually landed. Mass-resync the count from the
            // actually-promoted manifest rows for every doc this migration
            // staged — never trust the imported count. Scoped to staged docs
            // so live serving writes outside the migration are untouched.
            // TOMBSTONE-EXEMPT (nexus-mqd6t): no deleted_at filter -- RDR-180
            // land-then-transform migration leg (nexus-jxizy.10.3/10.4), one-shot,
            // never serving-path, same sanction class as RawSqlGateTest's raw-SQL
            // allowance for this file. See TombstoneFilterGateTest.TOMBSTONE_EXEMPT.
            counts.put("chunk_count_resynced", ctx.execute(
                "UPDATE nexus.catalog_documents d "
                + "SET chunk_count = COALESCE(m.cnt, 0) "
                + "FROM (SELECT DISTINCT doc_id FROM staging.document_chunks) sd "
                + "LEFT JOIN (SELECT doc_id, count(*) AS cnt "
                + "             FROM nexus.catalog_document_chunks GROUP BY doc_id) m "
                + "  ON m.doc_id = sd.doc_id "
                + "WHERE d.tumbler = sd.doc_id "
                + "  AND d.chunk_count IS DISTINCT FROM COALESCE(m.cnt, 0)"));

            // (2c) nexus-b6enc F3: store_put-origin docs (content_type =
            // 'knowledge', empty file_path) have NO source file — an
            // unresolved pointer for one can never converge via re-index, so
            // it must be surfaced BY TITLE for the user to re-store.
            // TOMBSTONE-EXEMPT (nexus-mqd6t): same migration-leg sanction as the
            // chunk_count_resync UPDATE above. See TombstoneFilterGateTest.TOMBSTONE_EXEMPT.
            List<String> unresolvedKnowledgeTitles = ctx.fetch(
                "SELECT DISTINCT d.title FROM nexus.catalog_documents d "
                + "JOIN staging.document_chunks s ON s.doc_id = d.tumbler "
                + "WHERE d.content_type = 'knowledge' "
                + "  AND COALESCE(d.file_path, '') = '' "
                + "  AND NOT EXISTS (SELECT 1 FROM nexus.chash_alias a WHERE a.old_ref = s.chash) "
                + "  AND NOT (s.chash ~ '^[0-9a-f]{64}$' AND (" + canonExists + ")) "
                + "ORDER BY 1 LIMIT 100")
                .map(r -> r.get(0, String.class));
            counts.put("unresolved_knowledge_titles", unresolvedKnowledgeTitles);

            // (3) topic_assignments: alias-repoint chash-shaped doc_ids,
            //     verbatim pass-through for memory titles (mixed identity).
            //     RESOLVABLE-ONLY (census discipline, nexus-jxizy.10.5): a
            //     legacy-shaped doc_id with NO alias yet stays STAGED — a
            //     later finalize converges it once its content collection
            //     promotes; verbatim legacy ids never enter nexus.
            // TOPIC IDENTITY (critic-p1 Critical): the staged legacy integer
            // id is BIGSERIAL-local and can NEVER reference nexus.topics(id).
            // Resolution rides (label, collection) — the SAME key the topic
            // ETL upserts by — so the promoted row carries the TARGET store's
            // own id. Resolvable-only on BOTH axes: an assignment whose topic
            // has not landed (topics ETL pending) or whose chash-shaped
            // doc_id has no alias yet stays STAGED for a later finalize.
            counts.put("topic_assignments_promoted", ctx.execute(
                "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id) "
                + "SELECT DISTINCT current_setting('nexus.tenant', true), "
                + "       COALESCE(encode(a.new_chash, 'hex'), s.doc_id), t.id "
                + "FROM staging.topic_assignments s "
                + "JOIN nexus.topics t "
                + "  ON t.label = s.topic_label AND t.collection = s.topic_collection "
                + "LEFT JOIN nexus.chash_alias a ON a.old_ref = s.doc_id "
                + "WHERE a.new_chash IS NOT NULL "
                + "   OR s.doc_id !~ '^([0-9a-f]{16}|[0-9a-f]{32})$' "
                + "ON CONFLICT (tenant_id, doc_id, topic_id) DO NOTHING"));
            counts.put("topic_assignments_unresolved", ctx.fetchOne(
                "SELECT count(*) FROM staging.topic_assignments s "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.topics t "
                + "  WHERE t.label = s.topic_label AND t.collection = s.topic_collection)")
                .get(0, Integer.class));

            // (4) frecency: GREATEST-merge from the staged rows through the
            //     alias — staging-sourced twin of ChashSqlIdioms'
            //     frecencyAliasAggregate (same semantics, staged source).
            String stagedFrecencyAgg =
                "(SELECT COALESCE(encode(a.new_chash, 'hex'), s.chunk_id) AS target_id, "
                + "        max(s.frecency_score) AS fs, max(s.miss_count) AS mc, "
                + "        max(COALESCE(NULLIF(s.last_hit_at, '')::timestamptz, now())) AS lh, "
                + "        max(COALESCE(NULLIF(s.embedded_at, '')::timestamptz, now())) AS ea, "
                + "        max(s.ttl_days) AS td "
                + "   FROM staging.frecency s LEFT JOIN nexus.chash_alias a "
                + "     ON s.chunk_id = a.old_ref "
                + "   WHERE a.new_chash IS NOT NULL OR s.chunk_id ~ '^[0-9a-f]{64}$' "
                + "   GROUP BY 1) g";
            ctx.execute(
                "UPDATE nexus.frecency t SET "
                + "  frecency_score = GREATEST(t.frecency_score, g.fs), "
                + "  miss_count     = GREATEST(t.miss_count,     g.mc), "
                + "  last_hit_at    = GREATEST(t.last_hit_at,    g.lh), "
                + "  embedded_at    = GREATEST(t.embedded_at,    g.ea), "
                + "  ttl_days       = GREATEST(t.ttl_days,       g.td) "
                + "FROM " + stagedFrecencyAgg + " WHERE t.chunk_id = g.target_id");
            counts.put("frecency_promoted", ctx.execute(
                "INSERT INTO nexus.frecency (tenant_id, chunk_id, embedded_at, ttl_days, frecency_score, miss_count, last_hit_at) "
                + "SELECT current_setting('nexus.tenant', true), g.target_id, g.ea, g.td, g.fs, g.mc, g.lh "
                + "FROM " + stagedFrecencyAgg + " "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.frecency t WHERE t.chunk_id = g.target_id)"));

            // (5) relevance_log: BIGSERIAL target, no natural key — anti-join
            //     on the full staged identity for idempotent re-finalize.
            counts.put("relevance_log_promoted", ctx.execute(
                "INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, collection, action, session_id, timestamp) "
                + "SELECT current_setting('nexus.tenant', true), s.query, "
                + "       COALESCE(encode(a.new_chash, 'hex'), s.chunk_id), s.collection, s.action, s.session_id, "
                + "       COALESCE(NULLIF(s.ts, '')::timestamptz, now()) "
                + "FROM staging.relevance_log s "
                + "LEFT JOIN nexus.chash_alias a ON a.old_ref = s.chunk_id "
                + "WHERE (a.new_chash IS NOT NULL OR s.chunk_id ~ '^[0-9a-f]{64}$') "
                + "AND NOT EXISTS (SELECT 1 FROM nexus.relevance_log t "
                + "  WHERE t.query = s.query "
                + "    AND t.chunk_id = COALESCE(encode(a.new_chash, 'hex'), s.chunk_id) "
                + "    AND t.action = s.action "
                + "    AND t.timestamp = COALESCE(NULLIF(s.ts, '')::timestamptz, now()))"));

            // (6) aspects (Class-D): anti-join on (collection, source_path).
            //     source_path/source_uri carry no in-flight rewrite here —
            //     chroma:// URI repoints ride the alias at READ time via the
            //     shared resolvers; staged values land verbatim.
            counts.put("document_aspects_promoted", ctx.execute(
                "INSERT INTO nexus.document_aspects "
                + "  (tenant_id, collection, source_path, problem_formulation, proposed_method, "
                + "   experimental_datasets, experimental_baselines, experimental_results, extras, "
                + "   confidence, extracted_at, model_version, extractor_name, source_uri, doc_id) "
                + "SELECT current_setting('nexus.tenant', true), s.collection, s.source_path, "
                + "       s.problem_formulation, s.proposed_method, s.experimental_datasets, "
                + "       s.experimental_baselines, s.experimental_results, s.extras, s.confidence, "
                + "       COALESCE(NULLIF(s.extracted_at, '')::timestamptz, now()), "
                + "       s.model_version, s.extractor_name, s.source_uri, s.doc_id "
                + "FROM staging.document_aspects s "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.document_aspects t "
                + "  WHERE t.collection = s.collection AND t.source_path = s.source_path)"));
            counts.put("aspect_queue_promoted", ctx.execute(
                "INSERT INTO nexus.aspect_extraction_queue "
                + "  (tenant_id, collection, source_path, doc_id, content_hash, content, status, "
                + "   retry_count, enqueued_at, last_attempt_at, last_error) "
                + "SELECT current_setting('nexus.tenant', true), s.collection, s.source_path, s.doc_id, "
                + "       s.content_hash, s.content, s.status, s.retry_count, "
                + "       COALESCE(NULLIF(s.enqueued_at, '')::timestamptz, now()), "
                + "       NULLIF(s.last_attempt_at, '')::timestamptz, s.last_error "
                + "FROM staging.aspect_extraction_queue s "
                + "WHERE NOT EXISTS (SELECT 1 FROM nexus.aspect_extraction_queue t "
                + "  WHERE t.collection = s.collection AND t.source_path = s.source_path)"));

            // (7) in-txn verify (the census extension rides nexus-jxizy.10.5).
            int residual = 0;
            for (String t : ChashSqlIdioms.CHUNK_TABLES) {
                residual += ctx.fetchOne(
                    ChashSqlIdioms.residualMismatchCount(t)).get(0, Integer.class);
            }
            counts.put("residual_mismatched", residual);
            Integer danglingManifest = ChashSqlIdioms.danglingManifestCountDsl(ctx);
            counts.put("dangling_manifest", danglingManifest);
            if (residual != 0) {
                throw new IllegalStateException(
                    "finalize left " + residual + " digest-mismatched content row(s) — aborting");
            }
            // Review P1 Critical: the class contract says BOTH counts MUST be
            // zero — the resolvable-only manifest promote above means this can
            // only fire on pre-existing corruption, which must abort loud.
            if (danglingManifest != null && danglingManifest != 0) {
                throw new IllegalStateException(
                    "finalize found " + danglingManifest + " dangling manifest row(s) "
                    + "(manifest chash with no content row in any dim) — aborting");
            }
            // (8) THE COLUMN CENSUS (nexus-jxizy.10.5, Hal directive): every
            // TEXT/BYTEA column in schema nexus, schema-derived, must scan
            // clean of legacy residue outside the justified allowlist — the
            // mechanical missed-leg killer. FATAL here: a finalize that
            // leaves residue in nexus has left the migration incomplete.
            ChashCensus.assertDiscoversKnownInventory(ctx);
            Map<String, Integer> census = ChashCensus.scan(ctx);
            counts.put("census_residue_columns", census.size());
            if (!census.isEmpty()) {
                throw new IllegalStateException(
                    "census found legacy residue in nexus columns after finalize: "
                    + census + " — a migration leg missed these; refusing to report clean");
            }
            return counts;
        });
        log.info("event=staging_finalize tenant={} counts={}", tenant, out);
        return out;
    }
}
