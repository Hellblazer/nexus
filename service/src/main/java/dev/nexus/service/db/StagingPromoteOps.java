/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import dev.nexus.service.jooq.binding.Vector;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import dev.nexus.service.vectors.DimTables;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.FRECENCY;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

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
// nexus-4okz4 increment 3: this class's raw-SQL surface — the land-then-
// transform promote/finalize composing the ChashSqlIdioms fragments in the
// INSERT-into-populated-target shape (DISTINCT ON keepers, alias joins,
// GREATEST-merge, anti-join dedupes) — is now typed jOOQ DSL end to end.
// Every ctx.execute("...")/fetch*("...") string form from the
// nexus-jxizy.10.3 origin converted this increment (no residue, unlike
// RekeyOps' narrower increment-2 residue of two genuinely-no-DSL-form
// idioms) — REMOVED from RawSqlGateTest's SANCTIONED_METHODS allowlist
// outright, a dead sanction entry would otherwise point at a class with
// nothing left to excuse. staging.* tables carry no generated jOOQ class
// (codegen covers only the nexus/t1 schemas — staging is a one-shot
// landing area, never serving-path), so every staged-table reference below
// uses the house pattern already established at this file's pre-increment-3
// gate-resolution query: DSL.table(DSL.name(schema, table)).as(alias) +
// explicit per-column DSL.field(DSL.name(alias, col), Type.class).
//
// ONE SQL-shape decision worth recording: the staged-to-typed `vector`
// embedding column carries NO explicit `::vector(<dim>)` cast in the
// generated SQL (the raw form had one). PostgreSQL applies the identical
// typmod-coercion function automatically on assignment to a `vector(<dim>)`
// target column in an INSERT...SELECT — the same rule that applies to
// varchar(n)/numeric(p,s) targets — so this is a representation change,
// not a behavior change; verified against
// StagingPromoteOpsIntegrationTest's promoted-row/dim assertions.
public final class StagingPromoteOps {

    private static final Logger log = LoggerFactory.getLogger(StagingPromoteOps.class);

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
     * Typed rendering of {@code current_setting('nexus.tenant', true)}
     * (nexus-4okz4 increment 3, mirrors {@code RekeyOps.currentTenantSetting}
     * exactly — kept as a private twin here rather than hoisting to
     * {@link ChashSqlIdioms}, so RekeyOps' already-reviewed increment-2 diff
     * stays untouched). The SQL-side tenant read every writer in this class
     * deliberately uses INSTEAD OF the Java {@code tenant} parameter, so the
     * value always matches whatever {@link TenantScope#withTenant} actually
     * stamped on the connection.
     */
    private static Field<String> currentTenantSetting() {
        return DSL.function("current_setting", String.class, DSL.val("nexus.tenant"), DSL.val(true));
    }

    /** {@code encode(bytes, 'hex')} — same rendering as {@code RekeyOps.hex}. */
    private static Field<String> hex(Field<byte[]> bytes) {
        return DSL.function("encode", String.class, bytes, DSL.val("hex"));
    }

    /**
     * {@code coalesce(nullif(raw, ''), now())::timestamptz} — the staged-TEXT-
     * timestamp parse idiom every staging pointer-store promote applies
     * (empty string means "not recorded", falls back to now()). Typed
     * rendering of the raw {@code COALESCE(NULLIF(s.col, '')::timestamptz,
     * now())} fragment repeated verbatim across frecency/relevance_log/
     * document_aspects/aspect_extraction_queue (nexus-4okz4 increment 3).
     * {@code DSL.currentOffsetDateTime()} renders {@code CURRENT_TIMESTAMP}
     * rather than {@code now()} — in PostgreSQL these are the SAME function
     * (documented alias, both transaction-stable), not a behavior change.
     */
    private static Field<OffsetDateTime> parseStagedTimestamp(Field<String> raw) {
        return DSL.coalesce(DSL.nullif(raw, "").cast(OffsetDateTime.class), DSL.currentOffsetDateTime());
    }

    /** {@code nullif(raw, '')::timestamptz}, no {@code now()} fallback — the
     *  nullable-timestamp half of {@link #parseStagedTimestamp} (used only by
     *  {@code aspect_extraction_queue.last_attempt_at}, which stays NULL when
     *  unset rather than defaulting). */
    private static Field<OffsetDateTime> parseStagedTimestampNullable(Field<String> raw) {
        return DSL.nullif(raw, "").cast(OffsetDateTime.class);
    }

    /**
     * {@code nullif(raw, '')::jsonb} — the staged-TEXT-to-jsonb parse idiom
     * for columns converted by the schema type-hygiene arc (epic
     * nexus-cefa1; {@code document_aspects.extras} is the first user,
     * nexus-cefa1.4 / P3). Sibling of {@link #parseStagedTimestamp} /
     * {@link #parseStagedTimestampNullable}: same {@code NULLIF(raw,'')}
     * shape, cast to {@code jsonb} instead of {@code timestamptz}, no
     * {@code now()}-style fallback (there is no meaningful default JSON
     * value to substitute).
     *
     * <p>Staging stays typeless by design (the staged column itself is
     * still TEXT — this cast only happens at promote time). A malformed
     * staged value therefore fails LOUD at promote time (PostgreSQL raises
     * "invalid input syntax for type json" for the whole transactional
     * {@code finalizeTenant} promote), rather than landing silently — the
     * same fail-loud posture this class already applies via {@link
     * PromoteConflictException} / {@link PromotePreconditionException}, not
     * a new behavior.
     */
    private static Field<JSONB> parseStagedJson(Field<String> raw) {
        return DSL.nullif(raw, "").cast(JSONB.class);
    }

    /**
     * Content-table accessor for {@link #promoteCollection}'s content
     * INSERT (nexus-4okz4 increment 3, RDR-191 repoint nexus-o8dil.17).
     *
     * <p>Unlike {@code RekeyOps.Dim} (which carries no embedding field —
     * RekeyOps never INSERTs content rows), this record genuinely needs a
     * per-dim {@code embedding} accessor: {@link #promoteCollection} writes
     * a NEW content row whose embedding lands in the dim-specific column.
     * Post-unification all three dims share the SAME {@code table}
     * ({@link dev.nexus.service.jooq.nexus.Tables#CHUNKS}) and the SAME
     * tenantId/collection/chash/chunkText/metadata fields — only {@code
     * embedding} differs, selected via {@link DimTables#embeddingColumn(int)}
     * (the single name authority the raw-SQL/typed-DSL split shares, per
     * DimTables' own class javadoc) rather than hand-rolling
     * {@code "embedding_" + dim} here. byte[]-typed {@code chash} kept
     * (built directly off the generated table, not {@code
     * DimTables.ChunkTable}'s hex-string accessor) for the same reason as
     * {@code RekeyOps.Dim}: {@link ChashSqlIdioms#digestField} and the
     * {@code chash_alias} joins below are byte[]-typed, and {@code
     * Tables.CHUNKS.CHASH} is already byte[]-typed natively.
     */
    private record ChunkDim(Table<?> table, Field<String> tenantId, Field<String> collection,
                             Field<byte[]> chash, Field<String> chunkText, Field<Vector> embedding,
                             Field<JSONB> metadata) {
    }

    @SuppressWarnings("unchecked")
    private static ChunkDim chunkDim(int impliedDim) {
        return switch (impliedDim) {
            case 384, 768, 1024 -> new ChunkDim(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION,
                CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                (Field<Vector>) CHUNKS.field(DimTables.embeddingColumn(impliedDim)),
                CHUNKS.METADATA);
            // unreachable in practice: promoteCollection's own
            // PromotePreconditionException guard validates impliedDim is one
            // of 384/768/1024 before this method is ever invoked.
            default -> throw new IllegalStateException(
                "impliedDim must be 384/768/1024, got " + impliedDim);
        };
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

            // staging.chunks accessors (house pattern — see class javadoc).
            var sc = DSL.table(DSL.name("staging", "chunks")).as("s");
            Field<String> scCollection = DSL.field(DSL.name("s", "collection"), String.class);
            Field<Integer> scDim = DSL.field(DSL.name("s", "dim"), Integer.class);
            Field<String> scChunkText = DSL.field(DSL.name("s", "chunk_text"), String.class);
            Field<String> scLegacyRef = DSL.field(DSL.name("s", "legacy_ref"), String.class);
            // Object-typed for the NULL check below only — jOOQ's boolean
            // isNull() predicate doesn't care about the field's value type,
            // so no need for the Vector-typed accessor this early (impliedDim
            // isn't dim-resolved into a ChunkDim yet).
            Field<Object> scEmbeddingRaw = DSL.field(DSL.name("s", "embedding"), Object.class);
            Field<JSONB> scChunkMeta = DSL.field(DSL.name("s", "chunk_meta"), JSONB.class);
            Field<byte[]> scDigest = ChashSqlIdioms.digestField(scChunkText);

            // Per-tenant serialization (review P1 High: TOCTOU between the
            // C1 guard SELECT and the alias INSERT under READ COMMITTED —
            // a concurrent promote could commit a conflicting alias in the
            // gap and the ON CONFLICT DO NOTHING would keep it SILENTLY).
            // The xact-scoped advisory lock serializes every promote AND
            // finalize for one tenant; released automatically at txn end.
            // jOOQ DSL rendering (nexus-4okz4 increment 3): mirrors
            // RekeyOps' identical advisory-lock rendering exactly.
            ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
                       DSL.function("hashtext", Integer.class,
                           DSL.val("staging:").concat(currentTenantSetting()))))
               .fetch();

            // (1) dim precheck — every staged content row must carry the
            // name-implied dim (land-time classification already renamed
            // mislabeled collections to their honest target).
            Integer badDim = ctx.selectCount().from(sc)
                .where(scCollection.eq(collection))
                .and(scDim.ne(impliedDim))
                .fetchOne(0, Integer.class);
            if (badDim != null && badDim > 0) {
                throw new PromotePreconditionException(
                    badDim + " staged row(s) in '" + collection + "' carry a dim "
                    + "differing from the collection name's implied " + impliedDim
                    + " — the land-time classification must rename mislabeled "
                    + "sources to their honest target (nexus-nb7hr), never "
                    + "promote a name/dim disagreement");
            }
            Integer nullVec = ctx.selectCount().from(sc)
                .where(scCollection.eq(collection))
                .and(scChunkText.ne(""))
                .and(scEmbeddingRaw.isNull())
                .fetchOne(0, Integer.class);
            if (nullVec != null && nullVec > 0) {
                throw new PromotePreconditionException(
                    nullVec + " staged content row(s) in '" + collection + "' have "
                    + "no embedding — embed-fill staging before promote (reuse "
                    + "was not legal for these rows)");
            }

            // (2) C1 GUARD: staged refs vs COMMITTED alias state, tenant-wide.
            var conflict = ctx.select(
                    scLegacyRef,
                    hex(CHASH_ALIAS.NEW_CHASH),
                    hex(scDigest),
                    CHASH_ALIAS.SOURCE)
                .from(sc)
                .join(CHASH_ALIAS).on(CHASH_ALIAS.OLD_REF.eq(scLegacyRef))
                .where(scCollection.eq(collection))
                .and(scChunkText.ne(""))
                .and(CHASH_ALIAS.NEW_CHASH.isDistinctFrom(scDigest))
                .fetchAny();
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
            counts.put("alias_rows", ctx.insertInto(CHASH_ALIAS,
                    CHASH_ALIAS.TENANT_ID, CHASH_ALIAS.OLD_REF, CHASH_ALIAS.OLD_BYTES,
                    CHASH_ALIAS.NEW_CHASH, CHASH_ALIAS.SOURCE)
                .select(ctx.select(
                        currentTenantSetting(),
                        scLegacyRef,
                        ChashSqlIdioms.chashOldBytesField(scLegacyRef),
                        scDigest,
                        DSL.val("staging:").concat(scCollection))
                    .from(sc)
                    .where(scCollection.eq(collection))
                    .and(scChunkText.ne(""))
                    .and(scLegacyRef.ne(hex(scDigest))))
                .onConflict(CHASH_ALIAS.TENANT_ID, CHASH_ALIAS.OLD_REF)
                .doNothing()
                .execute());

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
            ctx.insertInto(CATALOG_COLLECTIONS,
                    CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE)
                .values(currentTenantSetting(), DSL.val(collection), DSL.val(contentType))
                .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
                .doNothing()
                .execute();

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
            // jOOQ DSL rendering (nexus-4okz4 increment 3): DISTINCT ON via
            // selectDistinct(...).on(...), the PostgreSQL-specific jOOQ
            // step (SelectSelectStep extends SelectDistinctOnStep) —
            // identical semantics to the deleted raw SQL's
            // `SELECT DISTINCT ON (...)`. The boolean tiebreak
            // `(legacy_ref = digest_hex) DESC` renders via
            // DSL.field(Condition) (jOOQ's documented condition-as-field
            // wrapper) + .desc(). See the class javadoc for the dropped
            // `::vector(<dim>)` cast's equivalence justification.
            ChunkDim dim = chunkDim(impliedDim);
            // Vector-typed accessor for THIS dim's target column — reuses the
            // target column's own DataType (carries the VectorBinding) so
            // insertInto(...).select(...) type-checks against a matching
            // Field<Vector> on both sides, per-dim (the DataType instance
            // differs by table even though the Java type and binding are
            // structurally identical across all three).
            Field<Vector> scEmbedding = DSL.field(DSL.name("s", "embedding"), dim.embedding().getDataType());
            Field<JSONB> promotedStamp = ChashSqlIdioms.jsonbBuildObject(
                "chunk_text_hash", hex(scDigest));
            int promoted = ctx.insertInto(dim.table(),
                    dim.tenantId(), dim.collection(), dim.chash(), dim.chunkText(),
                    dim.embedding(), dim.metadata())
                .select(ctx.selectDistinct(
                        currentTenantSetting(), scCollection, scDigest, scChunkText, scEmbedding,
                        ChashSqlIdioms.mergeMetadata(scChunkMeta, promotedStamp))
                    .on(scDigest)
                    .from(sc)
                    .where(scCollection.eq(collection))
                    .and(scChunkText.ne(""))
                    .orderBy(scDigest, DSL.field(scLegacyRef.eq(hex(scDigest))).desc(), scLegacyRef))
                .onConflict(dim.tenantId(), dim.collection(), dim.chash())
                .doNothing()
                .execute();
            counts.put("promoted", promoted);
            Integer stagedContent = ctx.selectCount().from(sc)
                .where(scCollection.eq(collection))
                .and(scChunkText.ne(""))
                .fetchOne(0, Integer.class);
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

            // staging.chunks accessors (house pattern — see class javadoc).
            var sc = DSL.table(DSL.name("staging", "chunks")).as("s");
            Field<String> scCollection = DSL.field(DSL.name("s", "collection"), String.class);
            Field<String> scChunkText = DSL.field(DSL.name("s", "chunk_text"), String.class);
            Field<String> scLegacyRef = DSL.field(DSL.name("s", "legacy_ref"), String.class);
            Field<Integer> scDim = DSL.field(DSL.name("s", "dim"), Integer.class);
            // Object-typed for the isNotNull() predicate below only; the
            // orphan-synthesize INSERT's select list needs a SEPARATE
            // Vector-typed accessor (scEmbeddingTyped, built where it's used)
            // matching the target ChunkDim's embedding Field's exact Java
            // type (dim-selected via DimTables.embeddingColumn — see
            // chunkDim(int)) so insertInto(...).select(...) type-checks.
            Field<Object> scEmbeddingRaw = DSL.field(DSL.name("s", "embedding"), Object.class);
            Field<JSONB> scChunkMeta = DSL.field(DSL.name("s", "chunk_meta"), JSONB.class);

            // Same per-tenant serialization as promoteCollection: finalize
            // must never run concurrently with an in-flight promote (review
            // P1 Medium: Item8's tenant-wide visibility would miss an
            // uncommitted promote's aliases and could drop a resolvable
            // reference).
            ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
                       DSL.function("hashtext", Integer.class,
                           DSL.val("staging:").concat(currentTenantSetting()))))
               .fetch();

            // (1) Item8, tenant-wide (C4): staged empty-text rows.
            //     reference-only = the ref resolves through the alias (content
            //     landed in ANY collection) or is already-canonical for a live
            //     chunk. Orphans get the policy.
            // jOOQ DSL rendering (nexus-4okz4 increment 3): the self-join
            // against staging.chunks uses a SECOND, DISTINCTLY-named alias
            // ("c2", matching the deleted raw SQL's own alias exactly) —
            // never the bare `sc` reference — so this does not repeat the
            // RekeyOps C1 same-table-shadowing class: `sc` ("s") and this
            // subquery's table ("c2") are two genuinely different alias
            // names, not one unaliased constant referenced twice.
            var c2 = DSL.table(DSL.name("staging", "chunks")).as("c2");
            Field<String> c2LegacyRef = DSL.field(DSL.name("c2", "legacy_ref"), String.class);
            Field<String> c2ChunkText = DSL.field(DSL.name("c2", "chunk_text"), String.class);
            Condition orphanCond = scChunkText.eq("")
                .and(DSL.notExists(ctx.selectOne().from(CHASH_ALIAS)
                    .where(CHASH_ALIAS.OLD_REF.eq(scLegacyRef))))
                .and(DSL.notExists(ctx.selectOne().from(c2)
                    .where(c2LegacyRef.eq(scLegacyRef))
                    .and(c2ChunkText.ne(""))));
            counts.put("reference_only_resolved", ctx.selectCount().from(sc)
                .where(scChunkText.eq(""))
                .and(DSL.exists(ctx.selectOne().from(CHASH_ALIAS)
                    .where(CHASH_ALIAS.OLD_REF.eq(scLegacyRef))))
                .fetchOne(0, Integer.class));
            if (synthesizeOrphans) {
                // Alias the surrogates FIRST so pointer promotion below
                // repoints them (the RekeyOps ordering, verbatim rationale).
                // Synthetic-seed concat + digest: reuses ChashSqlIdioms.
                // digestField (the shared sha256(convert_to(_, 'UTF8'))
                // formula, generic over ANY Field<String> seed) exactly as
                // RekeyOps' own orphan-synthesize branch does — same idiom,
                // staged-source operands.
                Field<String> synthSeed = DSL.val("nexus:synthetic-chash:v1|")
                    .concat(currentTenantSetting())
                    .concat(DSL.val("|"))
                    .concat(scCollection)
                    .concat(DSL.val("|"))
                    .concat(scLegacyRef);
                Field<byte[]> synthChash = ChashSqlIdioms.digestField(synthSeed);
                ctx.insertInto(CHASH_ALIAS,
                        CHASH_ALIAS.TENANT_ID, CHASH_ALIAS.OLD_REF, CHASH_ALIAS.OLD_BYTES,
                        CHASH_ALIAS.NEW_CHASH, CHASH_ALIAS.SOURCE)
                    .select(ctx.select(
                            currentTenantSetting(), scLegacyRef,
                            ChashSqlIdioms.chashOldBytesField(scLegacyRef),
                            synthChash, DSL.val("staging:synthetic"))
                        .from(sc)
                        .where(orphanCond))
                    .onConflict(CHASH_ALIAS.TENANT_ID, CHASH_ALIAS.OLD_REF)
                    .doNothing()
                    .execute();
                // chunk_text_hash mirrors the SURROGATE chash (RDR-086
                // metadata parity, same rationale as the content INSERT).
                //
                // FIXED (nexus-o8dil.50, RDR-191 P4): this branch used to be
                // hardcoded to chunks_768/dim=768 only, "preserved verbatim"
                // from the deleted raw SQL. That was not a merely-incomplete
                // convenience: the CHASH_ALIAS insert directly above is
                // dim-agnostic (aliases EVERY row matching orphanCond,
                // regardless of scDim), so a 384/1024 orphan got a committed
                // alias with NO backing content row in any dim table. If a
                // staged manifest pointer (staging.document_chunks) later
                // resolved through that alias — precisely the case Item8
                // exists to handle — this method's own dangling_manifest
                // fatal check (below, (7) in-txn verify) would find a
                // catalog_document_chunks row whose chash has no content
                // anywhere and throw IllegalStateException, ABORTING THE
                // WHOLE finalizeTenant TRANSACTION for the tenant. Because
                // finalize is documented idempotent/re-runnable, every retry
                // hits the identical alias again — a repeatable, tenant-wide
                // finalize blocker, not a silent skip.
                //
                // Both non-768 dims are reachable in production, not
                // hypothetical: 384 is Tier-0 MiniLM (nexus.db.local_ef,
                // the fastembed-less local-mode default — installs without
                // `conexus[local]` embed at 384d), 1024 is Voyage (the
                // cloud-mode default). staging.chunks.dim is a VALUE column
                // by design for exactly this reason — see
                // staging-001-landing-tables.xml's own comment: "mixed dims
                // legal, no ANN index — never searched". A guided-upgrade
                // migration from either of those installs can legitimately
                // land 384/1024 orphan rows.
                //
                // Fix: loop over all three dims via the same chunkDim(int)
                // accessor promoteCollection's content INSERT (5) already
                // uses, explicit-three (not a loop over a shared dim
                // registry — same no-common-typed-interface discipline as
                // RekeyOps.DIMS / ChashSqlIdioms.danglingManifestCountDsl,
                // see the chunkDim javadoc). "orphans_synthesized" stays a
                // single summed count — same convention residual_mismatched
                // already uses for its three explicit per-dim calls.
                Field<JSONB> synthStamp = ChashSqlIdioms.jsonbBuildObject(
                    "chash_origin", "synthetic", "chunk_text_hash", hex(CHASH_ALIAS.NEW_CHASH));
                int synthesized = 0;
                for (int d : new int[] {384, 768, 1024}) {
                    ChunkDim dim = chunkDim(d);
                    Field<Vector> scEmbeddingTyped =
                        DSL.field(DSL.name("s", "embedding"), dim.embedding().getDataType());
                    synthesized += ctx.insertInto(dim.table(),
                            dim.tenantId(), dim.collection(), dim.chash(),
                            dim.chunkText(), dim.embedding(), dim.metadata())
                        .select(ctx.select(
                                currentTenantSetting(), scCollection, CHASH_ALIAS.NEW_CHASH, DSL.val(""),
                                scEmbeddingTyped, ChashSqlIdioms.mergeMetadata(scChunkMeta, synthStamp))
                            .from(sc)
                            .join(CHASH_ALIAS).on(CHASH_ALIAS.OLD_REF.eq(scLegacyRef)
                                .and(CHASH_ALIAS.SOURCE.eq("staging:synthetic")))
                            .where(scChunkText.eq(""))
                            .and(scDim.eq(d))
                            .and(scEmbeddingRaw.isNotNull()))
                        .onConflict(dim.tenantId(), dim.collection(), dim.chash())
                        .doNothing()
                        .execute();
                }
                counts.put("orphans_synthesized", synthesized);
                counts.put("orphans_dropped", 0);
            } else {
                counts.put("orphans_synthesized", 0);
                counts.put("orphans_dropped", ctx.selectCount().from(sc)
                    .where(orphanCond)
                    .fetchOne(0, Integer.class));
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
            // jOOQ DSL rendering (nexus-4okz4 increment 3, converged onto
            // the shared home increment 5): ChashSqlIdioms.existsInAnyDim(
            // ctx, sChashDecoded) replaces the raw canonExists string,
            // reused identically across all three call sites below. This
            // class no longer carries its own private copy of the
            // three-way EXISTS disjunction (see ChashSqlIdioms' javadoc).

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
            // nexus-4okz4 increment 3: the remaining staging.document_chunks
            // columns the manifest INSERT/UPDATE below need — same alias
            // ("s") as sChash above, safe to share (immutable Field
            // handles, no per-statement state).
            Field<String> sDocId = DSL.field(DSL.name("s", "doc_id"), String.class);
            Field<Integer> sPosition = DSL.field(DSL.name("s", "position"), Integer.class);
            Field<Integer> sChunkIndex = DSL.field(DSL.name("s", "chunk_index"), Integer.class);
            Field<Integer> sLineStart = DSL.field(DSL.name("s", "line_start"), Integer.class);
            Field<Integer> sLineEnd = DSL.field(DSL.name("s", "line_end"), Integer.class);
            Field<Integer> sCharStart = DSL.field(DSL.name("s", "char_start"), Integer.class);
            Field<Integer> sCharEnd = DSL.field(DSL.name("s", "char_end"), Integer.class);
            // nexus-o8dil.7 (RDR-191 GATE-2): the row's own resolved chash --
            // shared between `cand` below (the batch-wide distinct list used
            // for sweep-gate acquisition) and the per-row verified manifest
            // resolution further down (which needs the SAME chash, not the
            // batch's whole set).
            Field<byte[]> resolvedChash = DSL.coalesce(CHASH_ALIAS.NEW_CHASH, sChashDecoded);
            var cand = ctx.selectDistinct(resolvedChash.as("chash"))
                .from(sdc)
                .leftJoin(CHASH_ALIAS).on(CHASH_ALIAS.OLD_REF.eq(sChash))
                .where(CHASH_ALIAS.NEW_CHASH.isNotNull().or(sChash.likeRegex("^[0-9a-f]{64}$")))
                .asTable("cand");
            // RDR-191 repoint (nexus-o8dil.17): the former three-dim UNION
            // ALL collapses to a single SELECT — nexus.chunks is now the
            // single table every dim's content lives in (dim-independent
            // PK: tenant_id, collection, chash), so it already IS the union
            // the three branches used to compute; re-unioning it with
            // itself would triple-count. Same collapse as RekeyOps' own
            // step-2b `phys` (see T2 nexus/rdr-191-batch-D3-2026-08-13).
            var phys = ctx.select(CHUNKS.COLLECTION, CHUNKS.CHASH).from(CHUNKS).asTable("phys");
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

            Condition manifestResolvable = CHASH_ALIAS.NEW_CHASH.isNotNull()
                .or(sChash.likeRegex("^[0-9a-f]{64}$").and(ChashSqlIdioms.existsInAnyDim(ctx, sChashDecoded)));
            // nexus-o8dil.3 (RDR-191 F12b(ii), verified anchor sheet finding
            // C): this INSERT's 9-column list omitted `collection` entirely
            // -- every finalize-promoted manifest row was a partial-NULL FK
            // key, unlike promoteCollection's CONTENT insert above (:420-433),
            // which DOES stamp it. Stamped from catalog_documents.
            // physical_collection (NULL-if-empty for ghost/sourceless docs)
            // -- the one caller-known fact this bulk migration path has,
            // mirroring the explicit `collection` parameter every live
            // writer (CatalogRepository.writeManifestRows/
            // appendManifestChunks/importChunksBatch) now requires the
            // caller to supply directly -- NOT from wherever the resolved
            // chash's content happens to physically live, which is a
            // DIFFERENT, collection-agnostic question (canonExists / the
            // per-collection sweep-gate resolution a few lines above) that
            // can legitimately diverge under shared-chash reuse across
            // collections. A manifest row's `collection` is the eventual
            // FK's join key against the OWNING document's placement, not a
            // content-location lookup.
            Field<String> docPhysicalCollection =
                DSL.nullif(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "");
            // RDR-191 (Hal ruling 2026-08-12, nexus-lyhac): writers STAMP
            // what they already know; they never INFER it. This bulk
            // migration path's one caller-known fact about a staged chunk
            // row's collection is the OWNING DOCUMENT's own registered
            // `physical_collection` -- `docPhysicalCollection` above. Before
            // this fix the INSERT below stamped it UNCONDITIONALLY,
            // including when it was NULL (a doc_id staged here with no
            // catalog_documents row at all, or a ghost document that has
            // never had `physical_collection` set) -- emitting a literal
            // NULL into a column collection now disallows (catalog-025-
            // collection-not-null.xml is NOT NULL, no sentinel, no
            // DEFAULT).
            //
            // The fix mirrors the ROOT ruling exactly, not a new mechanism:
            // an unresolvable row is not written. A staged row whose doc has
            // no known `physical_collection` simply stays staged -- a later
            // finalize (this method is explicitly idempotent/re-runnable,
            // see the class javadoc) converges it once the document is
            // registered with one. No per-chash content-location lookup, no
            // sibling tiebreak: `docPhysicalCollection` is a per-DOCUMENT
            // fact, correctly scalar, and is the ONLY source this method
            // stamps from.
            Condition manifestPromotable = manifestResolvable.and(docPhysicalCollection.isNotNull());
            counts.put("manifest_promoted", ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                    CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.LINE_START,
                    CATALOG_DOCUMENT_CHUNKS.LINE_END, CATALOG_DOCUMENT_CHUNKS.CHAR_START,
                    CATALOG_DOCUMENT_CHUNKS.CHAR_END, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .select(ctx.select(
                        currentTenantSetting(), sDocId, sPosition,
                        resolvedChash,
                        sChunkIndex, sLineStart, sLineEnd, sCharStart, sCharEnd,
                        docPhysicalCollection)
                    .from(sdc)
                    .leftJoin(CHASH_ALIAS).on(CHASH_ALIAS.OLD_REF.eq(sChash))
                    .leftJoin(CATALOG_DOCUMENTS).on(CATALOG_DOCUMENTS.TUMBLER.eq(sDocId))
                    .where(manifestPromotable))
                .onConflict(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION)
                .doNothing()
                .execute());
            counts.put("manifest_unresolved", ctx.selectCount().from(sdc)
                .where(DSL.notExists(ctx.selectOne().from(CHASH_ALIAS)
                    .where(CHASH_ALIAS.OLD_REF.eq(sChash))))
                .and(DSL.not(sChash.likeRegex("^[0-9a-f]{64}$").and(ChashSqlIdioms.existsInAnyDim(ctx, sChashDecoded))))
                .fetchOne(0, Integer.class));
            // nexus-s13u0 visibility: DISTINCT staged doc_ids with NO
            // catalog_documents row at all -- a NARROWER diagnostic than
            // `manifestPromotable`'s NOT-NULL gate above (which also skips a
            // registered GHOST document with no physical_collection yet).
            // Reported via a counter rather than a throw: aborting the
            // WHOLE finalize transaction over one staged row referencing an
            // unregistered doc_id would be disproportionate for this
            // idempotent/re-runnable method (class javadoc) -- a later
            // finalize converges it once the document exists.
            var docsNotRegistered = ctx.selectDistinct(sDocId).from(sdc).asTable("dnr");
            int docNotRegisteredCount = ctx.selectCount()
                .from(docsNotRegistered)
                .where(DSL.notExists(ctx.selectOne().from(CATALOG_DOCUMENTS)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(currentTenantSetting()))
                    .and(CATALOG_DOCUMENTS.TUMBLER.eq(docsNotRegistered.field("doc_id", String.class)))))
                .fetchOne(0, Integer.class);
            counts.put("manifest_doc_not_registered", docNotRegisteredCount);
            // review finding (nexus-s13u0 follow-up): a counter alone is
            // WEAKER than a throw against the fail-loud directive -- nobody
            // reads a counter unless something already told them to look,
            // and this method's own log.info("event=staging_finalize"...)
            // below fires unconditionally, at INFO, burying this alongside
            // routine success counts rather than calling it out. Escalate
            // explicitly at WARN when nonzero, the same discipline
            // insertManifestChunkRows' case-3 SKIP and writeManifestMany's
            // aggregate failure log already apply, so this is at minimum
            // one structured-log grep away from being found rather than
            // silently buried in a per-tenant JSON blob nobody is currently
            // wired to read (verified: zero Python callers of
            // POST /v1/staging/finalize anywhere in src/nexus/, and the one
            // E2E rehearsal script that exercises it asserts on five
            // unrelated fields, never this one or its pre-existing sibling
            // manifest_unresolved). A WARN log is still not a metric or an
            // alert path -- wiring either is follow-up-bead scale, not a
            // fit for this guard-contract change.
            if (docNotRegisteredCount > 0) {
                log.warn("event=staging_finalize_doc_not_registered tenant={} count={}",
                    tenant, docNotRegisteredCount);
            }

            // nexus-0dkdx (RDR-191 GATE-2, substantive-critic round-2 finding,
            // T2 nexus/critique-round2-nexus-j862l-test-reconciliation-2026-08-12
            // [22340]): the OTHER half of `manifestPromotable`'s gate --
            // `docPhysicalCollection.isNotNull()` -- was silent. A doc_id that
            // IS registered (unlike the case above) but whose
            // `physical_collection` is NULL/empty (a ghost/sourceless doc,
            // see the `docPhysicalCollection` commentary above) produces NO
            // manifest row for its staged chunks, with zero counter and zero
            // log signal -- unlike its sibling `manifest_doc_not_registered`
            // case, which got both in the same round. A ghost doc's staged
            // chunks could sit stuck indefinitely with nothing to find it.
            // Mirrors `manifest_doc_not_registered` exactly: DISTINCT staged
            // doc_ids whose `catalog_documents` row EXISTS (that's the
            // difference from the case above) but resolves to a NULL/empty
            // `physical_collection`.
            var docsNoCollection = ctx.selectDistinct(sDocId).from(sdc).asTable("dnc");
            int docNoCollectionCount = ctx.selectCount()
                .from(docsNoCollection)
                .where(DSL.exists(ctx.selectOne().from(CATALOG_DOCUMENTS)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(currentTenantSetting()))
                    .and(CATALOG_DOCUMENTS.TUMBLER.eq(docsNoCollection.field("doc_id", String.class)))
                    .and(DSL.nullif(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "").isNull())))
                .fetchOne(0, Integer.class);
            counts.put("manifest_doc_no_collection", docNoCollectionCount);
            if (docNoCollectionCount > 0) {
                log.warn("event=staging_finalize_doc_no_collection tenant={} count={}",
                    tenant, docNoCollectionCount);
            }

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
            // jOOQ DSL rendering (nexus-4okz4 increment 3): UPDATE...FROM a
            // joined derived table (sd LEFT JOIN m), identical predicate —
            // no deleted_at filter added (would change tombstone-exempt
            // behavior, out of scope for a representation-only pass).
            var sdSub = ctx.selectDistinct(sDocId).from(sdc).asTable("sd");
            var mSub = ctx.select(CATALOG_DOCUMENT_CHUNKS.DOC_ID, DSL.count().as("cnt"))
                .from(CATALOG_DOCUMENT_CHUNKS)
                .groupBy(CATALOG_DOCUMENT_CHUNKS.DOC_ID)
                .asTable("m");
            Field<String> sdDocId = sdSub.field("doc_id", String.class);
            Field<String> mDocId = mSub.field("doc_id", String.class);
            Field<Integer> mCnt = mSub.field("cnt", Integer.class);
            counts.put("chunk_count_resynced", ctx.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.CHUNK_COUNT, DSL.coalesce(mCnt, 0))
                .from(sdSub.leftJoin(mSub).on(mDocId.eq(sdDocId)))
                .where(CATALOG_DOCUMENTS.TUMBLER.eq(sdDocId))
                .and(CATALOG_DOCUMENTS.CHUNK_COUNT.isDistinctFrom(DSL.coalesce(mCnt, 0)))
                .execute());

            // (2c) nexus-b6enc F3: store_put-origin docs (content_type =
            // 'knowledge', empty file_path) have NO source file — an
            // unresolved pointer for one can never converge via re-index, so
            // it must be surfaced BY TITLE for the user to re-store.
            // TOMBSTONE-EXEMPT (nexus-mqd6t): same migration-leg sanction as the
            // chunk_count_resync UPDATE above. See TombstoneFilterGateTest.TOMBSTONE_EXEMPT.
            // jOOQ DSL rendering (nexus-4okz4 increment 3): ORDER BY 1 in the
            // deleted raw SQL is positionally the sole SELECT column
            // (d.title) — equivalent to ordering by CATALOG_DOCUMENTS.TITLE
            // directly.
            List<String> unresolvedKnowledgeTitles = ctx.selectDistinct(CATALOG_DOCUMENTS.TITLE)
                .from(CATALOG_DOCUMENTS)
                .join(sdc).on(sDocId.eq(CATALOG_DOCUMENTS.TUMBLER))
                .where(CATALOG_DOCUMENTS.CONTENT_TYPE.eq("knowledge"))
                .and(DSL.coalesce(CATALOG_DOCUMENTS.FILE_PATH, "").eq(""))
                .and(DSL.notExists(ctx.selectOne().from(CHASH_ALIAS)
                    .where(CHASH_ALIAS.OLD_REF.eq(sChash))))
                .and(DSL.not(sChash.likeRegex("^[0-9a-f]{64}$").and(ChashSqlIdioms.existsInAnyDim(ctx, sChashDecoded))))
                .orderBy(CATALOG_DOCUMENTS.TITLE)
                .limit(100)
                .fetch(CATALOG_DOCUMENTS.TITLE);
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
            // jOOQ DSL rendering (nexus-4okz4 increment 3): staging.
            // topic_assignments accessors via the house pattern;
            // Field.notLikeRegex renders PostgreSQL's `!~`.
            var sta = DSL.table(DSL.name("staging", "topic_assignments")).as("s");
            Field<String> staDocId = DSL.field(DSL.name("s", "doc_id"), String.class);
            Field<String> staTopicLabel = DSL.field(DSL.name("s", "topic_label"), String.class);
            Field<String> staTopicCollection = DSL.field(DSL.name("s", "topic_collection"), String.class);
            counts.put("topic_assignments_promoted", ctx.insertInto(TOPIC_ASSIGNMENTS,
                    TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID)
                .select(ctx.selectDistinct(
                        currentTenantSetting(),
                        DSL.coalesce(hex(CHASH_ALIAS.NEW_CHASH), staDocId),
                        TOPICS.ID)
                    .from(sta)
                    .join(TOPICS).on(TOPICS.LABEL.eq(staTopicLabel)
                        .and(TOPICS.COLLECTION.eq(staTopicCollection)))
                    .leftJoin(CHASH_ALIAS).on(CHASH_ALIAS.OLD_REF.eq(staDocId))
                    .where(CHASH_ALIAS.NEW_CHASH.isNotNull()
                        .or(staDocId.notLikeRegex("^([0-9a-f]{16}|[0-9a-f]{32})$"))))
                .onConflict(TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID)
                .doNothing()
                .execute());
            counts.put("topic_assignments_unresolved", ctx.selectCount().from(sta)
                .where(DSL.notExists(ctx.selectOne().from(TOPICS)
                    .where(TOPICS.LABEL.eq(staTopicLabel).and(TOPICS.COLLECTION.eq(staTopicCollection)))))
                .fetchOne(0, Integer.class));

            // (4) frecency: GREATEST-merge from the staged rows through the
            //     alias — staging-sourced twin of ChashSqlIdioms'
            //     frecencyAliasAggregate (same semantics, staged source; NOT
            //     the same query as RekeyOps' frecencyAliasAggregateDsl —
            //     that one reads FROM nexus.frecency INNER JOIN chash_alias,
            //     this one reads FROM staging.frecency LEFT JOIN chash_alias
            //     with a chash-shape fallback predicate — genuinely
            //     different source/join shape, not reusable as-is).
            // jOOQ DSL rendering (nexus-4okz4 increment 3): the GROUP BY
            // uses the UNALIASED target-id expression object (not the
            // `.as("target_id")` decorated one used in the SELECT list) so
            // the rendered SQL groups by the full COALESCE(...) expression,
            // matching the raw form's `GROUP BY 1` (positional — the sole
            // non-aggregate SELECT column) without relying on PostgreSQL's
            // group-by-output-alias extension.
            //
            // PIN (empirically found via StagingPromoteOpsIntegrationTest,
            // not anticipated by review): targetIdExpr's `encode(..., 'hex')`
            // MUST use DSL.inline("hex"), not the shared hex() helper's
            // DSL.val("hex") — this expression is rendered TWICE in the
            // final SQL text (once for the SELECT list, once for GROUP BY),
            // and jOOQ mints an INDEPENDENT bind placeholder per textual
            // occurrence even for the identical Java Field object. PostgreSQL
            // validates GROUP BY coverage by PARSE-TREE structural equality
            // BEFORE parameter binding, so two occurrences of
            // `encode(chash_alias.new_chash, $N)` with DIFFERENT parameter
            // numbers are NOT recognized as the same expression — Postgres
            // then reports `chash_alias.new_chash` as absent from GROUP BY,
            // even though the SAME value is bound to both placeholders at
            // execution time. DSL.inline(...) renders the literal directly
            // into the SQL text (no placeholder), so both occurrences are
            // byte-for-byte identical and the parser's equality check
            // passes. Bare-column GROUP BY targets (e.g. RekeyOps'
            // frecencyAliasAggregateDsl, which groups by CHASH_ALIAS.NEW_CHASH
            // directly) never hit this — a plain column reference has no
            // embedded literal to duplicate.
            var sf = DSL.table(DSL.name("staging", "frecency")).as("s");
            Field<String> sfChunkId = DSL.field(DSL.name("s", "chunk_id"), String.class);
            Field<Double> sfFrecencyScore = DSL.field(DSL.name("s", "frecency_score"), Double.class);
            Field<Integer> sfMissCount = DSL.field(DSL.name("s", "miss_count"), Integer.class);
            Field<String> sfLastHitAt = DSL.field(DSL.name("s", "last_hit_at"), String.class);
            Field<String> sfEmbeddedAt = DSL.field(DSL.name("s", "embedded_at"), String.class);
            Field<Integer> sfTtlDays = DSL.field(DSL.name("s", "ttl_days"), Integer.class);
            Field<String> targetIdExpr = DSL.coalesce(
                DSL.function("encode", String.class, CHASH_ALIAS.NEW_CHASH, DSL.inline("hex")),
                sfChunkId);
            var stagedFrecencyAgg = ctx.select(
                    targetIdExpr.as("target_id"),
                    DSL.max(sfFrecencyScore).as("fs"),
                    DSL.max(sfMissCount).as("mc"),
                    DSL.max(parseStagedTimestamp(sfLastHitAt)).as("lh"),
                    DSL.max(parseStagedTimestamp(sfEmbeddedAt)).as("ea"),
                    DSL.max(sfTtlDays).as("td"))
                .from(sf)
                .leftJoin(CHASH_ALIAS).on(sfChunkId.eq(CHASH_ALIAS.OLD_REF))
                .where(CHASH_ALIAS.NEW_CHASH.isNotNull().or(sfChunkId.likeRegex("^[0-9a-f]{64}$")))
                .groupBy(targetIdExpr)
                .asTable("g");
            Field<String> gTargetId = stagedFrecencyAgg.field("target_id", String.class);
            Field<Double> gFs = stagedFrecencyAgg.field("fs", Double.class);
            Field<Integer> gMc = stagedFrecencyAgg.field("mc", Integer.class);
            Field<OffsetDateTime> gLh = stagedFrecencyAgg.field("lh", OffsetDateTime.class);
            Field<OffsetDateTime> gEa = stagedFrecencyAgg.field("ea", OffsetDateTime.class);
            Field<Integer> gTd = stagedFrecencyAgg.field("td", Integer.class);
            ctx.update(FRECENCY)
                .set(FRECENCY.FRECENCY_SCORE, DSL.greatest(FRECENCY.FRECENCY_SCORE, gFs))
                .set(FRECENCY.MISS_COUNT, DSL.greatest(FRECENCY.MISS_COUNT, gMc))
                .set(FRECENCY.LAST_HIT_AT, DSL.greatest(FRECENCY.LAST_HIT_AT, gLh))
                .set(FRECENCY.EMBEDDED_AT, DSL.greatest(FRECENCY.EMBEDDED_AT, gEa))
                .set(FRECENCY.TTL_DAYS, DSL.greatest(FRECENCY.TTL_DAYS, gTd))
                .from(stagedFrecencyAgg)
                .where(FRECENCY.CHUNK_ID.eq(gTargetId))
                .execute();
            counts.put("frecency_promoted", ctx.insertInto(FRECENCY,
                    FRECENCY.TENANT_ID, FRECENCY.CHUNK_ID, FRECENCY.EMBEDDED_AT, FRECENCY.TTL_DAYS,
                    FRECENCY.FRECENCY_SCORE, FRECENCY.MISS_COUNT, FRECENCY.LAST_HIT_AT)
                .select(ctx.select(currentTenantSetting(), gTargetId, gEa, gTd, gFs, gMc, gLh)
                    .from(stagedFrecencyAgg)
                    .where(DSL.notExists(ctx.selectOne().from(FRECENCY)
                        .where(FRECENCY.CHUNK_ID.eq(gTargetId)))))
                .execute());

            // (5) relevance_log: BIGSERIAL target, no natural key — anti-join
            //     on the full staged identity for idempotent re-finalize.
            // jOOQ DSL rendering (nexus-4okz4 increment 3): resolvedChunkId
            // / resolvedTs are expression objects reused verbatim in BOTH
            // the SELECT list and the NOT EXISTS subquery, mirroring the
            // raw SQL's own textual duplication of
            // `COALESCE(encode(a.new_chash,'hex'), s.chunk_id)` and
            // `COALESCE(NULLIF(s.ts,'')::timestamptz, now())` — safe because
            // PostgreSQL's now()/CURRENT_TIMESTAMP is transaction-stable
            // (same value both occurrences within one statement, exactly as
            // the raw form relied on).
            var srl = DSL.table(DSL.name("staging", "relevance_log")).as("s");
            Field<String> srlQuery = DSL.field(DSL.name("s", "query"), String.class);
            Field<String> srlChunkId = DSL.field(DSL.name("s", "chunk_id"), String.class);
            Field<String> srlCollection = DSL.field(DSL.name("s", "collection"), String.class);
            Field<String> srlAction = DSL.field(DSL.name("s", "action"), String.class);
            Field<String> srlSessionId = DSL.field(DSL.name("s", "session_id"), String.class);
            Field<String> srlTs = DSL.field(DSL.name("s", "ts"), String.class);
            Field<String> resolvedChunkId = DSL.coalesce(hex(CHASH_ALIAS.NEW_CHASH), srlChunkId);
            Field<OffsetDateTime> resolvedTs = parseStagedTimestamp(srlTs);
            counts.put("relevance_log_promoted", ctx.insertInto(RELEVANCE_LOG,
                    RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY, RELEVANCE_LOG.CHUNK_ID,
                    RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.ACTION, RELEVANCE_LOG.SESSION_ID,
                    RELEVANCE_LOG.TIMESTAMP)
                .select(ctx.select(
                        currentTenantSetting(), srlQuery, resolvedChunkId, srlCollection,
                        srlAction, srlSessionId, resolvedTs)
                    .from(srl)
                    .leftJoin(CHASH_ALIAS).on(CHASH_ALIAS.OLD_REF.eq(srlChunkId))
                    .where(CHASH_ALIAS.NEW_CHASH.isNotNull().or(srlChunkId.likeRegex("^[0-9a-f]{64}$")))
                    .and(DSL.notExists(ctx.selectOne().from(RELEVANCE_LOG)
                        .where(RELEVANCE_LOG.QUERY.eq(srlQuery))
                        .and(RELEVANCE_LOG.CHUNK_ID.eq(resolvedChunkId))
                        .and(RELEVANCE_LOG.ACTION.eq(srlAction))
                        .and(RELEVANCE_LOG.TIMESTAMP.eq(resolvedTs)))))
                .execute());

            // (6) aspects (Class-D): anti-join on (collection, source_path).
            //     source_path/source_uri carry no in-flight rewrite here —
            //     chroma:// URI repoints ride the alias at READ time via the
            //     shared resolvers; staged values land verbatim.
            var sda = DSL.table(DSL.name("staging", "document_aspects")).as("s");
            Field<String> sdaCollection = DSL.field(DSL.name("s", "collection"), String.class);
            Field<String> sdaSourcePath = DSL.field(DSL.name("s", "source_path"), String.class);
            Field<String> sdaProblemFormulation = DSL.field(DSL.name("s", "problem_formulation"), String.class);
            Field<String> sdaProposedMethod = DSL.field(DSL.name("s", "proposed_method"), String.class);
            Field<String> sdaExperimentalDatasets =
                DSL.field(DSL.name("s", "experimental_datasets"), String.class);
            Field<String> sdaExperimentalBaselines =
                DSL.field(DSL.name("s", "experimental_baselines"), String.class);
            Field<String> sdaExperimentalResults =
                DSL.field(DSL.name("s", "experimental_results"), String.class);
            Field<String> sdaExtras = DSL.field(DSL.name("s", "extras"), String.class);
            Field<Double> sdaConfidence = DSL.field(DSL.name("s", "confidence"), Double.class);
            Field<String> sdaExtractedAt = DSL.field(DSL.name("s", "extracted_at"), String.class);
            Field<String> sdaModelVersion = DSL.field(DSL.name("s", "model_version"), String.class);
            Field<String> sdaExtractorName = DSL.field(DSL.name("s", "extractor_name"), String.class);
            Field<String> sdaSourceUri = DSL.field(DSL.name("s", "source_uri"), String.class);
            Field<String> sdaDocId = DSL.field(DSL.name("s", "doc_id"), String.class);
            counts.put("document_aspects_promoted", ctx.insertInto(DOCUMENT_ASPECTS,
                    DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION, DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS, DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS, DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE, DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION, DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI, DOCUMENT_ASPECTS.DOC_ID)
                .select(ctx.select(
                        currentTenantSetting(), sdaCollection, sdaSourcePath,
                        sdaProblemFormulation, sdaProposedMethod, sdaExperimentalDatasets,
                        sdaExperimentalBaselines, sdaExperimentalResults,
                        // nexus-cefa1.4: DOCUMENT_ASPECTS.EXTRAS is jsonb now — staging.
                        // document_aspects.extras stays TEXT (typeless by design), so the
                        // promote SELECT needs the explicit staged-TEXT-to-jsonb cast.
                        parseStagedJson(sdaExtras), sdaConfidence,
                        parseStagedTimestamp(sdaExtractedAt),
                        sdaModelVersion, sdaExtractorName, sdaSourceUri, sdaDocId)
                    .from(sda)
                    .where(DSL.notExists(ctx.selectOne().from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.COLLECTION.eq(sdaCollection))
                        .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(sdaSourcePath)))))
                .execute());
            var saq = DSL.table(DSL.name("staging", "aspect_extraction_queue")).as("s");
            Field<String> saqCollection = DSL.field(DSL.name("s", "collection"), String.class);
            Field<String> saqSourcePath = DSL.field(DSL.name("s", "source_path"), String.class);
            Field<String> saqDocId = DSL.field(DSL.name("s", "doc_id"), String.class);
            Field<String> saqContentHash = DSL.field(DSL.name("s", "content_hash"), String.class);
            Field<String> saqContent = DSL.field(DSL.name("s", "content"), String.class);
            Field<String> saqStatus = DSL.field(DSL.name("s", "status"), String.class);
            Field<Integer> saqRetryCount = DSL.field(DSL.name("s", "retry_count"), Integer.class);
            Field<String> saqEnqueuedAt = DSL.field(DSL.name("s", "enqueued_at"), String.class);
            Field<String> saqLastAttemptAt = DSL.field(DSL.name("s", "last_attempt_at"), String.class);
            Field<String> saqLastError = DSL.field(DSL.name("s", "last_error"), String.class);
            counts.put("aspect_queue_promoted", ctx.insertInto(ASPECT_EXTRACTION_QUEUE,
                    ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.DOC_ID,
                    ASPECT_EXTRACTION_QUEUE.CONTENT_HASH, ASPECT_EXTRACTION_QUEUE.CONTENT,
                    ASPECT_EXTRACTION_QUEUE.STATUS, ASPECT_EXTRACTION_QUEUE.RETRY_COUNT,
                    ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.LAST_ATTEMPT_AT,
                    ASPECT_EXTRACTION_QUEUE.LAST_ERROR)
                .select(ctx.select(
                        currentTenantSetting(), saqCollection, saqSourcePath, saqDocId, saqContentHash,
                        saqContent, saqStatus, saqRetryCount, parseStagedTimestamp(saqEnqueuedAt),
                        parseStagedTimestampNullable(saqLastAttemptAt), saqLastError)
                    .from(saq)
                    .where(DSL.notExists(ctx.selectOne().from(ASPECT_EXTRACTION_QUEUE)
                        .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(saqCollection))
                        .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq(saqSourcePath)))))
                .execute());

            // (7) in-txn verify (the census extension rides nexus-jxizy.10.5).
            // nexus-4okz4 increment 2: residualMismatchCount (the raw-string
            // form) was deleted when RekeyOps' step 6 converted to typed DSL
            // — this SHARED fragment's only remaining form is
            // ChashSqlIdioms.residualMismatchCountDsl.
            //
            // RDR-191 repoint (nexus-o8dil.17): collapsed from three
            // explicit per-table calls (one per former physical dim table)
            // to ONE — nexus.chunks is now the single table holding every
            // dim's content rows, dim-independent (tenant_id, collection,
            // chash) identity. Summing three calls against the SAME table
            // with the SAME predicate would now triple-count every
            // mismatched row (the DimTables D1 hazard: table membership no
            // longer implies dim). A single call over the unified table is
            // the byte-for-byte same predicate over the same row set that
            // the three summed calls used to cover — not an approximation.
            int residual = ChashSqlIdioms.residualMismatchCountDsl(
                ctx, CHUNKS, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT);
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
