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
 * would then refuse the row. The rows it looks at are those where what is left of the metadata after the allowed keys are taken off is not empty.
 *
 * <p><b>What it never touches.</b> {@code quarantine-*} and {@code taxonomy__*} rows (the plan's bypass prefixes; the
 * quarantine restore strips its own rows to CHUNK keys, nexus-l003p, so they never carry a retired key into a live
 * collection); a row whose chash is not 32 bytes (the existing NOT VALID {@code chunks_chash_octet_check} would refuse
 * the UPDATE, RF-23: counted, not swept); and a collection whose manifest-less census reads {@code legacy-unmanifested}
 * above zero (the census's forward join reads {@code catalog_doc_id}/{@code doc_id} off the row, RF-19, so the
 * sweep would blind it: the collection is skipped and reported as blocked).
 *
 * <p><b>Timestamp neutral.</b> The UPDATE assigns {@code metadata} and nothing else, so {@code last_written_at} (the
 * reaper's grace anchor, nexus-wbfpw.43) keeps its value. The statement is typed jOOQ, one row per chash, in the
 * {@code (collection, chash)} order the client writers lock in (nexus-hxrcm), over rows taken {@code FOR UPDATE SKIP
 * LOCKED} so a concurrent writer is never waited for: the row it holds is swept by the next call.
 *
 * <p><b>By tenant, explicitly.</b> Every statement carries {@code tenant_id = ?} beside the RLS stamp: the owner role
 * sees every tenant's rows (RF-16), and a sweep run as that role must not depend on the stamp.
 *
 * <p><b>Cost.</b> {@link #dryRun} reports the rows to sweep per collection and the measured cost of one real batch: the
 * batch is taken and rewritten inside a transaction that is rolled back, so nothing it did stays (its aborted tuples
 * wait for autovacuum, which is the only trace). A row whose vector is 3 KB or larger is rewritten whole by an UPDATE of
 * its metadata, and when the update is not HOT its HNSW index entry too (inferred from the schema, not measured),
 * which is why the cost is measured rather than assumed.
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
     * Residues an operator may accept by name: each is a count of rows the sweep would leave without a home for a fact
     * (a label, a TTL, a position) or that the backfill verbs report. Non-zero and not accepted, apply refuses.
     */
    public static final List<String> ACCEPTABLE = List.of(
        "legacy_label_rows", "keyless_notes", "keyless_ttl_rows", "homeless_documents", "null_span_rows",
        "generated_nulls");

    private final TenantScope scope;
    private final PgVectorRepository vectors;

    public ChunkKeySweep(TenantScope scope, PgVectorRepository vectors) {
        this.scope = scope;
        this.vectors = vectors;
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

    /** What one real batch cost, in a transaction that was rolled back. */
    public record BatchCost(long rows, long selectMs, long updateMs) {}

    /** The dry-run report of one tenant. */
    public record DryRun(String tenant, List<CollectionCount> collections, Map<String, Long> preconditions,
                         List<String> blocking, BatchCost batchCost, int batchSize) {
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

    /** The result of one apply call. */
    public record Applied(String tenant, long rowsSwept, int batches, boolean done, long remainingRows,
                          long firstBatchMs, long totalMs, List<CollectionCount> blocked) {}

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

    /** A row that holds a key outside the allow-list. */
    static Condition retired(ChunkKeyPlan plan) {
        // jsonb - text[] is jsonb_delete(jsonb, VARIADIC text[]), which a typed call cannot spell, so the allowed keys
        // are taken off one at a time with the (jsonb, text) form MergeMetadata uses: what is left is the retired keys.
        Field<JSONB> leftover = CHUNKS.METADATA;
        for (String key : plan.allowed()) {
            leftover = DSL.function("jsonb_delete", JSONB.class, leftover, DSL.cast(DSL.val(key), SQLDataType.CLOB));
        }
        return leftover.ne(DSL.val(JSONB.jsonb("{}")));
    }

    /** A row outside every bypass prefix. */
    static Condition notBypass(Field<String> collection, ChunkKeyPlan plan) {
        Condition c = DSL.trueCondition();
        for (String prefix : plan.bypassPrefixes()) {
            c = c.and(DSL.not(collection.startsWith(prefix)));
        }
        return c;
    }

    private static Condition width(boolean correct) {
        Field<Integer> w = DSL.function("octet_length", SQLDataType.INTEGER, CHUNKS.CHASH);
        return correct ? w.eq(CHASH_WIDTH) : w.ne(CHASH_WIDTH);
    }

    private static Condition hasAny(Field<JSONB> metadata, List<String> keys) {
        return DSL.condition(DSL.function("jsonb_exists_any", SQLDataType.BOOLEAN, metadata, keyArray(keys)));
    }

    private static Condition hasKey(Field<JSONB> metadata, String key) {
        return DSL.condition(DSL.function("jsonb_exists", SQLDataType.BOOLEAN, metadata, DSL.val(key)));
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
            var census = vectors.manifestLessCensusBounded(tenant, c.collection(), 1, 0, CENSUS_BOUND);
            long legacy = census.totals().getOrDefault("legacy-unmanifested", 0L);
            return new CollectionCount(c.collection(), c.retiredRows(), c.wrongWidthRows(), legacy,
                legacy > 0 ? "legacy_unmanifested" : null);
        } catch (RuntimeException e) {
            // Unknown is not clean: a census that did not finish blocks the collection (SQLSTATE 57014 included).
            log.warn("event=retire_keys_census_unread tenant={} collection={} cause=\"{}\"", tenant, c.collection(),
                e.getMessage());
            return new CollectionCount(c.collection(), c.retiredRows(), c.wrongWidthRows(), null, "census_unread");
        }
    }

    /** The preconditions of the sweep and of the constraints, counted for one tenant. */
    public Map<String, Long> preconditions(String tenant, ChunkKeyPlan plan) {
        return scope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
            return countPreconditions(ctx, tenant, plan);
        });
    }

    private Map<String, Long> countPreconditions(DSLContext ctx, String tenant, ChunkKeyPlan plan) {
        Map<String, Long> out = new LinkedHashMap<>();
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

        // A manifest row with no span whose chunk row carries one: the sweep would lose the position.
        out.put("null_span_rows", (long) ctx.fetchCount(
            DSL.selectOne().from(m)
                .join(d).on(d.TENANT_ID.eq(m.TENANT_ID).and(d.TUMBLER.eq(m.DOC_ID)))
                .join(c).on(c.TENANT_ID.eq(m.TENANT_ID).and(c.COLLECTION.eq(m.COLLECTION)).and(c.CHASH.eq(m.CHASH)))
                .where(m.TENANT_ID.eq(tenant)).and(liveDoc)
                .and(m.LINE_START.isNull()).and(m.LINE_END.isNull()).and(m.CHAR_START.isNull()).and(m.CHAR_END.isNull())
                .and(hasAny(c.METADATA, SPAN_KEYS))));

        // An indexed document that lacks a stored source_format, tags or category (backfill-meta's residue).
        out.put("generated_nulls", (long) ctx.fetchCount(
            DSL.selectOne().from(d)
                .where(liveDoc).and(d.CONTENT_TYPE.ne(NOTE_CONTENT_TYPE))
                .and(DSL.coalesce(DSL.function("jsonb_exists_all", SQLDataType.BOOLEAN, d.METADATA,
                    keyArray(DERIVED_META_KEYS)), DSL.inline(false)).isFalse())));

        // The constraints' own equalities and the width, counted over every collection of the tenant.
        Field<String> hash = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "chunk_text_hash");
        Field<String> model = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "embedding_model");
        out.put("chunk_text_hash_mismatch", (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
                .and(hash.ne(DSL.function("encode", SQLDataType.VARCHAR, CHUNKS.CHASH, DSL.inline("hex"))))));
        out.put("embedding_model_mismatch", (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(model.ne(CHUNKS.EMBEDDING_MODEL))));
        out.put("wrong_width_rows", (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(width(false))));

        // Rows an earlier restore left half-stamped: the engine's row keys on a live row, and the implication broken.
        out.put("row_keys_on_live_rows", plan.rowKeys().isEmpty() ? 0L : (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
                .and(notBypass(CHUNKS.COLLECTION, plan)).and(hasAny(CHUNKS.METADATA, plan.rowKeys()))));
        out.put("quarantine_implication_violations", plan.quarantineImplies().isEmpty() ? 0L : (long) ctx.fetchCount(
            DSL.selectOne().from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
                .and(hasKey(CHUNKS.METADATA, plan.quarantineImplies().get(0)))
                .and(DSL.not(hasKey(CHUNKS.METADATA, plan.quarantineImplies().get(1))))));
        return out;
    }

    private static Field<Boolean> hasKeyAsField(Field<JSONB> metadata, String key) {
        return DSL.function("jsonb_exists", SQLDataType.BOOLEAN, metadata, DSL.val(key));
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
     * The dry run: rows per collection, the censused verdict of each, the preconditions, and one measured batch.
     *
     * @param measureBatch take and rewrite one real batch inside a transaction that is rolled back
     */
    public DryRun dryRun(String tenant, ChunkKeyPlan plan, Set<String> accepted, int batchSize, boolean measureBatch) {
        int size = clampBatch(batchSize);
        List<CollectionCount> collections = collectionsOf(tenant, plan);
        Map<String, Long> pre = preconditions(tenant, plan);
        List<String> blocking = new ArrayList<>();
        for (String name : ACCEPTABLE) {
            if (pre.getOrDefault(name, 0L) > 0 && !accepted.contains(name)) {
                blocking.add(name);
            }
        }
        BatchCost cost = null;
        Set<String> sweepable = sweepableCollections(collections);
        if (measureBatch && !sweepable.isEmpty()) {
            cost = measureOneBatch(tenant, plan, sweepable, size);
        }
        return new DryRun(tenant, collections, pre, blocking, cost, size);
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

    private static final class Rollback extends RuntimeException {
        private final transient BatchCost cost;

        Rollback(BatchCost cost) {
            super(null, null, false, false);
            this.cost = cost;
        }
    }

    private BatchCost measureOneBatch(String tenant, ChunkKeyPlan plan, Set<String> collections, int size) {
        try {
            scope.withTenant(tenant, ctx -> {
                PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
                throw new Rollback(oneBatch(ctx, tenant, plan, collections, size).cost());
            });
            return null;
        } catch (Rollback r) {
            return r.cost;
        }
    }

    // ---- apply --------------------------------------------------------------------------------------------------

    /**
     * Apply: refuse unless the preconditions hold (or each residue is accepted), then sweep batch by batch until
     * {@code maxBatches} or {@code budget} is spent or no row is left. Each batch is its own transaction.
     *
     * @param only restricts the sweep to one collection when non-null
     * @throws PreconditionRefused when a precondition is non-zero and not accepted
     */
    public Applied apply(String tenant, ChunkKeyPlan plan, Set<String> accepted, String only, int batchSize,
                         int maxBatches, Duration budget) {
        int size = clampBatch(batchSize);
        long t0 = System.nanoTime();
        DryRun report = dryRun(tenant, plan, accepted, size, false);
        if (!report.blocking().isEmpty()) {
            throw new PreconditionRefused(report);
        }
        Set<String> sweepable = sweepableCollections(report.collections());
        if (only != null) {
            sweepable.retainAll(Set.of(only));
        }
        List<CollectionCount> blocked = report.collections().stream().filter(CollectionCount::blocked).toList();

        long swept = 0;
        int batches = 0;
        long firstMs = 0;
        boolean done = sweepable.isEmpty();
        while (!done && batches < maxBatches && Duration.ofNanos(System.nanoTime() - t0).compareTo(budget) < 0) {
            Batch b = scope.withTenant(tenant, ctx -> {
                PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
                return oneBatch(ctx, tenant, plan, sweepable, size);
            });
            batches++;
            if (batches == 1) {
                firstMs = b.cost().selectMs() + b.cost().updateMs();
                log.info("event=retire_keys_first_batch tenant={} rows={} select_ms={} update_ms={}", tenant,
                    b.cost().rows(), b.cost().selectMs(), b.cost().updateMs());
            }
            swept += b.updated();
            done = b.cost().rows() == 0;
        }
        long left = scope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
            return remaining(ctx, tenant, plan, sweepable);
        });
        log.info("event=retire_keys_swept tenant={} rows={} batches={} remaining={} blocked_collections={}", tenant,
            swept, batches, left, blocked.size());
        return new Applied(tenant, swept, batches, done || left == 0, left, firstMs,
            Duration.ofNanos(System.nanoTime() - t0).toMillis(), blocked);
    }

    private record Batch(long updated, BatchCost cost) {}

    /** One batch: take up to {@code size} rows with a retired key, rewrite their metadata, return the cost. */
    private Batch oneBatch(DSLContext ctx, String tenant, ChunkKeyPlan plan, Set<String> collections, int size) {
        long s0 = System.nanoTime();
        Result<Record4<String, byte[], String, JSONB>> rows = ctx
            .select(CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL, CHUNKS.METADATA)
            .from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.in(collections))
            .and(width(true))
            .and(retired(plan))
            .orderBy(CHUNKS.COLLECTION, CHUNKS.CHASH)
            .limit(size)
            .forUpdate().skipLocked()
            .fetch();
        long selectMs = (System.nanoTime() - s0) / 1_000_000;
        if (rows.isEmpty()) {
            return new Batch(0, new BatchCost(0, selectMs, 0));
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
        return new Batch(updated, new BatchCost(rows.size(), selectMs, updateMs));
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

    /**
     * Rows still holding a retired key in a tenant, counting every row the sweep leaves behind: those in a blocked
     * collection and those whose chash is the wrong width. The VALIDATE of the allow-list CHECK needs this to be zero;
     * the add route needs only {@link #sweepCount}.
     */
    public long retiredRowsTotal(String tenant, ChunkKeyPlan plan) {
        return scope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, BATCH_STATEMENT_MS, BATCH_LOCK_MS);
            return (long) ctx.fetchCount(DSL.selectOne().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(notBypass(CHUNKS.COLLECTION, plan)).and(retired(plan)));
        });
    }

    /**
     * The tenant's sweep count: rows a sweep would rewrite now (retired key, correct width, collection not blocked by
     * its census). Zero means the tenant is swept as far as the sweep can take it.
     */
    public long sweepCount(String tenant, ChunkKeyPlan plan) {
        return collectionsOf(tenant, plan).stream().mapToLong(CollectionCount::sweepableRows).sum();
    }

    /** Per-collection standing of one tenant, each collection with rows to sweep censused. */
    List<CollectionCount> collectionsOf(String tenant, ChunkKeyPlan plan) {
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
