// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.nexus.tables.CatalogDocumentChunks;
import dev.nexus.service.jooq.nexus.tables.CatalogDocuments;
import dev.nexus.service.jooq.nexus.tables.Chunks;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Query;
import org.jooq.Record4;
import org.jooq.Result;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The retire-keys sweep of RDR-236 Phase 4 Step 3 (nexus-6pfgb.43): strips every metadata key a chunk row may no longer
 * hold, per tenant, in batches, and counts what stands in its way.
 *
 * <p><b>Allow-list shaped.</b> A row keeps only the keys of the {@link ChunkKeyPlan} (CHUNK and ROW) and loses every
 * other: a deny-list would leave any key nobody listed and the CHECK the add-constraint route puts behind this sweep
 * would then refuse the row. The rows it looks at are those where what is left of the metadata after the allowed keys are
 * taken off is not empty. A row whose metadata is not a JSON object cannot be rewritten by key; it is skipped and counted
 * ({@code non_object_metadata_rows}), never selected again and again.
 *
 * <p><b>What it never touches.</b> {@code quarantine-*} and {@code taxonomy__*} rows (the plan's bypass prefixes; the
 * quarantine restore strips its own rows to CHUNK keys, nexus-l003p, so they never carry a retired key into a live
 * collection); a row whose chash is not 32 bytes (the existing NOT VALID {@code chunks_chash_octet_check} would refuse
 * the UPDATE, RF-23: counted, not swept); and a collection whose manifest-less census reads {@code legacy-unmanifested}
 * above zero or could not be read (the census's forward join reads {@code catalog_doc_id}/{@code doc_id} off the row,
 * RF-19, so the sweep would blind it: the collection is skipped and reported as blocked).
 *
 * <p><b>Timestamp neutral.</b> The UPDATE assigns {@code metadata} and nothing else, so {@code last_written_at} (the
 * reaper's grace anchor, nexus-wbfpw.43) keeps its value. The statement is typed jOOQ, one row per chash, in the
 * {@code (collection, chash)} order the client writers lock in (nexus-hxrcm), over rows taken {@code FOR UPDATE SKIP
 * LOCKED} so a concurrent writer is never waited for.
 *
 * <p><b>Keyset, not rescan.</b> A batch continues after the last {@code (collection, chash)} the previous one took
 * (a row-value comparison on the primary key's order), so a row the sweep skipped (locked, or one an UPDATE did not
 * change) is not read again until the pass wraps to the start. A pass that wraps and finds nothing is the end of the run;
 * if rows are still there, they were locked: {@code done} is then false and {@code locked_skipped_rows} says how many.
 *
 * <p><b>Once per run, not per call.</b> The preconditions, the per-collection census and the residue counts are
 * computed by the first apply call (or the dry run) only; a call that carries the {@link Resume} the previous one
 * returned goes straight to the batches, and its time budget starts after any checks.
 *
 * <p><b>By tenant, explicitly.</b> Every statement carries {@code tenant_id = ?} beside the RLS stamp: the owner role
 * sees every tenant's rows (RF-16), and a sweep run as that role must not depend on the stamp.
 *
 * <p><b>The dry run writes nothing.</b> Its cost figure is the time of the batch SELECT alone, taken on up to three
 * collections spread over the sweep (no row lock, no UPDATE, not even one rolled back). The update half of a batch is
 * therefore not measured, and the projection built from the sample is a LOWER BOUND: a row whose vector is 3 KB or larger
 * is rewritten whole by an UPDATE of its metadata, and when the update is not HOT its HNSW index entry too (inferred from
 * the schema, not measured). Run one small apply (the verb prints the first batch's real time) before reading the
 * projection as a duration.
 *
 * <p><b>What it does not census.</b> The two equalities the CHECKs add ({@code chunk_text_hash} against the chash and
 * {@code embedding_model} against the column) are counted here and refuse the table-wide add, not the sweep. The Phase 3
 * capability flags (the manifest-keyed reads the sweep's removal of POSITION and DOCUMENT keys depends on, RDR-236) are
 * constants in this binary, true by construction wherever this route exists, so the sweep does not read them from
 * {@code /version}.
 */
public final class ChunkKeySweep {

    private static final Logger log = LoggerFactory.getLogger(ChunkKeySweep.class);

    /** Rows per batch: the RDR's 1000. */
    public static final int DEFAULT_BATCH = 1000;
    /** The most rows one batch may carry, whatever a request asks. */
    public static final int MAX_BATCH = 1000;

    /** The chash width every row must have for the UPDATE to pass the width CHECK. */
    static final int CHASH_WIDTH = 32;

    /** Statement and lock bounds for one batch transaction. */
    private static final int BATCH_STATEMENT_MS = 120_000;
    private static final int BATCH_LOCK_MS = 2_000;
    /** The census of one collection is a per-chunk lateral join; bound it, and read a trip as "not read". */
    static final Duration CENSUS_BOUND = Duration.ofSeconds(120);
    /** Collections sampled for the dry run's cost figure. */
    static final int SAMPLE_COLLECTIONS = 3;

    /** The manifest label columns' chunk-row keys: the three POSITION keys a legacy manifest row falls back to. */
    static final List<String> LABEL_KEYS = List.of("section_title", "section_type", "page_number");
    /** The chunk-row keys behind the four manifest span columns. */
    static final List<String> SPAN_KEYS = List.of("line_start", "line_end", "chunk_start_char", "chunk_end_char");
    /** A catalog note's content type. */
    static final String NOTE_CONTENT_TYPE = "knowledge";
    /** The catalog meta keys an indexed document stores at register (RDR-236 option A). */
    static final List<String> DERIVED_META_KEYS = List.of("source_format", "tags", "category");

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .configure(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS, true);

    /**
     * Residues an operator may accept by name AND count ({@code NAME=N}): each is a count of rows the sweep would leave
     * without a home for a fact (a label, a TTL, a position) or that the backfill verbs report. Non-zero and not accepted
     * up to its live count, apply refuses.
     */
    public static final List<String> ACCEPTABLE = List.of(
        "legacy_label_rows", "keyless_notes", "keyless_ttl_rows", "homeless_documents", "null_span_rows",
        "generated_nulls");

    /** The reason a collection is blocked when its census did not finish. */
    public static final String CENSUS_UNREAD = "census_unread";
    /** The reason a collection is blocked when its census reads legacy-unmanifested chunks. */
    public static final String LEGACY_UNMANIFESTED = "legacy_unmanifested";

    /** Reads a collection's {@code legacy-unmanifested} total, bounded; a thrown exception reads as "not read". */
    @FunctionalInterface
    public interface CensusReader {
        long legacyUnmanifested(String tenant, String collection, Duration bound);
    }

    private final TenantScope scope;
    private final CensusReader census;

    public ChunkKeySweep(TenantScope scope, PgVectorRepository vectors) {
        this(scope, (tenant, collection, bound) -> vectors
            .manifestLessCensusBounded(tenant, collection, 1, 0, bound).totals().getOrDefault("legacy-unmanifested", 0L));
    }

    public ChunkKeySweep(TenantScope scope, CensusReader census) {
        this.scope = scope;
        this.census = census;
    }

    // ---- reports ------------------------------------------------------------------------------------------------

    /** One collection's standing: rows with a retired key, of which rows with a wrong-width chash; its census verdict. */
    public record CollectionCount(String collection, long retiredRows, long wrongWidthRows, Long legacyUnmanifested,
                                  String blockedReason) {
        public boolean blocked() {
            return blockedReason != null;
        }

        /** Rows the sweep would rewrite: not blocked, correct width. */
        public long sweepableRows() {
            return blocked() ? 0L : retiredRows - wrongWidthRows;
        }
    }

    /** One timed batch SELECT of the dry run: the collection it ran over, the rows it returned and its time. */
    public record Sample(String collection, long rows, long selectMs) {}

    /** The preconditions of one tenant: the counts, and the breakdown of the two that have one. */
    public record Preconditions(Map<String, Long> counts, Map<String, Object> details) {}

    /**
     * The dry-run report of one tenant. {@code accepted} is the {@code NAME=N} the request carried; {@code blocking}
     * names each acceptable residue whose live count is non-zero and above what was accepted.
     */
    public record DryRun(String tenant, List<CollectionCount> collections, Map<String, Long> preconditions,
                         Map<String, Object> details, Map<String, Long> accepted, List<String> blocking,
                         List<Sample> samples, int batchSize) {
        public long sweepableRows() {
            return collections.stream().mapToLong(CollectionCount::sweepableRows).sum();
        }

        public long blockedRows() {
            return collections.stream().filter(CollectionCount::blocked).mapToLong(c -> c.retiredRows() - c.wrongWidthRows())
                .sum();
        }

        public long wrongWidthRows() {
            return collections.stream().mapToLong(CollectionCount::wrongWidthRows).sum();
        }

        public long retiredRows() {
            return collections.stream().mapToLong(CollectionCount::retiredRows).sum();
        }
    }

    /** A position in the sweep's order: the last {@code (collection, chash)} a batch took; {@code chash} is 64 hex. */
    public record Cursor(String collection, String chash) {}

    /** A collection the sweep leaves alone, why, and how many rows with a retired key it holds. */
    public record Blocked(String collection, String reason, long retiredRows) {}

    /**
     * What an apply call hands back so the next one skips the checks: where it stopped, the collections still to sweep,
     * the blocked ones (so the final report names them) and the rows still to sweep as the first call counted them less
     * what was swept since. The engine trusts it as the operator's own continuation of a run whose first call did the
     * checks; a call without it does them.
     */
    public record Resume(Cursor cursor, List<String> collections, List<Blocked> blocked, long remaining) {}

    /** The result of one apply call. {@code resume} is null once the run is done. */
    public record Applied(String tenant, long rowsSwept, int batches, boolean done, long remainingRows,
                          long lockedSkippedRows, long firstBatchMs, long totalMs, List<Blocked> blocked,
                          Resume resume) {}

    /** A refused apply: the preconditions that stood in the way and the counts. */
    public static final class PreconditionRefused extends RuntimeException {
        private final transient DryRun report;

        PreconditionRefused(DryRun report) {
            super("the sweep preconditions are not met: " + report.blocking());
            this.report = report;
        }

        public DryRun report() {
            return report;
        }
    }

    // ---- predicates ---------------------------------------------------------------------------------------------

    private static Field<String[]> keyArray(List<String> keys) {
        return DSL.val(keys.toArray(new String[0]), SQLDataType.CLOB.array());
    }

    /**
     * A row that holds a key outside the allow-list. A metadata value that is not an object is never "retired" (taking a
     * key off a scalar raises), so the test is a CASE: the object guard is evaluated first, as an AND would not promise.
     */
    static Condition retired(ChunkKeyPlan plan) {
        // jsonb - text[] is jsonb_delete(jsonb, VARIADIC text[]), which a typed call cannot spell, so the allowed keys
        // are taken off one at a time with the (jsonb, text) form MergeMetadata uses: what is left is the retired keys.
        Field<JSONB> leftover = CHUNKS.METADATA;
        for (String key : plan.allowed()) {
            leftover = DSL.function("jsonb_delete", JSONB.class, leftover, DSL.cast(DSL.val(key), SQLDataType.CLOB));
        }
        Condition leftoverNotEmpty = leftover.ne(DSL.val(JSONB.jsonb("{}")));
        return DSL.condition(DSL.when(isObject(CHUNKS.METADATA), DSL.field(leftoverNotEmpty)).otherwise(DSL.inline(false)));
    }

    private static Condition isObject(Field<JSONB> metadata) {
        return DSL.function("jsonb_typeof", SQLDataType.VARCHAR, metadata).eq("object");
    }

    /** A row outside every bypass prefix. */
    static Condition notBypass(Field<String> collection, ChunkKeyPlan plan) {
        Condition c = DSL.trueCondition();
        for (String prefix : plan.bypassPrefixes()) {
            c = c.and(DSL.not(collection.startsWith(prefix)));
        }
        return c;
    }

    static Condition width(boolean correct) {
        Field<Integer> w = DSL.function("octet_length", SQLDataType.INTEGER, CHUNKS.CHASH);
        return correct ? w.eq(CHASH_WIDTH) : w.ne(CHASH_WIDTH);
    }

    private static Condition hasAny(Field<JSONB> metadata, List<String> keys) {
        return DSL.condition(DSL.function("jsonb_exists_any", SQLDataType.BOOLEAN, metadata, keyArray(keys)));
    }

    static Condition hasKey(Field<JSONB> metadata, String key) {
        return DSL.condition(DSL.function("jsonb_exists", SQLDataType.BOOLEAN, metadata, DSL.val(key)));
    }

    /** A row's {@code chunk_text_hash} that is not its chash in hex (a row without the key passes, as the CHECK does). */
    static Condition hashMismatch() {
        Field<String> hash = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "chunk_text_hash");
        return hash.ne(DSL.function("encode", SQLDataType.VARCHAR, CHUNKS.CHASH, DSL.inline("hex")));
    }

    /** A row's {@code embedding_model} that is not the column. */
    static Condition modelMismatch() {
        Field<String> model = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "embedding_model");
        return model.ne(CHUNKS.EMBEDDING_MODEL);
    }

    /** {@code by} present without {@code at}: the quarantine implication broken. */
    static Condition quarantineBreach(ChunkKeyPlan plan) {
        return hasKey(CHUNKS.METADATA, plan.quarantineImplies().get(0))
            .and(DSL.not(hasKey(CHUNKS.METADATA, plan.quarantineImplies().get(1))));
    }

    // ---- counts -------------------------------------------------------------------------------------------------

    /** Rows with a retired key per non-bypass collection, with the wrong-width part. */
    private List<CollectionCount> retiredByCollection(DSLContext ctx, String tenant, ChunkKeyPlan plan) {
        var wrong = DSL.count().filterWhere(width(false));
        Result<?> rows = ctx.select(CHUNKS.COLLECTION, DSL.count(), wrong)
            .from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant))
            .and(notBypass(CHUNKS.COLLECTION, plan))
            .and(retired(plan))
            .groupBy(CHUNKS.COLLECTION)
            .orderBy(CHUNKS.COLLECTION)
            .fetch();
        List<CollectionCount> out = new ArrayList<>();
        for (var r : rows) {
            out.add(new CollectionCount(r.get(0, String.class), r.get(1, Long.class), r.get(2, Long.class), null, null));
        }
        return out;
    }

    /** The manifest-less census's {@code legacy-unmanifested} total for one collection, or a blocked verdict. */
    private CollectionCount withCensus(String tenant, CollectionCount c) {
        try {
            long legacy = census.legacyUnmanifested(tenant, c.collection(), CENSUS_BOUND);
            return new CollectionCount(c.collection(), c.retiredRows(), c.wrongWidthRows(), legacy,
                legacy > 0 ? LEGACY_UNMANIFESTED : null);
        } catch (RuntimeException e) {
            // Unknown is not clean: a census that did not finish blocks the collection (SQLSTATE 57014 included).
            log.warn("event=retire_keys_census_unread tenant={} collection={} cause=\"{}\"", tenant, c.collection(),
                e.getMessage());
            return new CollectionCount(c.collection(), c.retiredRows(), c.wrongWidthRows(), null, CENSUS_UNREAD);
        }
    }

    /** The preconditions of the sweep and of the constraints, counted for one tenant. */
    public Map<String, Long> preconditions(String tenant, ChunkKeyPlan plan) {
        return preconditionReport(tenant, plan).counts();
    }

    /** As {@link #preconditions}, with the breakdown of {@code generated_nulls} and {@code null_span_rows}. */
    public Preconditions preconditionReport(String tenant, ChunkKeyPlan plan) {
        return scope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
            return countPreconditions(ctx, tenant, plan);
        });
    }

    private Preconditions countPreconditions(DSLContext ctx, String tenant, ChunkKeyPlan plan) {
        Map<String, Long> out = new LinkedHashMap<>();
        Map<String, Object> details = new LinkedHashMap<>();
        CatalogDocuments d = CATALOG_DOCUMENTS.as("d");
        CatalogDocumentChunks m = CATALOG_DOCUMENT_CHUNKS.as("m");
        Chunks c = CHUNKS.as("c");
        Condition liveDoc = d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull());

        // A legacy manifest row (labels_written false) whose label is on the chunk row only: the sweep would lose it.
        out.put("legacy_label_rows", (long) ctx.fetchCount(
            DSL.selectOne().from(m)
                .join(d).on(d.TENANT_ID.eq(m.TENANT_ID).and(d.TUMBLER.eq(m.DOC_ID)))
                .join(c).on(c.TENANT_ID.eq(m.TENANT_ID).and(c.COLLECTION.eq(m.COLLECTION)).and(c.CHASH.eq(m.CHASH)))
                .where(m.TENANT_ID.eq(tenant)).and(liveDoc).and(m.LABELS_WRITTEN.isFalse())
                .and(hasAny(c.METADATA, LABEL_KEYS))));

        // A live note with no put_at in its catalog meta: its TTL and tags are read from the chunk row until it has one.
        out.put("keyless_notes", (long) ctx.fetchCount(
            DSL.selectOne().from(d)
                .where(liveDoc).and(d.CONTENT_TYPE.eq(NOTE_CONTENT_TYPE))
                .and(DSL.coalesce(hasKeyAsField(d.METADATA, "put_at"), DSL.inline(false)).isFalse())));

        // A chunk row that records a TTL no live owner's catalog meta records: the sweep would make that note permanent.
        Field<String> rowTtl = DSL.jsonbGetAttributeAsText(c.METADATA, "ttl_days");
        out.put("keyless_ttl_rows", (long) ctx.fetchCount(
            DSL.selectOne().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(notBypass(c.COLLECTION, plan))
                .and(rowTtl.isNotNull()).and(rowTtl.ne("0")).and(rowTtl.ne(""))
                .andNotExists(DSL.selectOne().from(m).join(d)
                    .on(d.TENANT_ID.eq(m.TENANT_ID).and(d.TUMBLER.eq(m.DOC_ID)))
                    .where(m.TENANT_ID.eq(c.TENANT_ID)).and(m.COLLECTION.eq(c.COLLECTION)).and(m.CHASH.eq(c.CHASH))
                    .and(d.DELETED_AT.isNull())
                    .and(DSL.coalesce(hasKeyAsField(d.METADATA, "ttl_days"), DSL.inline(false)).isTrue()))));

        // A live document with no physical collection: its chunks have no home the manifest can name (RDR-191).
        out.put("homeless_documents", (long) ctx.fetchCount(
            DSL.selectOne().from(d).where(liveDoc).and(d.PHYSICAL_COLLECTION.eq(""))));

        // A manifest span column that is NULL while the chunk row holds the matching key: the sweep would lose that
        // position. Counted per column (the sweep deletes all four keys), and a row once whatever columns it lacks.
        Condition lineStart = m.LINE_START.isNull().and(hasKey(c.METADATA, "line_start"));
        Condition lineEnd = m.LINE_END.isNull().and(hasKey(c.METADATA, "line_end"));
        Condition charStart = m.CHAR_START.isNull().and(hasKey(c.METADATA, "chunk_start_char"));
        Condition charEnd = m.CHAR_END.isNull().and(hasKey(c.METADATA, "chunk_end_char"));
        var spanRow = ctx.select(
                DSL.count(),
                DSL.count().filterWhere(lineStart), DSL.count().filterWhere(lineEnd),
                DSL.count().filterWhere(charStart), DSL.count().filterWhere(charEnd))
            .from(m)
            .join(d).on(d.TENANT_ID.eq(m.TENANT_ID).and(d.TUMBLER.eq(m.DOC_ID)))
            .join(c).on(c.TENANT_ID.eq(m.TENANT_ID).and(c.COLLECTION.eq(m.COLLECTION)).and(c.CHASH.eq(m.CHASH)))
            .where(m.TENANT_ID.eq(tenant)).and(liveDoc)
            .and(lineStart.or(lineEnd).or(charStart).or(charEnd))
            .fetchOne();
        out.put("null_span_rows", spanRow.get(0, Long.class));
        Map<String, Long> byField = new LinkedHashMap<>();
        for (int i = 0; i < SPAN_KEYS.size(); i++) {
            byField.put(SPAN_KEYS.get(i), spanRow.get(i + 1, Long.class));
        }
        details.put("null_span_rows", Map.of("by_field", byField));

        // An indexed document that lacks a stored source_format, tags or category (backfill-meta's residue), by content
        // type and by which key is missing: the count alone does not say whether backfill-meta has run for the type.
        Condition missingAny = DSL.coalesce(DSL.function("jsonb_exists_all", SQLDataType.BOOLEAN, d.METADATA,
            keyArray(DERIVED_META_KEYS)), DSL.inline(false)).isFalse();
        Result<?> gen = ctx.select(d.CONTENT_TYPE, DSL.count(),
                DSL.count().filterWhere(missingKey(d.METADATA, "source_format")),
                DSL.count().filterWhere(missingKey(d.METADATA, "tags")),
                DSL.count().filterWhere(missingKey(d.METADATA, "category")))
            .from(d)
            .where(liveDoc).and(d.CONTENT_TYPE.ne(NOTE_CONTENT_TYPE)).and(missingAny)
            .groupBy(d.CONTENT_TYPE).orderBy(d.CONTENT_TYPE)
            .fetch();
        long genTotal = 0;
        Map<String, Long> byType = new LinkedHashMap<>();
        Map<String, Long> byKey = new LinkedHashMap<>();
        for (String k : DERIVED_META_KEYS) {
            byKey.put(k, 0L);
        }
        for (var r : gen) {
            String type = r.get(0, String.class);
            long n = r.get(1, Long.class);
            genTotal += n;
            byType.put(type == null || type.isEmpty() ? "(none)" : type, n);
            for (int i = 0; i < DERIVED_META_KEYS.size(); i++) {
                byKey.merge(DERIVED_META_KEYS.get(i), r.get(i + 2, Long.class), Long::sum);
            }
        }
        out.put("generated_nulls", genTotal);
        Map<String, Object> genDetail = new LinkedHashMap<>();
        genDetail.put("by_content_type", byType);
        genDetail.put("missing_key", byKey);
        details.put("generated_nulls", genDetail);

        // The constraints' own equalities and the width, counted over every collection of the tenant.
        out.put("chunk_text_hash_mismatch", (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(hashMismatch())));
        out.put("embedding_model_mismatch", (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(modelMismatch())));
        out.put("wrong_width_rows", (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(width(false))));

        // Rows the sweep cannot rewrite by key: a metadata value that is not a JSON object.
        out.put("non_object_metadata_rows", (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(DSL.not(isObject(CHUNKS.METADATA)))));

        // Rows an earlier restore left half-stamped: the engine's row keys on a live row, and the implication broken.
        out.put("row_keys_on_live_rows", plan.rowKeys().isEmpty() ? 0L : (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
                .and(notBypass(CHUNKS.COLLECTION, plan)).and(hasAny(CHUNKS.METADATA, plan.rowKeys()))));
        out.put("quarantine_implication_violations", plan.quarantineImplies().isEmpty() ? 0L : (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(quarantineBreach(plan))));
        return new Preconditions(out, details);
    }

    private static Field<Boolean> hasKeyAsField(Field<JSONB> metadata, String key) {
        return DSL.function("jsonb_exists", SQLDataType.BOOLEAN, metadata, DSL.val(key));
    }

    private static Condition missingKey(Field<JSONB> metadata, String key) {
        return DSL.coalesce(hasKeyAsField(metadata, key), DSL.inline(false)).isFalse();
    }

    /** Rows still to sweep (not blocked, correct width) for the tenant, over {@code collections}. */
    private long remaining(DSLContext ctx, String tenant, ChunkKeyPlan plan, Set<String> collections) {
        if (collections.isEmpty()) {
            return 0L;
        }
        return ctx.fetchCount(DSL.selectOne().from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.in(collections))
            .and(retired(plan)).and(width(true)));
    }

    // ---- the dry run --------------------------------------------------------------------------------------------

    /**
     * The residues whose live count is above what the operator accepted. A residue of zero never blocks; a non-zero one
     * blocks unless {@code accepted} names it with a number at least as large as the live count.
     */
    static List<String> blocking(Map<String, Long> pre, Map<String, Long> accepted) {
        List<String> blocking = new ArrayList<>();
        for (String name : ACCEPTABLE) {
            long live = pre.getOrDefault(name, 0L);
            if (live > 0 && live > accepted.getOrDefault(name, 0L)) {
                blocking.add(name);
            }
        }
        return blocking;
    }

    /**
     * The dry run: rows per collection, the censused verdict of each, the preconditions, and the timed batch SELECTs. It
     * writes nothing and takes no row lock.
     *
     * @param accepted   {@code NAME -> N}: the residues the operator accepts, each up to N rows
     * @param measureBatch time the batch SELECT on up to {@value #SAMPLE_COLLECTIONS} collections
     */
    public DryRun dryRun(String tenant, ChunkKeyPlan plan, Map<String, Long> accepted, int batchSize,
                         boolean measureBatch) {
        int size = clampBatch(batchSize);
        List<CollectionCount> collections = collectionsOf(tenant, plan);
        Preconditions pre = preconditionReport(tenant, plan);
        List<String> blocking = blocking(pre.counts(), accepted);
        List<Sample> samples = List.of();
        List<String> sweepable = new ArrayList<>(sweepableCollections(collections));
        if (measureBatch && !sweepable.isEmpty()) {
            samples = sample(tenant, plan, sweepable, size);
        }
        return new DryRun(tenant, collections, pre.counts(), pre.details(), Map.copyOf(accepted), blocking, samples, size);
    }

    private static Set<String> sweepableCollections(List<CollectionCount> collections) {
        Set<String> out = new LinkedHashSet<>();
        for (CollectionCount c : collections) {
            if (c.sweepableRows() > 0) {
                out.add(c.collection());
            }
        }
        return out;
    }

    /** Up to {@link #SAMPLE_COLLECTIONS} collections spread over the list: the first, the middle and the last. */
    private List<Sample> sample(String tenant, ChunkKeyPlan plan, List<String> sweepable, int size) {
        Set<Integer> picks = new LinkedHashSet<>(List.of(0, sweepable.size() / 2, sweepable.size() - 1));
        List<Sample> out = new ArrayList<>();
        for (int i : picks) {
            if (out.size() >= SAMPLE_COLLECTIONS) {
                break;
            }
            String collection = sweepable.get(i);
            out.add(scope.withTenant(tenant, ctx -> {
                PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
                long s0 = System.nanoTime();
                // The batch's own SELECT, without FOR UPDATE: a plain read, no row lock, no tuple stamped.
                Result<?> rows = ctx.select(CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL, CHUNKS.METADATA)
                    .from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(tenant))
                    .and(CHUNKS.COLLECTION.eq(collection))
                    .and(width(true))
                    .and(retired(plan))
                    .orderBy(CHUNKS.COLLECTION, CHUNKS.CHASH)
                    .limit(size)
                    .fetch();
                return new Sample(collection, rows.size(), (System.nanoTime() - s0) / 1_000_000);
            }));
        }
        return out;
    }

    // ---- apply --------------------------------------------------------------------------------------------------

    /**
     * Apply: unless {@code resume} carries a run already checked, refuse unless the preconditions hold (or each residue
     * is accepted up to its count), then sweep batch by batch until {@code maxBatches} or {@code budget} is spent or no
     * row is left. The budget starts after the checks. Each batch is its own transaction.
     *
     * @param only   restricts the sweep to one collection when non-null
     * @param resume the previous call's continuation, or null for the first call of a run
     * @throws PreconditionRefused when a precondition is non-zero and not accepted up to its count
     */
    public Applied apply(String tenant, ChunkKeyPlan plan, Map<String, Long> accepted, String only, int batchSize,
                         int maxBatches, Duration budget, Resume resume) {
        int size = clampBatch(batchSize);
        long t00 = System.nanoTime();
        Set<String> sweepable;
        List<Blocked> blocked;
        long remaining;
        Cursor cursor;
        if (resume == null) {
            DryRun report = dryRun(tenant, plan, accepted, size, false);
            if (!report.blocking().isEmpty()) {
                throw new PreconditionRefused(report);
            }
            sweepable = sweepableCollections(report.collections());
            if (only != null) {
                sweepable.retainAll(Set.of(only));
            }
            blocked = report.collections().stream().filter(CollectionCount::blocked)
                .map(c -> new Blocked(c.collection(), c.blockedReason(), c.retiredRows() - c.wrongWidthRows())).toList();
            remaining = report.collections().stream().filter(c -> sweepable.contains(c.collection()))
                .mapToLong(CollectionCount::sweepableRows).sum();
            cursor = null;
        } else {
            sweepable = new LinkedHashSet<>(resume.collections());
            if (only != null) {
                sweepable.retainAll(Set.of(only));
            }
            blocked = resume.blocked();
            remaining = resume.remaining();
            cursor = resume.cursor();
        }

        long t0 = System.nanoTime();   // the budget is for the batches, not for the checks before them
        long swept = 0;
        int batches = 0;
        long firstMs = 0;
        boolean finished = sweepable.isEmpty();
        while (!finished && batches < maxBatches && Duration.ofNanos(System.nanoTime() - t0).compareTo(budget) < 0) {
            Cursor from = cursor;
            Batch b = scope.withTenant(tenant, ctx -> {
                PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
                return oneBatch(ctx, tenant, plan, sweepable, size, from);
            });
            if (b.rows() == 0 && from != null) {
                // The pass reached the end: wrap to the start once, to take a row an earlier pass found locked.
                b = scope.withTenant(tenant, ctx -> {
                    PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
                    return oneBatch(ctx, tenant, plan, sweepable, size, null);
                });
                cursor = null;
            }
            batches++;
            if (batches == 1) {
                firstMs = b.selectMs() + b.updateMs();
                log.info("event=retire_keys_first_batch tenant={} rows={} select_ms={} update_ms={}", tenant,
                    b.rows(), b.selectMs(), b.updateMs());
            }
            swept += b.updated();
            if (b.rows() == 0) {
                finished = true;
            } else {
                cursor = b.last();
            }
        }
        long lockedLeft = 0;
        boolean done;
        long remainingOut;
        if (finished) {
            // A pass from the start found nothing it could take. Anything the predicate still matches was locked.
            lockedLeft = scope.withTenant(tenant, ctx -> {
                PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
                return remaining(ctx, tenant, plan, sweepable);
            });
            done = lockedLeft == 0;
            remainingOut = lockedLeft;
        } else {
            done = false;
            remainingOut = Math.max(0L, remaining - swept);
        }
        Resume next = done ? null : new Resume(cursor, List.copyOf(sweepable), blocked, remainingOut);
        log.info("event=retire_keys_swept tenant={} rows={} batches={} remaining={} locked_skipped={} blocked_collections={}",
            tenant, swept, batches, remainingOut, lockedLeft, blocked.size());
        return new Applied(tenant, swept, batches, done, remainingOut, lockedLeft, firstMs,
            Duration.ofNanos(System.nanoTime() - t00).toMillis(), blocked, next);
    }

    private record Batch(long updated, long rows, long selectMs, long updateMs, Cursor last) {}

    private static byte[] cursorChash(Cursor c) {
        return Chash.fromHex(c.chash()).toBytes();
    }

    /**
     * One batch: take up to {@code size} rows with a retired key after {@code after} (the keyset), rewrite their
     * metadata, return the cost and the last key taken.
     */
    private Batch oneBatch(DSLContext ctx, String tenant, ChunkKeyPlan plan, Set<String> collections, int size,
                           Cursor after) {
        long s0 = System.nanoTime();
        Condition keyset = after == null ? DSL.trueCondition()
            : DSL.row(CHUNKS.COLLECTION, CHUNKS.CHASH).gt(DSL.row(after.collection(), cursorChash(after)));
        Result<Record4<String, byte[], String, JSONB>> rows = ctx
            .select(CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL, CHUNKS.METADATA)
            .from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.in(collections))
            .and(width(true))
            .and(retired(plan))
            .and(keyset)
            .orderBy(CHUNKS.COLLECTION, CHUNKS.CHASH)
            .limit(size)
            .forUpdate().skipLocked()
            .fetch();
        long selectMs = (System.nanoTime() - s0) / 1_000_000;
        if (rows.isEmpty()) {
            return new Batch(0, 0, selectMs, 0, after);
        }
        Set<String> allowed = Set.copyOf(plan.allowed());
        List<Query> updates = new ArrayList<>(rows.size());
        for (var r : rows) {
            JSONB kept = keepOnly(r.value4(), allowed);
            // The statement assigns metadata and nothing else: last_written_at is the reaper's grace anchor.
            updates.add(ctx.update(CHUNKS)
                .set(CHUNKS.METADATA, kept)
                .where(CHUNKS.TENANT_ID.eq(tenant))
                .and(CHUNKS.COLLECTION.eq(r.value1()))
                .and(CHUNKS.CHASH.eq(r.value2()))
                .and(CHUNKS.EMBEDDING_MODEL.eq(r.value3())));
        }
        long u0 = System.nanoTime();
        int[] counts = ctx.batch(updates).execute();
        long updateMs = (System.nanoTime() - u0) / 1_000_000;
        long updated = 0;
        for (int n : counts) {
            updated += Math.max(0, n);
        }
        var lastRow = rows.get(rows.size() - 1);
        Cursor last = new Cursor(lastRow.value1(), HexFormat.of().formatHex(lastRow.value2()));
        return new Batch(updated, rows.size(), selectMs, updateMs, last);
    }

    /** {@code metadata} with every key outside {@code allowed} removed. */
    static JSONB keepOnly(JSONB metadata, Set<String> allowed) {
        try {
            JsonNode node = MAPPER.readTree(metadata.data());
            if (node instanceof ObjectNode obj) {
                obj.retain(allowed);
                return JSONB.jsonb(MAPPER.writeValueAsString(obj));
            }
            return metadata;
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException("chunk metadata is not JSON: " + e.getMessage(), e);
        }
    }

    private static int clampBatch(int requested) {
        return requested <= 0 ? DEFAULT_BATCH : Math.min(requested, MAX_BATCH);
    }

    // ---- the table-wide view ------------------------------------------------------------------------------------

    /** Per-collection standing of one tenant, each collection with rows to sweep censused. */
    public List<CollectionCount> collectionsOf(String tenant, ChunkKeyPlan plan) {
        List<CollectionCount> raw = scope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
            return retiredByCollection(ctx, tenant, plan);
        });
        List<CollectionCount> collections = new ArrayList<>();
        for (CollectionCount c : raw) {
            collections.add(c.retiredRows() - c.wrongWidthRows() > 0 ? withCensus(tenant, c) : c);
        }
        return collections;
    }
}
