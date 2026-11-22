// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.sql.Savepoint;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.function.Function;

import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * One upsert statement on a table that carries both a name key and its id twin (RDR-229, nexus-p2m1x.20 finding #7).
 *
 * <p><b>The race.</b> {@code INSERT ... ON CONFLICT (k) DO UPDATE} absorbs a conflict only on its arbiter {@code k}. Until P4.1 drops
 * the name columns, chunks and taxonomy_centroids (P3.3b's per-leaf id unique index) and the tables P3.4's pass gives an id unique index
 * hold two unique keys over the same rows. Two writers inserting the same new row at once: the second one's speculative insert passes its
 * arbiter's check, then trips the OTHER key as a plain {@code 23505}, which aborts its transaction. Measured by
 * {@code SuppliedVectorsTest#twoWritersClassifyingTheSameNewChashesOppositeWays_doNotDeadlock} (3 of 3 red on the stack).
 *
 * <p><b>The rule (linda's ruling, 2026-11-22).</b> The statement runs under a savepoint. On a {@code 23505} whose constraint is the
 * non-arbiter member of the statement's own name/id pair, the savepoint is rolled back and the statement runs once more: by then the row
 * that won has committed, so the arbiter takes the conflict. One retry, logged {@code event=twin_key_upsert_retry}. A {@code 23505} on the
 * arbiter itself, on any other constraint, or a second {@code 23505}, surfaces unchanged. The caller names its arbiter, which flips at
 * P3.5c's switch (name key before, id key after), so nothing here guesses it. A {@code DO NOTHING} site needs none of this: it drops its
 * conflict target, and a target-less {@code DO NOTHING} arbitrates every unique index.
 *
 * <p><b>The pairs.</b> {@link #pairs()} is the one checked list: P3.3b's two leaf keys ({@code LeafIndexPass.KEYS}) here, and P3.4's
 * arbiters when {@code IdUniqueIndexPass.arbiters()} lands (marlowe). {@code TwinKeyUpsertIntegrationTest#pairsMatchTheMigratedSchema} pins each pair against
 * the migrated schema: both members are unique indexes of the table with exactly these columns.
 */
public final class TwinKeyUpsert {

    private static final Logger log = LoggerFactory.getLogger(TwinKeyUpsert.class);

    /** Which member of the pair the statement's {@code ON CONFLICT} names. */
    public enum Arbiter { NAME_KEY, ID_KEY }

    /** A table's name key and its id twin, as ordered column lists. */
    public record Pair(String table, List<String> nameKey, List<String> idKey) {
        public Pair {
            Objects.requireNonNull(table, "table");
            nameKey = List.copyOf(nameKey);
            idKey = List.copyOf(idKey);
        }

        List<String> other(Arbiter arbiter) {
            return arbiter == Arbiter.NAME_KEY ? idKey : nameKey;
        }
    }

    private static final List<Pair> PAIRS = List.of(
        new Pair("chunks", List.of("tenant_id", "collection", "chash", "embedding_model"),
                 List.of("tenant_id", "collection_id", "chash", "embedding_model")),
        new Pair("taxonomy_centroids", List.of("tenant_id", "collection", "topic_id", "embedding_model"),
                 List.of("tenant_id", "collection_id", "topic_id", "embedding_model")));

    private TwinKeyUpsert() {
    }

    /** The checked list of name/id pairs. */
    public static List<Pair> pairs() {
        return PAIRS;
    }

    static Optional<Pair> pair(String table) {
        return PAIRS.stream().filter(p -> p.table().equals(table)).findFirst();
    }

    /**
     * Runs {@code statement} on {@code ctx} under a savepoint, retrying it once per the class rule. {@code table} is the unqualified
     * {@code nexus} table the statement upserts into; it must be in {@link #pairs()}.
     */
    public static <T> T run(DSLContext ctx, String table, Arbiter arbiter, Function<DSLContext, T> statement) {
        Objects.requireNonNull(arbiter, "arbiter");
        Pair pair = pair(table).orElseThrow(() -> new IllegalArgumentException("no name/id pair for table " + table));
        try {
            return underSavepoint(ctx, statement);
        } catch (DataAccessException first) {
            String constraint = SqlConstraints.isUniqueViolation(first) ? SqlConstraints.violated(first) : null;
            if (constraint == null || !isMember(ctx, constraint, pair, pair.other(arbiter))) {
                throw first;
            }
            log.info("event=twin_key_upsert_retry table={} arbiter={} constraint={}", table, arbiter, constraint);
            return underSavepoint(ctx, statement);
        }
    }

    private static <T> T underSavepoint(DSLContext ctx, Function<DSLContext, T> statement) {
        Savepoint[] taken = new Savepoint[1];
        ctx.connection(conn -> taken[0] = conn.setSavepoint());
        T result;
        try {
            result = statement.apply(ctx);
        } catch (RuntimeException e) {
            ctx.connection(conn -> conn.rollback(taken[0]));
            throw e;
        }
        ctx.connection(conn -> conn.releaseSavepoint(taken[0]));
        return result;
    }

    /**
     * Whether {@code index} (the constraint a {@code 23505} named: a leaf's index or the parent's) is a unique index of {@code pair}'s table,
     * or of one of its partitions, whose columns are exactly {@code columns}. Read after the savepoint's rollback, so the transaction is usable.
     */
    static boolean isMember(DSLContext ctx, String index, Pair pair, List<String> columns) {
        var row = ctx.fetchOptional("""
            SELECT coalesce(pg_catalog.pg_partition_root(i.indrelid), i.indrelid)::pg_catalog.regclass::pg_catalog.text AS root,
                   (SELECT pg_catalog.array_agg(a.attname::pg_catalog.text ORDER BY k.ord)
                      FROM pg_catalog.unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                      JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum) AS cols
              FROM pg_catalog.pg_index i
              JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'nexus' AND c.relname = {0} AND i.indisunique""", DSL.val(index));
        if (row.isEmpty()) {
            return false;
        }
        String root = row.get().get("root", String.class);
        String[] cols = row.get().get("cols", String[].class);
        boolean sameTable = ("nexus." + pair.table()).equals(root) || pair.table().equals(root);
        return sameTable && cols != null && List.of(cols).equals(columns);
    }
}
