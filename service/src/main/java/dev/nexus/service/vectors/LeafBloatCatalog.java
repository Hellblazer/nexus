// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.ChunksLeafAutovacuumCheck;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.SweepBounds;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Savepoint;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * nexus-dxsjc (HNSW maintenance policy nexus-4lnn1, R4): the per-leaf bloat view of {@code nexus.chunks}. One read
 * says, for every ordinary leaf, how many live and dead tuples the statistics hold, when autovacuum last finished on
 * it, whether it still carries the four autovacuum storage parameters of {@code vectors-034} and whether
 * {@code autovacuum_enabled} was switched off; and, for the authenticated leaf listing (nexus-i827c), the size of every
 * {@code pci_} index on it.
 *
 * <p><b>Leaves, never the parent.</b> The partitioned parent and the model partitions hold no tuples and autovacuum
 * never visits them, so their {@code pg_stat_user_tables} rows are empty; reading {@code relname = 'chunks'} reports
 * zero bloat for a table that has some. The leaves come from {@link ChunksLeafAutovacuumCheck#leaves}, the same walk
 * the post-migrate check uses, so the two cannot disagree about what a leaf is.
 *
 * <p><b>"Over" means autovacuum is overdue, not a flat fraction.</b> Autovacuum starts on a leaf when its dead rows pass
 * {@code autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor x rows} (vectors-034: 1000 and 0.05). A flat
 * dead fraction ignores the 1000-row base, so a leaf of a few thousand rows sits far above 10 percent dead while
 * autovacuum correctly leaves it alone. {@link Leaf#overdue()} therefore asks for the base plus twice the scale
 * (0.10): the leaf is past the point where autovacuum should already have fired. The dead fraction stays in the view as
 * information.
 *
 * <p><b>"Rows" is the larger of two counts.</b> Autovacuum scales its trigger by {@code pg_class.reltuples}, the planner's
 * row estimate, which survives a restart. {@code n_live_tup} does not: an unclean shutdown, {@code pg_stat_reset}, a
 * restore or a major upgrade zero it, and it stays 0 until a vacuum or analyze runs, which a quiet leaf may never get.
 * Reading {@code n_live_tup + n_dead_tup} alone would then call a 100,000-row leaf with 1,500 deletes overdue while
 * autovacuum, correctly, stays idle. So rows is {@code max(live + dead, reltuples)}, {@code reltuples} read as 0 when
 * the leaf has never been analyzed ({@code -1}).
 *
 * <p><b>The counts are an estimate.</b> {@code n_dead_tup} is the statistics system's count, not a scan, and a leaf in
 * the middle of a cost-throttled vacuum shows its dead tuples until the vacuum ends. {@link LeafBloatSweep} therefore
 * judges an overdue leaf on how long it has been overdue and on whether a vacuum has finished since it became so; the
 * vacuum count ({@link Leaf#vacuumCount()}) is what separates the two.
 *
 * <p><b>The read is bounded</b> as {@link PciCatalog#read()} is (nexus-u9zkn): one borrowed connection, a transaction
 * that opens with {@code set_config('statement_timeout', ...)} through {@link SweepBounds#applyStatementTimeout}.
 * Every read here is a catalog or statistics-view read through jOOQ's name-based DSL; nothing here is raw SQL text.
 */
public final class LeafBloatCatalog {

    private static final Logger log = LoggerFactory.getLogger(LeafBloatCatalog.class);

    /** SQLSTATE of a lock wait that ran out ({@code lock_timeout}). */
    private static final String LOCK_NOT_AVAILABLE = "55P03";

    /**
     * How long the {@code pci_} index size read waits for a lock. {@code pg_relation_size} opens the index with an
     * ACCESS SHARE lock, so a non-concurrent {@code REINDEX} or {@code DROP INDEX} (ACCESS EXCLUSIVE on the index)
     * makes it wait. The sizes are listing-only, so the wait is short and its failure costs only the sizes (see
     * {@link #pciIndexSizes}), never the aggregate view the status route reads.
     */
    public static final Duration PCI_SIZE_LOCK_BOUND = Duration.ofSeconds(1);

    /** The base of the overdue line: {@code autovacuum_vacuum_threshold} of vectors-034 (and the s9wav leaf policy). */
    public static final long OVERDUE_BASE_ROWS = 1000;

    /**
     * The scale of the overdue line: twice vectors-034's {@code autovacuum_vacuum_scale_factor} of 0.05, so a leaf is
     * overdue only once it is past the point where autovacuum should already have started.
     */
    public static final double OVERDUE_SCALE_FACTOR = 0.10;

    /** The statement bound of one read: the engine's sweep-statement bound. */
    public static final Duration DEFAULT_READ_BOUND = SweepBounds.STATEMENT_TIMEOUT;

    /** The prefix of a per-collection index name ({@link PciCatalog#PREFIX}); the size read considers no other index. */
    private static final String PCI_PREFIX = PciCatalog.PREFIX;

    /**
     * One {@code pci_} index on a leaf (nexus-i827c: read again, for the authenticated leaf listing; the aggregate
     * view on {@code GET /v1/status} does not carry it).
     *
     * @param name  the index name
     * @param valid {@code pg_index.indisvalid}
     * @param bytes {@code pg_relation_size} of the index when the read ran, or {@code null} when the size could not be
     *              read (a lock held on the index past {@link #PCI_SIZE_LOCK_BOUND}); unknown is never 0
     */
    public record PciIndex(String name, boolean valid, Long bytes) { }

    /**
     * One leaf of {@code nexus.chunks}.
     *
     * @param name               the leaf's relation name
     * @param liveRows           {@code n_live_tup}; 0 for a leaf the statistics have no row for yet
     * @param deadRows           {@code n_dead_tup}; 0 likewise
     * @param plannerRows        {@code pg_class.reltuples}; 0 for a leaf never analyzed ({@code -1})
     * @param lastAutovacuum     when autovacuum last finished on the leaf, or {@code null} when it has not
     * @param vacuumCount        {@code vacuum_count + autovacuum_count}: vacuums that have finished on the leaf since
     *                           the statistics began (a reset of the statistics sets it back to 0)
     * @param policySet          all four {@code vectors-034} autovacuum parameters are present
     * @param autovacuumEnabled  false only when {@code autovacuum_enabled} is set to a false spelling
     * @param pciIndexes         its {@code pci_} indexes in name order; empty when it has none
     */
    public record Leaf(String name, long liveRows, long deadRows, long plannerRows, Instant lastAutovacuum,
                       long vacuumCount, boolean policySet, boolean autovacuumEnabled, List<PciIndex> pciIndexes) {

        public Leaf {
            pciIndexes = List.copyOf(pciIndexes);
        }

        /** A leaf with no {@code pci_} index, which is every leaf the aggregate view's tests build. */
        public Leaf(String name, long liveRows, long deadRows, long plannerRows, Instant lastAutovacuum,
                    long vacuumCount, boolean policySet, boolean autovacuumEnabled) {
            this(name, liveRows, deadRows, plannerRows, lastAutovacuum, vacuumCount, policySet, autovacuumEnabled,
                List.of());
        }

        /**
         * Bytes of every {@code pci_} index on the leaf, valid or not (an invalid one still occupies its pages); 0 for a
         * leaf with none, and {@code null} when any of them has no size ({@link PciIndex#bytes()}), so a size that could
         * not be read is not reported as a small one.
         */
        public Long pciIndexBytes() {
            long total = 0;
            for (PciIndex i : pciIndexes) {
                if (i.bytes() == null) {
                    return null;
                }
                total += Math.max(0L, i.bytes());
            }
            return total;
        }

        /**
         * The row count the overdue line scales by: the larger of the statistics' {@code live + dead} and the planner's
         * estimate, each clamped at 0, so a leaf whose counters were reset is judged on the estimate autovacuum itself
         * uses.
         */
        public long rows() {
            return Math.max(Math.max(0L, liveRows) + Math.max(0L, deadRows), Math.max(0L, plannerRows));
        }

        /** {@code dead / (live + dead)}, 0 when the leaf holds neither. */
        public double deadFraction() {
            long total = liveRows + deadRows;
            return total == 0 ? 0.0 : (double) deadRows / (double) total;
        }

        /**
         * True when autovacuum is overdue on this leaf: {@code dead > OVERDUE_BASE_ROWS + OVERDUE_SCALE_FACTOR x
         * rows()}. A small leaf at a high dead fraction is not overdue (autovacuum has no reason to run); a
         * 100,000-row leaf at 12 percent is; a 100,000-row leaf whose counters were reset and holds 1,500 dead is not.
         */
        public boolean overdue() {
            return Math.max(0L, deadRows) > OVERDUE_BASE_ROWS + OVERDUE_SCALE_FACTOR * (double) rows();
        }
    }

    /** What one read saw, leaves in name order. Empty means the read found no leaf of {@code nexus.chunks}. */
    public record Snapshot(List<Leaf> leaves) {
        public Snapshot {
            leaves = List.copyOf(leaves);
        }
    }

    private final DataSource dataSource;
    private final Duration readBound;
    private final Duration pciSizeLockBound;

    /** @param dataSource the pool the engine's role connects through; the read borrows one connection per call */
    public LeafBloatCatalog(DataSource dataSource) {
        this(dataSource, DEFAULT_READ_BOUND);
    }

    LeafBloatCatalog(DataSource dataSource, Duration readBound) {
        this(dataSource, readBound, PCI_SIZE_LOCK_BOUND);
    }

    /** As above with the {@code pci_} size read's lock bound named, for a test that cannot wait a second. */
    LeafBloatCatalog(DataSource dataSource, Duration readBound, Duration pciSizeLockBound) {
        this.dataSource = Objects.requireNonNull(dataSource, "dataSource");
        this.readBound = Objects.requireNonNull(readBound, "readBound");
        this.pciSizeLockBound = Objects.requireNonNull(pciSizeLockBound, "pciSizeLockBound");
        if (readBound.toMillis() < 1) {
            throw new IllegalArgumentException("readBound must be at least 1 ms (0 would disable it), got " + readBound);
        }
        if (pciSizeLockBound.toMillis() < 1) {
            throw new IllegalArgumentException(
                "pciSizeLockBound must be at least 1 ms (0 would disable it), got " + pciSizeLockBound);
        }
    }

    /** The statement bound of {@link #read()}. */
    public Duration readBound() {
        return readBound;
    }

    /**
     * Read the leaves on a connection borrowed from the pool and returned before this method does.
     *
     * @throws DataAccessException when a statement fails, hits {@link #readBound()} (SQLSTATE 57014) or the server
     *                             stays silent past the bound plus the network margin
     */
    public Snapshot read() {
        try (Connection conn = dataSource.getConnection()) {
            conn.setAutoCommit(false);
            try {
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                SweepBounds.applyStatementTimeout(ctx, readBound);
                Snapshot snapshot = read(ctx);
                conn.commit();
                return snapshot;
            } catch (RuntimeException e) {
                try {
                    conn.rollback();
                } catch (SQLException rollback) {
                    e.addSuppressed(rollback);
                }
                throw e;
            }
        } catch (SQLException e) {
            throw new DataAccessException("chunks leaf bloat read failed: " + e.getMessage(), e);
        }
    }

    /** Read through {@code ctx}: four statements in the caller's transaction. */
    Snapshot read(DSLContext ctx) {
        List<ChunksLeafAutovacuumCheck.LeafRel> rels = ChunksLeafAutovacuumCheck.leaves(ctx);
        if (rels.isEmpty()) {
            return new Snapshot(List.of());
        }

        // pg_stat_user_tables, keyed by relid. A leaf with no row yet (never touched since the statistics were
        // reset) reads as zero live and zero dead.
        Field<Long> statRelid = DSL.field(DSL.name("s", "relid")).cast(SQLDataType.BIGINT);
        Field<Long> live = DSL.field(DSL.name("s", "n_live_tup"), SQLDataType.BIGINT);
        Field<Long> dead = DSL.field(DSL.name("s", "n_dead_tup"), SQLDataType.BIGINT);
        Field<java.time.OffsetDateTime> lastAutovacuum =
            DSL.field(DSL.name("s", "last_autovacuum"), SQLDataType.TIMESTAMPWITHTIMEZONE);
        Field<Long> vacuumCount = DSL.field(DSL.name("s", "vacuum_count"), SQLDataType.BIGINT);
        Field<Long> autovacuumCount = DSL.field(DSL.name("s", "autovacuum_count"), SQLDataType.BIGINT);
        Map<Long, long[]> counts = new HashMap<>();
        Map<Long, Instant> vacuumed = new HashMap<>();
        for (var row : ctx.select(statRelid, live, dead, lastAutovacuum, vacuumCount, autovacuumCount)
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_tables")).as("s"))
                .where(DSL.field(DSL.name("s", "schemaname"), SQLDataType.VARCHAR).eq("nexus"))
                .fetch()) {
            counts.put(row.value1(),
                new long[] {nz(row.value2()), nz(row.value3()), nz(row.value5()) + nz(row.value6())});
            if (row.value4() != null) {
                vacuumed.put(row.value1(), row.value4().toInstant());
            }
        }

        // pg_class.reltuples, keyed by oid: the row estimate autovacuum scales its trigger by. It survives a restart
        // and a statistics reset, which n_live_tup does not; -1 (never analyzed) reads as 0.
        Field<Long> classOid = DSL.field(DSL.name("c", "oid")).cast(SQLDataType.BIGINT);
        Field<Double> reltuples = DSL.field(DSL.name("c", "reltuples")).cast(SQLDataType.DOUBLE);
        Map<Long, Long> estimates = new HashMap<>();
        for (var row : ctx.select(classOid, reltuples)
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .where(classOid.in(rels.stream().map(ChunksLeafAutovacuumCheck.LeafRel::oid).toList()))
                .fetch()) {
            Double est = row.value2();
            estimates.put(row.value1(), est == null || est < 0 ? 0L : Math.round(est));
        }

        // The pci_ indexes, keyed by the leaf they sit on (nexus-i827c). Their names and validity come from the
        // catalog, which takes no lock on the index; the sizes are a separate, bounded step (pciIndexSizes) so a
        // lock held on one index costs the sizes and nothing else.
        Field<Long> indRelid = DSL.field(DSL.name("i", "indrelid")).cast(SQLDataType.BIGINT);
        Field<String> indName = DSL.field(DSL.name("ix", "relname"), SQLDataType.VARCHAR);
        Field<Boolean> indValid = DSL.field(DSL.name("i", "indisvalid"), SQLDataType.BOOLEAN);
        Map<String, Long> sizes = pciIndexSizes(ctx);
        Map<Long, List<PciIndex>> indexes = new HashMap<>();
        for (var row : ctx.select(indRelid, indName, indValid)
                .from(DSL.table(DSL.name("pg_catalog", "pg_index")).as("i"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("ix"))
                .on(DSL.field(DSL.name("ix", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
                .where(DSL.left(indName, PCI_PREFIX.length()).eq(PCI_PREFIX))
                .orderBy(indName)
                .fetch()) {
            indexes.computeIfAbsent(row.value1(), k -> new ArrayList<>())
                .add(new PciIndex(row.value2(), Boolean.TRUE.equals(row.value3()), sizes.get(row.value2())));
        }

        List<Leaf> leaves = new ArrayList<>(rels.size());
        for (ChunksLeafAutovacuumCheck.LeafRel rel : rels) {
            long[] c = counts.getOrDefault(rel.oid(), new long[] {0, 0, 0});
            leaves.add(new Leaf(rel.name(), c[0], c[1], estimates.getOrDefault(rel.oid(), 0L),
                vacuumed.get(rel.oid()), c[2], rel.policySet(), !rel.autovacuumDisabled(),
                indexes.getOrDefault(rel.oid(), List.of())));
        }
        leaves.sort(Comparator.comparing(Leaf::name));
        return new Snapshot(leaves);
    }

    /**
     * The size of every {@code pci_} index, by name, or an EMPTY map when a lock held on one of them outlasted
     * {@link #PCI_SIZE_LOCK_BOUND} (a non-concurrent {@code REINDEX} or {@code DROP INDEX}).
     *
     * <p>Why a separate step with its own lock bound, not one more column of the statement above: {@code
     * pg_relation_size} takes an ACCESS SHARE lock on each index, so with none a DDL on a single {@code pci_} index
     * would stall the whole read to its 30 s statement bound and fail it, and {@code GET /v1/status} would then report
     * {@code chunks_leaf_bloat} expired for a read that only wanted a listing's byte counts. The step runs in a
     * savepoint: a lock timeout aborts the statement, and without one it would abort the surrounding transaction and
     * with it the counts. A failure other than the lock wait (the statement bound included) is rethrown as before;
     * only the lock wait is absorbed, and it is logged.
     */
    private Map<String, Long> pciIndexSizes(DSLContext ctx) {
        Field<String> name = DSL.field(DSL.name("ix", "relname"), SQLDataType.VARCHAR);
        Field<Long> bytes = DSL.function(DSL.name("pg_catalog", "pg_relation_size"), SQLDataType.BIGINT,
            DSL.field(DSL.name("ix", "oid")));
        Map<String, Long> sizes = new HashMap<>();
        try {
            ctx.connection(conn -> {
                Savepoint savepoint = conn.setSavepoint();
                try {
                    PgSession.setLocal(ctx, "lock_timeout", Long.toString(pciSizeLockBound.toMillis()));
                    for (var row : ctx.select(name, bytes)
                            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("ix"))
                            .where(DSL.left(name, PCI_PREFIX.length()).eq(PCI_PREFIX))
                            .and(DSL.field(DSL.name("ix", "relkind"), SQLDataType.VARCHAR).eq("i"))
                            .fetch()) {
                        if (row.value2() != null) {   // NULL: the index went away since the name was read
                            sizes.put(row.value1(), row.value2());
                        }
                    }
                    // Roll back to the savepoint even on success: the sizes are already read, and only a
                    // rollback reverts the SET LOCAL lock_timeout (a release keeps it until the transaction ends).
                    conn.rollback(savepoint);
                    conn.releaseSavepoint(savepoint);
                } catch (RuntimeException e) {
                    conn.rollback(savepoint);
                    throw e;
                }
            });
        } catch (DataAccessException e) {
            if (!LOCK_NOT_AVAILABLE.equals(sqlState(e))) {
                throw e;
            }
            log.warn("event=chunks_leaf_bloat_pci_size_unread reason=lock_wait bound_ms={}", pciSizeLockBound.toMillis());
            sizes.clear();
        }
        return sizes;
    }

    private static String sqlState(Throwable e) {
        int depth = 0;
        for (Throwable c = e; c != null && depth < 32; c = c.getCause(), depth++) {
            if (c instanceof SQLException sql && sql.getSQLState() != null) {
                return sql.getSQLState();
            }
        }
        return null;
    }

    private static long nz(Long value) {
        return value == null ? 0L : value;
    }
}
