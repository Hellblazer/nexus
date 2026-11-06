// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.TreeMap;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;

/**
 * Bead nexus-hbnml (RDR-235 step B of nexus-bpikw): the one-shot, idempotent drain of the retired
 * {@code nexus.aspect_extraction_queue} table, run by the engine once at start.
 *
 * <p>{@code aspects-005-2} drops the table only when it holds no row, so an install an older client left rows in
 * keeps it. Nothing else reads it any more (the {@code /v1/aspects/queue/*} routes are gone), so this class moves
 * what is in it onto the {@code aspects} tuple subspace, where the worker, {@code nx aspects drain} and
 * {@code nx aspects requeue-failed} look:
 * <ul>
 *   <li>{@code pending} and {@code in_progress} rows become available {@code aspects} tasks, with the identity body
 *       the producer writes (collection, source_path, doc_id, content_hash), nonce = doc_id, and the row's
 *       {@code next_retry_at} as {@code delay_s} (a row mid-retry keeps its wait; an {@code in_progress} row, whose
 *       claimant is gone, moves with none);</li>
 *   <li>{@code failed} rows become dead-lettered tasks, so {@code nx aspects requeue-failed} revives them.</li>
 * </ul>
 * Each row is written and deleted in ONE transaction, scoped to the row's own tenant, so a crash leaves it in the
 * table (the next boot moves it) or in the tuple space and gone from the table, never in neither. A document
 * that already has a live task is not overwritten ({@link TupleRepository#drainReplaceWithin}): the row is the
 * older of the two, and is deleted.
 *
 * <p>Rows that cannot move stay, and are reported: {@code event=aspect_queue_rows_retained rows=N reasons=...}.
 * The next boot's guard drops the table the first time it is empty. {@link Result#rowsRetained()} is what
 * {@code GET /v1/status} serves, so {@code nx doctor} and the console never read a clean zero while rows remain.
 *
 * <p><b>Why the census is separate from the moves.</b> The table is FORCE ROW LEVEL SECURITY and the engine's
 * service role is not its owner, so the service role sees a tenant's rows only under that tenant's GUC, and cannot
 * list which tenants have any. {@link AdminCensus} reads {@code GROUP BY tenant_id} on the admin connection (the
 * table's owner) with FORCE lifted inside a transaction it rolls back, which puts FORCE back by construction. The
 * moves then run on the service pool, per tenant. If the census cannot run the drain falls back to the engine's
 * known tenants and reports the retained count as unknown ({@code -1}), never zero.
 */
public final class AspectQueueDrain {

    private static final Logger log = LoggerFactory.getLogger(AspectQueueDrain.class);

    /** The tuple subspace and key the producer writes ({@code HttpAspectQueue.ASPECTS_SUBSPACE / ASPECTS_KEYS}). */
    static final String SUBSPACE = "aspects";
    static final Map<String, String> KEYS = Map.of("queue", "aspects");

    /** Rows read per tenant per round trip. */
    static final int BATCH = 100;

    /** Per-row log lines for retained rows stop here; the aggregate line still counts them all. */
    private static final int MAX_ROW_LINES = 20;

    /** Retained-row reasons (the keys of {@link Result#reasons()}). */
    public static final String REASON_EMPTY_DOC_ID = "empty_doc_id";
    public static final String REASON_BODY_OVER_CAP = "body_over_cap";
    public static final String REASON_TUPLE_SPACE_FULL = "tuple_space_full";
    public static final String REASON_NO_TEMPLATE = "no_aspects_template";
    public static final String REASON_UNKNOWN_STATUS = "unknown_status";
    public static final String REASON_SCHEMA = "schema_violation";
    public static final String REASON_ERROR = "error";
    public static final String REASON_NO_TUPLE_SPACE = "no_tuple_space";
    public static final String REASON_NOT_MOVED = "not_moved";

    private static final Table<?> QUEUE = DSL.table(DSL.name("nexus", "aspect_extraction_queue"));
    private static final Field<Long> ID = DSL.field(DSL.name("id"), Long.class);
    private static final Field<String> TENANT = DSL.field(DSL.name("tenant_id"), String.class);

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** What the table holds, as the owner sees it. */
    public record Snapshot(boolean tablePresent, Map<String, Long> rowsByTenant) {
        public long total() {
            long n = 0;
            for (long v : rowsByTenant.values()) {
                n += v;
            }
            return n;
        }
    }

    /** The owner's view of the table: which tenants hold rows. */
    @FunctionalInterface
    public interface Census {
        Snapshot read() throws Exception;
    }

    /**
     * What a run did. {@code rowsRetained} is the rows left in the table as a census saw them after the moves, or
     * {@code -1} when the table is present and its rows could not be counted. {@code reasons} tallies the rows
     * this run chose not to move, by reason (never a tenant-keyed state: counts by reason only).
     */
    public record Result(boolean tablePresent, long rowsRetained, long moved, long deadLettered,
                         long alreadyQueued, Map<String, Long> reasons) {
        /** The status a store without the table reports. */
        public static Result absent() {
            return new Result(false, 0, 0, 0, 0, Map.of());
        }
    }

    private final TenantScope scope;
    private final TupleRepository tuples;
    private final Census census;
    private final Supplier<? extends Collection<String>> fallbackTenants;

    /**
     * @param tuples          the tuple space to write into, or null when the engine has no tuple templates (the
     *                        drain then only counts, and reports every row as retained)
     * @param fallbackTenants the tenants to try when the census cannot run
     */
    public AspectQueueDrain(TenantScope scope, TupleRepository tuples, Census census,
                            Supplier<? extends Collection<String>> fallbackTenants) {
        this.scope = scope;
        this.tuples = tuples;
        this.census = census;
        this.fallbackTenants = fallbackTenants;
    }

    /** Run once. Never throws: a failure is logged and reported as an unknown retained count. */
    public Result run() {
        try {
            return runUnguarded();
        } catch (RuntimeException e) {
            log.error("event=aspect_queue_drain_failed error_class={} error={}",
                e.getClass().getName(), e.getMessage(), e);
            return new Result(true, -1, 0, 0, 0, Map.of(REASON_ERROR, 1L));
        }
    }

    private Result runUnguarded() {
        Snapshot before = readCensus("before");
        if (before != null && !before.tablePresent()) {
            return Result.absent();
        }
        if (before != null && before.total() == 0) {
            log.info("event=aspect_queue_rows_drained tenants=0 moved=0 dead_lettered=0 "
                + "note=table_present_and_empty; the next boot drops it");
            return new Result(true, 0, 0, 0, 0, Map.of());
        }
        Collection<String> tenants = before != null ? before.rowsByTenant().keySet() : fallbackTenants.get();
        Tally tally = new Tally();
        if (tuples == null) {
            long rows = before == null ? 0 : before.total();
            tally.retain(REASON_NO_TUPLE_SPACE, rows);
            log.warn("event=aspect_queue_drain_skipped reason={} rows={}", REASON_NO_TUPLE_SPACE, rows);
        } else {
            for (String tenant : tenants) {
                if (tally.stopAll) {
                    break;
                }
                drainTenant(tenant, tally);
            }
        }
        for (var e : new TreeMap<>(tally.perTenant).entrySet()) {
            Counts c = e.getValue();
            log.info("event=aspect_queue_rows_drained tenant={} moved={} dead_lettered={} already_queued={} retained={}",
                e.getKey(), c.moved, c.deadLettered, c.alreadyQueued, c.retained);
        }
        Snapshot after = readCensus("after");
        long retained;
        if (after == null) {
            retained = -1;
        } else if (!after.tablePresent()) {
            retained = 0;
        } else {
            retained = after.total();
            long explained = tally.reasons.values().stream().mapToLong(Long::longValue).sum();
            if (retained > explained) {
                tally.retain(tally.residualReason, retained - explained);
            }
        }
        if (retained != 0) {
            log.warn("event=aspect_queue_rows_retained rows={} reasons={} remedy=\"see https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md#the-retired-aspect-queue-tuples-rung-and-the-aspect-queue-table; "
                + "the table is dropped at the first boot after it is empty\"",
                retained, tally.reasons);
        }
        return new Result(after == null || after.tablePresent(), retained, tally.moved, tally.deadLettered,
            tally.alreadyQueued, Map.copyOf(tally.reasons));
    }

    private Snapshot readCensus(String when) {
        try {
            return census.read();
        } catch (Exception e) {
            log.warn("event=aspect_queue_census_failed when={} error_class={} error={}; "
                + "the retained count is unknown", when, e.getClass().getName(), e.getMessage());
            return null;
        }
    }

    /** Per-tenant counts for the drained line. */
    private static final class Counts {
        long moved;
        long deadLettered;
        long alreadyQueued;
        long retained;
    }

    private static final class Tally {
        final Map<String, Counts> perTenant = new LinkedHashMap<>();
        final Map<String, Long> reasons = new LinkedHashMap<>();
        long moved;
        long deadLettered;
        long alreadyQueued;
        int rowLines;
        boolean stopAll;
        /** What the rows a stop left unvisited are reported as (they are counted from the final census). */
        String residualReason = REASON_NOT_MOVED;

        Counts of(String tenant) {
            return perTenant.computeIfAbsent(tenant, t -> new Counts());
        }

        void retain(String reason, long n) {
            reasons.merge(reason, n, Long::sum);
        }
    }

    private record Row(long id, String collection, String sourcePath, String docId, String contentHash,
                       String status, Long delaySeconds) { }

    private void drainTenant(String tenant, Tally tally) {
        Counts counts = tally.of(tenant);
        long lastId = Long.MIN_VALUE;
        long retentionCap;
        try {
            retentionCap = tuples.retentionSeconds(SUBSPACE);
        } catch (UnknownSubspaceException e) {
            tally.stopAll = true;
            tally.residualReason = REASON_NO_TEMPLATE;
            return;
        }
        boolean stopTenant = false;
        int consecutiveErrors = 0;
        while (!stopTenant && !tally.stopAll) {
            final long after = lastId;
            List<Row> batch = scope.withTenant(tenant, ctx -> readBatch(ctx, tenant, after, retentionCap));
            if (batch.isEmpty()) {
                break;
            }
            for (Row row : batch) {
                lastId = row.id();
                String refusal;
                try {
                    refusal = moveRow(tenant, row, counts, tally);
                    consecutiveErrors = 0;
                } catch (MaxLiveRowsExceededException e) {
                    refusal = REASON_TUPLE_SPACE_FULL;
                    stopTenant = true;
                    tally.residualReason = REASON_TUPLE_SPACE_FULL;
                } catch (UnknownSubspaceException e) {
                    refusal = REASON_NO_TEMPLATE;
                    tally.stopAll = true;
                    tally.residualReason = REASON_NO_TEMPLATE;
                } catch (TooLargeException e) {
                    refusal = REASON_BODY_OVER_CAP;
                } catch (SchemaViolationException e) {
                    refusal = REASON_SCHEMA;
                } catch (RuntimeException e) {
                    refusal = REASON_ERROR;
                    log.warn("event=aspect_queue_row_move_failed tenant={} row_id={} error_class={} error={}",
                        tenant, row.id(), e.getClass().getName(), e.getMessage());
                    if (++consecutiveErrors >= 3) {
                        stopTenant = true;
                    }
                }
                if (refusal != null) {
                    counts.retained++;
                    tally.retain(refusal, 1);
                    if (tally.rowLines++ < MAX_ROW_LINES) {
                        log.warn("event=aspect_queue_row_retained tenant={} row_id={} status={} reason={}",
                            tenant, row.id(), row.status(), refusal);
                    }
                }
                if (stopTenant || tally.stopAll) {
                    break;
                }
            }
            tuples.signalAvailable(tenant, SUBSPACE);
        }
    }

    private static List<Row> readBatch(DSLContext ctx, String tenant, long afterId, long retentionCap) {
        OffsetDateTime now = ctx.select(DSL.currentOffsetDateTime()).fetchOne(0, OffsetDateTime.class);
        Table<?> q = QUEUE.as("q");
        Field<Long> qId = DSL.field(DSL.name("q", "id"), Long.class);
        Field<String> qTenant = DSL.field(DSL.name("q", "tenant_id"), String.class);
        Field<String> qCollection = DSL.field(DSL.name("q", "collection"), String.class);
        Field<String> qSourcePath = DSL.field(DSL.name("q", "source_path"), String.class);
        Field<String> qDocId = DSL.field(DSL.name("q", "doc_id"), String.class);
        Field<String> qHash = DSL.field(DSL.name("q", "content_hash"), String.class);
        Field<String> qStatus = DSL.field(DSL.name("q", "status"), String.class);
        Field<OffsetDateTime> qNext = DSL.field(DSL.name("q", "next_retry_at"), OffsetDateTime.class);
        var d = CATALOG_DOCUMENTS;
        List<Row> out = new ArrayList<>();
        for (Record r : ctx.select(qId, qCollection, qSourcePath, qDocId, qHash, qStatus, qNext, d.PHYSICAL_COLLECTION)
                .from(q)
                .leftJoin(d).on(d.TENANT_ID.eq(qTenant).and(d.TUMBLER.eq(qDocId)))
                .where(qTenant.eq(tenant).and(qId.gt(afterId)))
                .orderBy(qId)
                .limit(BATCH)
                .fetch()) {
            OffsetDateTime next = r.get(qNext);
            Long delay = null;
            if (next != null && next.isAfter(now)) {
                long ms = java.time.Duration.between(now, next).toMillis();
                delay = Math.min(retentionCap, (ms + 999) / 1000);
            }
            // The row's collection name is as old as the row: a collection rename or a re-embed move since then
            // re-homed the DOCUMENT (catalog_documents.physical_collection) and not this retired table. The
            // document's current home is the name a task must carry; the row's own is the fallback.
            String home = r.get(d.PHYSICAL_COLLECTION);
            String collection = home == null || home.isEmpty() ? r.get(qCollection) : home;
            out.add(new Row(r.get(qId), collection, r.get(qSourcePath), nz(r.get(qDocId)),
                nz(r.get(qHash)), r.get(qStatus), delay));
        }
        return out;
    }

    private static String nz(String s) {
        return s == null ? "" : s;
    }

    /**
     * Moves one row; returns null when moved (or already queued) and the reason when it stays. The write and the
     * delete are one transaction; exceptions the tuple write raises before any change propagate to the caller.
     */
    private String moveRow(String tenant, Row row, Counts counts, Tally tally) {
        if (row.docId().isEmpty()) {
            // The task's nonce is the doc_id; a nonce is not invented.
            return REASON_EMPTY_DOC_ID;
        }
        boolean dead;
        Long delay;
        switch (row.status()) {
            case "pending" -> { dead = false; delay = row.delaySeconds(); }
            case "in_progress" -> { dead = false; delay = null; }
            case "failed" -> { dead = true; delay = null; }
            default -> { return REASON_UNKNOWN_STATUS; }
        }
        String body = identityBody(row);
        TupleRepository.DrainWrite written = scope.withTenant(tenant, ctx -> {
            TupleRepository.DrainWrite w =
                tuples.drainReplaceWithin(ctx, tenant, SUBSPACE, KEYS, body, row.docId(), delay, dead);
            ctx.deleteFrom(QUEUE).where(TENANT.eq(tenant).and(ID.eq(row.id()))).execute();
            return w;
        });
        if (written == TupleRepository.DrainWrite.ALREADY_QUEUED) {
            counts.alreadyQueued++;
            tally.alreadyQueued++;
        } else if (dead) {
            counts.deadLettered++;
            tally.deadLettered++;
        } else {
            counts.moved++;
            tally.moved++;
        }
        return null;
    }

    /** The producer's identity body ({@code HttpAspectQueue._identity_body}): compact JSON, never document text. */
    static String identityBody(Row row) {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("collection", row.collection());
        m.put("source_path", row.sourcePath());
        m.put("doc_id", row.docId());
        m.put("content_hash", row.contentHash());
        try {
            return MAPPER.writeValueAsString(m);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * The census on the admin connection ({@link AdminConnection}): the table's owner, so a transaction that lifts
     * FORCE ROW LEVEL SECURITY, groups by tenant and rolls back sees every tenant's rows and leaves the table as it
     * found it. Opens its own connection per read.
     */
    public static final class AdminCensus implements Census {

        private static final int LOGIN_TIMEOUT_SECONDS = 10;
        private static final int SOCKET_TIMEOUT_SECONDS = 60;

        private final AdminConnection admin;

        public AdminCensus(AdminConnection admin) {
            this.admin = admin;
        }

        @Override
        public Snapshot read() throws SQLException {
            Properties props = new Properties();
            props.setProperty("user", admin.user());
            props.setProperty("password", admin.password());
            props.setProperty("loginTimeout", Integer.toString(LOGIN_TIMEOUT_SECONDS));
            props.setProperty("connectTimeout", Integer.toString(LOGIN_TIMEOUT_SECONDS));
            props.setProperty("socketTimeout", Integer.toString(SOCKET_TIMEOUT_SECONDS));
            props.setProperty("ApplicationName", "nexus-aspect-queue-drain");
            try (Connection conn = DriverManager.getConnection(admin.url(), props)) {
                conn.setAutoCommit(false);
                try {
                    return readOn(conn);
                } finally {
                    conn.rollback();
                }
            }
        }

        // SANCTIONED RAW (nexus-hbnml): ALTER TABLE ... NO FORCE ROW LEVEL SECURITY is DDL with no jOOQ typed-DSL form,
        // the same category as WalkLockPreflight.tenants (which counts tenants the same way: FORCE off in a
        // transaction that is rolled back). The table name is a constant.
        static Snapshot readOn(Connection conn) throws SQLException {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            // The ALTER takes ACCESS EXCLUSIVE: bounded, so a census beside a long writer fails (and the run reports
            // the retained count as unknown) instead of waiting silently.
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val("lock_timeout"), DSL.val("10s"), DSL.val(true))).fetchOne();
            Table<?> cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
            Table<?> ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
            Field<Boolean> forceField = DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class);
            Boolean forced = ctx.select(forceField).from(cls).join(ns)
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")
                    .and(DSL.field(DSL.name("c", "relname"), String.class).eq("aspect_extraction_queue")))
                .fetchOptional(forceField).orElse(null);
            if (forced == null) {
                return new Snapshot(false, Map.of());
            }
            if (forced) {
                String table = "aspect_extraction_queue";
                try (Statement alter = conn.createStatement()) {
                    alter.execute("ALTER TABLE nexus." + table + " NO FORCE ROW LEVEL SECURITY");
                }
            }
            Map<String, Long> byTenant = new TreeMap<>();
            for (Record r : ctx.select(TENANT, DSL.count()).from(QUEUE).groupBy(TENANT).fetch()) {
                byTenant.put(r.get(TENANT), r.get(1, Long.class));
            }
            return new Snapshot(true, byTenant);
        }
    }
}
