// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.ChunksLeafAutovacuumCheck;
import dev.nexus.service.db.SweepBounds;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
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
 * {@code autovacuum_enabled} was switched off; and, for every {@code pci_} index on the leaf, its size in bytes.
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

    /** The base of the overdue line: {@code autovacuum_vacuum_threshold} of vectors-034 (and the s9wav leaf policy). */
    public static final long OVERDUE_BASE_ROWS = 1000;

    /**
     * The scale of the overdue line: twice vectors-034's {@code autovacuum_vacuum_scale_factor} of 0.05, so a leaf is
     * overdue only once it is past the point where autovacuum should already have started.
     */
    public static final double OVERDUE_SCALE_FACTOR = 0.10;

    /** The statement bound of one read: the engine's sweep-statement bound. */
    public static final Duration DEFAULT_READ_BOUND = SweepBounds.STATEMENT_TIMEOUT;

    /**
     * One {@code pci_} index on a leaf.
     *
     * @param name  the index name
     * @param valid {@code pg_index.indisvalid}
     * @param bytes {@code pg_relation_size} of the index when the read ran
     */
    public record PciIndex(String name, boolean valid, long bytes) { }

    /**
     * One leaf of {@code nexus.chunks}.
     *
     * @param name               the leaf's relation name
     * @param liveRows           {@code n_live_tup}; 0 for a leaf the statistics have no row for yet
     * @param deadRows           {@code n_dead_tup}; 0 likewise
     * @param lastAutovacuum     when autovacuum last finished on the leaf, or {@code null} when it has not
     * @param vacuumCount        {@code vacuum_count + autovacuum_count}: vacuums that have finished on the leaf since
     *                           the statistics began (a reset of the statistics sets it back to 0)
     * @param policySet          all four {@code vectors-034} autovacuum parameters are present
     * @param autovacuumEnabled  false only when {@code autovacuum_enabled} is set to a false spelling
     * @param pciIndexes         its {@code pci_} indexes in name order; empty when it has none
     */
    public record Leaf(String name, long liveRows, long deadRows, Instant lastAutovacuum, long vacuumCount,
                       boolean policySet, boolean autovacuumEnabled, List<PciIndex> pciIndexes) {
        public Leaf {
            pciIndexes = List.copyOf(pciIndexes);
        }

        /** {@code dead / (live + dead)}, 0 when the leaf holds neither. */
        public double deadFraction() {
            long total = liveRows + deadRows;
            return total == 0 ? 0.0 : (double) deadRows / (double) total;
        }

        /**
         * True when autovacuum is overdue on this leaf: {@code dead > OVERDUE_BASE_ROWS + OVERDUE_SCALE_FACTOR x
         * (live + dead)}. A small leaf at a high dead fraction is not overdue (autovacuum has no reason to run); a
         * 100,000-row leaf at 12 percent is.
         */
        public boolean overdue() {
            return deadRows > OVERDUE_BASE_ROWS + OVERDUE_SCALE_FACTOR * (double) (liveRows + deadRows);
        }
    }

    /** What one read saw, leaves in name order. Empty means the read found no leaf of {@code nexus.chunks}. */
    public record Snapshot(List<Leaf> leaves) {
        public Snapshot {
            leaves = List.copyOf(leaves);
        }
    }

    private static final String PCI_PREFIX = PciCatalog.PREFIX;

    private final DataSource dataSource;
    private final Duration readBound;

    /** @param dataSource the pool the engine's role connects through; the read borrows one connection per call */
    public LeafBloatCatalog(DataSource dataSource) {
        this(dataSource, DEFAULT_READ_BOUND);
    }

    LeafBloatCatalog(DataSource dataSource, Duration readBound) {
        this.dataSource = Objects.requireNonNull(dataSource, "dataSource");
        this.readBound = Objects.requireNonNull(readBound, "readBound");
        if (readBound.toMillis() < 1) {
            throw new IllegalArgumentException("readBound must be at least 1 ms (0 would disable it), got " + readBound);
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

    /** Read through {@code ctx}: three statements in the caller's transaction. */
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

        // The pci_ indexes, keyed by the leaf they sit on.
        Field<Long> indRelid = DSL.field(DSL.name("i", "indrelid")).cast(SQLDataType.BIGINT);
        Field<String> indName = DSL.field(DSL.name("ix", "relname"), SQLDataType.VARCHAR);
        Field<Boolean> indValid = DSL.field(DSL.name("i", "indisvalid"), SQLDataType.BOOLEAN);
        Field<Long> indBytes = DSL.function(DSL.name("pg_catalog", "pg_relation_size"), SQLDataType.BIGINT,
            DSL.field(DSL.name("ix", "oid")));
        Map<Long, List<PciIndex>> indexes = new HashMap<>();
        for (var row : ctx.select(indRelid, indName, indValid, indBytes)
                .from(DSL.table(DSL.name("pg_catalog", "pg_index")).as("i"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("ix"))
                .on(DSL.field(DSL.name("ix", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
                .where(DSL.left(indName, PCI_PREFIX.length()).eq(PCI_PREFIX))
                .orderBy(indName)
                .fetch()) {
            indexes.computeIfAbsent(row.value1(), k -> new ArrayList<>())
                .add(new PciIndex(row.value2(), Boolean.TRUE.equals(row.value3()), nz(row.value4())));
        }

        List<Leaf> leaves = new ArrayList<>(rels.size());
        for (ChunksLeafAutovacuumCheck.LeafRel rel : rels) {
            long[] c = counts.getOrDefault(rel.oid(), new long[] {0, 0, 0});
            leaves.add(new Leaf(rel.name(), c[0], c[1], vacuumed.get(rel.oid()), c[2], rel.policySet(),
                !rel.autovacuumDisabled(), indexes.getOrDefault(rel.oid(), List.of())));
        }
        leaves.sort(Comparator.comparing(Leaf::name));
        return new Snapshot(leaves);
    }

    private static long nz(Long value) {
        return value == null ? 0L : value;
    }
}
