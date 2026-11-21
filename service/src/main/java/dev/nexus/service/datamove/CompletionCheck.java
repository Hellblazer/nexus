// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.PgSession;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), the boot-time completion check (RDR item 6, "Readers during the stamp"): on each install,
 * have the stamp moves finished, and so may the engine's id readers, the one-row rename and the index builds switch?
 *
 * <p><b>READ ONLY.</b> Nothing here writes a row, runs DDL or registers a move: Sam's rule is no boot-time data migrations,
 * and a data move is a visible, resumable job. {@link #evaluate} reads the registry ({@code nexus.data_moves}, admin
 * connection only: {@code nexus_svc} holds no grant), the catalog and the CHECK constraints, and returns what it found; the
 * engine reports it through {@code boot_work}, which {@code nx doctor} reads ({@link CompletionBootWork}). A test runs it in
 * a READ ONLY transaction, so a write would raise 25006.
 *
 * <p><b>Complete</b> means the label's move has {@code completed_at}, or the move that supersedes it has. A label with no row
 * is incomplete, because later releases' rows are registered at run time. Two exceptions, both settled by residual R3 (the
 * queue table is absent on most installs): a failed move whose driving table no longer exists counts as complete, and the
 * one allowlisted label ({@link StampLabels#NOT_APPLICABLE_WHEN_ABSENT}) is <i>not applicable</i>, whether it has no row or
 * a failed one, while its table or its {@code collection_id} column is absent (vectors-068-18 is skipped, unrecorded, on an
 * install whose kept queue table another role owns, so a stamp there is never a failed move). Every other label with no row
 * stays incomplete.
 *
 * <p>A stamp label is complete only while its table's CHECKs are present and validated. A move whose {@code on_complete}
 * could not validate (a row names a collection that has no registry row in its tenant: an orphan) leaves the CHECK
 * {@code NOT VALID}, so its label stays incomplete and the check says how many rows violate it: the VALIDATE waits instead of
 * failing a boot, and {@code nx doctor} names the table and the count.
 *
 * <p><b>The gates, in order</b> (a gate holds only if the one before it does):
 * <ol>
 *   <li>every stamp move (and P3.3a's, when a later release adds them) is complete: P3.3b's pass starts;</li>
 *   <li>also both P3.3c swap moves and the three child validation moves (a later release adds them): P3.4's builds start;</li>
 *   <li>also P3.4 complete on the install, per residual S-2 (nexus-p2m1x.40): every collection the planner would index has a
 *       VALID id-predicate index, read from the registry and the index catalog, never from the existence of name indexes
 *       (the planner drops a name index before it builds, so counting them passes early). {@link Gate3Source} supplies the
 *       two sets; until P3.4 ships one, gate 3 does not hold.</li>
 * </ol>
 * Every gate also refuses while {@value #SUPERSEDED_BY_CHECK} is missing (linda's ruling 2026-11-21, item c): that CHECK
 * waits on D2's data (vectors-068-19) and lands on a later boot, and the switch must not run without it.
 */
public final class CompletionCheck {

    /** The CHECK whose changeset waits on the data D2's repair UPDATE could violate. */
    public static final String SUPERSEDED_BY_CHECK = "catalog_collections_superseded_by_id_check";

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final long VIOLATOR_TIMEOUT_MS = 30_000L;

    private CompletionCheck() { /* static */ }

    /** How a label stands. */
    public enum Status { COMPLETE, NOT_APPLICABLE, INCOMPLETE }

    /** One label of the read set and why it stands as it does ({@code why} is empty for a complete one). */
    public record LabelState(String label, String table, Status status, String why) {
        public boolean satisfied() {
            return status != Status.INCOMPLETE;
        }
    }

    /** One listed CHECK on its table: present, and validated. */
    public record CheckState(String table, String constraint, boolean present, boolean validated) { }

    /**
     * Rows that still violate a table's CHECK once its stamp move is done. {@code orphans}: a non-empty name that names no
     * registry row in the row's tenant (the stamp cannot give it an id). {@code unstamped}: rows the stamp has not reached
     * (the registry row exists, or the CHECK is the NOT NULL guard). {@code counted} is false when the count timed out or
     * the read failed. The counts are what the engine's admin role can see.
     */
    public record Violators(String table, String constraint, long orphans, long unstamped, boolean counted) { }

    /** The labels each gate reads. A later release adds its labels here (and to the test that pins the set). */
    public record ReadSet(List<String> gate1Labels, List<String> gate2Labels) {
        public ReadSet {
            gate1Labels = List.copyOf(gate1Labels);
            gate2Labels = List.copyOf(gate2Labels);
        }

        /** P3.1's read set: one stamp label per table that carries a CHECK. Gate 2 adds nothing until P3.3c. */
        public static ReadSet p31() {
            return new ReadSet(StampLabels.all(), List.of());
        }
    }

    /**
     * What P3.4 supplies for gate 3: the collections the planner would index on this install (live, not superseded, enough
     * rows, not backed off, within the cap ranking), the ones with a VALID id-predicate index, and the ones whose id index is
     * INVALID. Keys are whatever identifies a collection to the caller; the check only compares them.
     */
    public record IdIndexCoverage(Set<String> planned, Set<String> validIdIndexed, Set<String> invalidIdIndexed) {
        public IdIndexCoverage {
            planned = Set.copyOf(planned);
            validIdIndexed = Set.copyOf(validIdIndexed);
            invalidIdIndexed = Set.copyOf(invalidIdIndexed);
        }

        /** Planned collections with no VALID id index: missing, or INVALID. */
        public Set<String> uncovered() {
            Set<String> out = new TreeSet<>(planned);
            out.removeAll(validIdIndexed);
            return out;
        }
    }

    /** Where gate 3 reads P3.4's state. */
    @FunctionalInterface
    public interface Gate3Source {
        /** Empty when no id-predicate index reader exists in this release: gate 3 then does not hold. */
        Optional<IdIndexCoverage> read(DSLContext ctx);

        /** This release builds no id-predicate index, so gate 3 cannot hold. */
        Gate3Source NOT_INSTALLED = ctx -> Optional.empty();
    }

    /** Counts the rows that still violate a table's CHECK. */
    @FunctionalInterface
    interface ViolatorCounter {
        Violators count(DSLContext ctx, P31Checks.Row row);
    }

    /** The counter a run uses: one bounded scan per table, only asked for in the state that needs it. */
    static final ViolatorCounter DIRECT = CompletionCheck::countViolators;

    /**
     * What one evaluation found.
     *
     * @param gate1                   every label of gate 1 is satisfied and {@value #SUPERSEDED_BY_CHECK} exists
     * @param gate2                   gate 1 and every label of gate 2
     * @param gate3                   gate 2 and P3.4 complete (S-2)
     * @param labels                  every label read, in read-set order
     * @param checks                  every listed CHECK of a table that exists
     * @param supersededByCheckPresent the CHECK of item (c)
     * @param supersededByWaiting     while it is missing: the registry rows its changeset waits on (a tombstone with a blank
     *                                model_version and no superseded_by_id), the count nx doctor shows; 0 otherwise
     * @param violators               tables whose stamp is done and whose CHECK is still NOT VALID, with the rows that violate it
     * @param gate3Uncovered          planned collections with no VALID id index, when gate 3 was asked
     * @param gate3Why                why gate 3 does not hold, or empty
     */
    public record Result(boolean gate1, boolean gate2, boolean gate3, List<LabelState> labels, List<CheckState> checks,
                         boolean supersededByCheckPresent, long supersededByWaiting, List<Violators> violators,
                         List<String> gate3Uncovered, String gate3Why) {

        public List<LabelState> incomplete() {
            return labels.stream().filter(l -> !l.satisfied()).toList();
        }

        public long labelsSatisfied() {
            return labels.stream().filter(LabelState::satisfied).count();
        }
    }

    /** {@link #evaluate(DSLContext, ReadSet, Gate3Source, ViolatorCounter)} for P3.1's read set, gate 3 not installed. */
    public static Result evaluate(DSLContext ctx) {
        return evaluate(ctx, ReadSet.p31(), Gate3Source.NOT_INSTALLED, DIRECT);
    }

    /**
     * Reads the registry, the catalog and the CHECK constraints on {@code ctx} (the admin connection) and returns the state of
     * the three gates. Writes nothing.
     */
    static Result evaluate(DSLContext ctx, ReadSet readSet, Gate3Source gate3, ViolatorCounter counter) {
        Objects.requireNonNull(readSet, "readSet");
        Objects.requireNonNull(gate3, "gate3");
        Objects.requireNonNull(counter, "counter");
        Map<String, Move> moves = readMoves(ctx);
        Map<String, List<P31Checks.Row>> checksByTable = P31Checks.checksByTable();
        Set<String> tables = tablesPresent(ctx);
        Map<String, Boolean> checkValidated = readChecks(ctx, checksByTable, tables);

        List<CheckState> checks = new ArrayList<>();
        for (var e : checksByTable.entrySet()) {
            if (!tables.contains(e.getKey())) {
                continue;
            }
            for (P31Checks.Row row : e.getValue()) {
                Boolean validated = checkValidated.get(row.constraint());
                checks.add(new CheckState(row.table(), row.constraint(), validated != null, Boolean.TRUE.equals(validated)));
            }
        }
        boolean supersededByPresent = checkValidated.containsKey(SUPERSEDED_BY_CHECK);
        long waiting = supersededByPresent ? 0L : supersededByWaiting(ctx, tables);

        List<Violators> violators = new ArrayList<>();
        Set<String> labelsRead = new LinkedHashSet<>(readSet.gate1Labels());
        labelsRead.addAll(readSet.gate2Labels());
        Map<String, LabelState> states = new LinkedHashMap<>();
        for (String label : labelsRead) {
            states.put(label, labelState(ctx, label, moves, checksByTable, tables, checkValidated, counter, violators));
        }

        boolean gate1 = supersededByPresent
            && readSet.gate1Labels().stream().allMatch(l -> states.get(l).satisfied());
        boolean gate2 = gate1 && readSet.gate2Labels().stream().allMatch(l -> states.get(l).satisfied());

        boolean gate3Holds = false;
        List<String> uncovered = List.of();
        String why = "";
        if (!gate2) {
            why = "gate 2 does not hold";
        } else {
            Optional<IdIndexCoverage> coverage = gate3.read(ctx);
            if (coverage.isEmpty()) {
                why = "this release builds no id-predicate index (P3.4), so no collection has a valid one";
            } else {
                uncovered = new ArrayList<>(coverage.get().uncovered());
                gate3Holds = uncovered.isEmpty();
                if (!gate3Holds) {
                    why = uncovered.size() + " collection(s) the planner would index have no VALID id-predicate index ("
                        + coverage.get().invalidIdIndexed().stream().filter(uncovered::contains).count() + " INVALID)";
                }
            }
        }
        return new Result(gate1, gate2, gate3Holds, List.copyOf(states.values()), List.copyOf(checks), supersededByPresent,
            waiting, List.copyOf(violators), List.copyOf(uncovered), why);
    }

    // ---- one label ---------------------------------------------------------------------------------------------------

    private static LabelState labelState(DSLContext ctx, String label, Map<String, Move> moves,
                                         Map<String, List<P31Checks.Row>> checksByTable, Set<String> tables,
                                         Map<String, Boolean> checkValidated, ViolatorCounter counter,
                                         List<Violators> violators) {
        String table = StampLabels.tableOf(label).orElse(null);
        Move row = moves.get(label);

        boolean done = complete(ctx, label, moves, new HashSet<>());
        StampLabels.Applicability only = StampLabels.NOT_APPLICABLE_WHEN_ABSENT.get(label);
        if (!done && only != null && !only.holds(ctx, tables) && (row == null || row.failed())) {
            // The queue's label with no row (a fresh install: aspects-005 dropped the table) or a failed one (the column
            // vectors-068-18 adds is missing on an install whose kept table another role owns): there is nothing to stamp.
            return new LabelState(label, table, Status.NOT_APPLICABLE, "n/a: " + only.table()
                + (tables.contains(only.table()) ? " has no " + only.column() + " column" : " is absent"));
        }
        if (!done) {
            return new LabelState(label, table, Status.INCOMPLETE, row == null ? "no row in nexus.data_moves"
                : (row.failed() ? "failed, and its driving table exists" : "open"));
        }
        if (table == null || !checksByTable.containsKey(table)) {
            return new LabelState(label, table, Status.COMPLETE, "");
        }
        // The move is done: its table's CHECKs must be there and validated, or a VALIDATE is waiting on rows. Every NOT VALID CHECK
        // of the table is counted, not the first: on a table with two name columns the orphan may sit behind the second.
        List<Violators> waiting = new ArrayList<>();
        for (P31Checks.Row check : checksByTable.get(table)) {
            Boolean validated = checkValidated.get(check.constraint());
            if (validated == null) {
                if (!tables.contains(table)) {
                    return new LabelState(label, table, Status.COMPLETE, "");
                }
                return new LabelState(label, table, Status.INCOMPLETE, "CHECK " + check.constraint() + " is missing");
            }
            if (!validated) {
                waiting.add(counter.count(ctx, check));
            }
        }
        if (waiting.isEmpty()) {
            return new LabelState(label, table, Status.COMPLETE, "");
        }
        violators.addAll(waiting);
        boolean counted = waiting.stream().allMatch(Violators::counted);
        return new LabelState(label, table, Status.INCOMPLETE, "CHECK "
            + waiting.stream().map(Violators::constraint).collect(java.util.stream.Collectors.joining(", "))
            + " is NOT VALID: " + (counted ? waiting.stream().mapToLong(Violators::orphans).sum() + " orphan row(s), "
                + waiting.stream().mapToLong(Violators::unstamped).sum() + " unstamped"
                : "the violating rows could not be counted"));
    }

    /** A move row, as far as the check reads it. */
    private record Move(String label, boolean completed, boolean failed, String schema, String table, String supersedes) { }

    private static Map<String, Move> readMoves(DSLContext ctx) {
        Field<String> label = DSL.field(DSL.name("label"), String.class);
        Field<OffsetDateTime> completedAt = DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
        Field<OffsetDateTime> failedAt = DSL.field(DSL.name("failed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
        Field<String> schema = DSL.field(DSL.name("driving_schema"), String.class);
        Field<String> table = DSL.field(DSL.name("driving_table"), String.class);
        Field<String> supersedes = DSL.field(DSL.name("supersedes"), String.class);
        Map<String, Move> out = new HashMap<>();
        for (var r : ctx.select(label, completedAt, failedAt, schema, table, supersedes).from(MOVES).fetch()) {
            out.put(r.value1(), new Move(r.value1(), r.value2() != null, r.value3() != null, r.value4(), r.value5(),
                r.value6()));
        }
        return out;
    }

    /** Complete: {@code completed_at}, or a completed superseding move, or failed with a driving table that no longer exists. */
    private static boolean complete(DSLContext ctx, String label, Map<String, Move> moves, Set<String> seen) {
        Move m = moves.get(label);
        if (m == null || !seen.add(label)) {
            return false;
        }
        if (m.completed()) {
            return true;
        }
        for (Move other : moves.values()) {
            if (label.equals(other.supersedes()) && complete(ctx, other.label(), moves, seen)) {
                return true;
            }
        }
        // to_regclass of the driving table is NULL (the table is gone): the move cannot ever run, and a table that is gone
        // has nothing to stamp (RDR-246: how aspect_extraction_queue ends, 42P01 is permanent).
        return m.failed() && !relationExists(ctx, m.schema(), m.table());
    }

    private static boolean relationExists(DSLContext ctx, String schema, String table) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return ctx.fetchExists(DSL.selectOne().from(c)
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table)));
    }

    // ---- catalog reads -----------------------------------------------------------------------------------------------

    private static Set<String> tablesPresent(DSLContext ctx) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);
        return new HashSet<>(ctx.select(relname).from(c)
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relkind"), String.class).in("r", "p"))
            .fetch(relname));
    }

    /** constraint name to {@code convalidated}, for the listed CHECKs found on their own (parent) table. */
    private static Map<String, Boolean> readChecks(DSLContext ctx, Map<String, List<P31Checks.Row>> byTable, Set<String> tables) {
        Set<String> names = new HashSet<>();
        Set<String> owners = new HashSet<>();
        for (var e : byTable.entrySet()) {
            if (tables.contains(e.getKey())) {
                owners.add(e.getKey());
                e.getValue().forEach(r -> names.add(r.constraint()));
            }
        }
        Map<String, Boolean> out = new HashMap<>();
        if (names.isEmpty()) {
            return out;
        }
        var con = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con");
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("cls");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("ns");
        Field<String> conname = DSL.field(DSL.name("con", "conname"), String.class);
        Field<String> relname = DSL.field(DSL.name("cls", "relname"), String.class);
        Field<Boolean> validated = DSL.field(DSL.name("con", "convalidated"), Boolean.class);
        for (var r : ctx.select(conname, relname, validated).from(con)
                .join(cls).on(DSL.field(DSL.name("cls", "oid")).eq(DSL.field(DSL.name("con", "conrelid"))))
                .join(ns).on(DSL.field(DSL.name("ns", "oid")).eq(DSL.field(DSL.name("cls", "relnamespace"))))
                .where(DSL.field(DSL.name("ns", "nspname"), String.class).eq("nexus"))
                .and(conname.in(names)).and(relname.in(owners)).fetch()) {
            // the constraint of its own table only: a leaf's inherited copy has another relname
            for (var e : byTable.entrySet()) {
                if (e.getKey().equals(r.value2()) && e.getValue().stream().anyMatch(x -> x.constraint().equals(r.value1()))) {
                    out.put(r.value1(), r.value3());
                }
            }
        }
        return out;
    }

    static boolean columnExists(DSLContext ctx, String table, String column) {
        var a = DSL.table(DSL.name("pg_catalog", "pg_attribute")).as("a");
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return ctx.fetchExists(DSL.selectOne().from(a)
            .join(c).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("a", "attrelid"))))
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
            .and(DSL.field(DSL.name("a", "attname"), String.class).eq(column))
            .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse()));
    }

    /** Registry rows vectors-068-19's precondition waits on, counted the way the precondition counts them. */
    private static long supersededByWaiting(DSLContext ctx, Set<String> tables) {
        if (!tables.contains("catalog_collections") || !columnExists(ctx, "catalog_collections", "superseded_by_id")) {
            return 0L;
        }
        Table<?> c = DSL.table(DSL.name("nexus", "catalog_collections"));
        Field<String> supersededBy = DSL.field(DSL.name("superseded_by"), String.class);
        Field<Long> supersededById = DSL.field(DSL.name("superseded_by_id"), Long.class);
        Field<String> modelVersion = DSL.field(DSL.name("model_version"), String.class);
        return ctx.fetchCount(c, supersededBy.ne("").and(supersededById.isNull()).and(modelVersion.eq("")));
    }

    // ---- the rows that keep a CHECK NOT VALID -------------------------------------------------------------------------

    private static Violators countViolators(DSLContext ctx, P31Checks.Row row) {
        try {
            return ctx.transactionResult(cfg -> {
                DSLContext tx = cfg.dsl();
                // PgSession.setLocal binds the matching network timeout with the statement bound (nexus-u9zkn)
                PgSession.setLocal(tx, "statement_timeout", Long.toString(VIOLATOR_TIMEOUT_MS));
                return count(tx, row);
            });
        } catch (DataAccessException e) {
            return new Violators(row.table(), row.constraint(), 0L, 0L, false);
        } finally {
            // the session outlives this scan (CompletionBootWork keeps it between samples): its short read bound does not
            try {
                ctx.connection(c -> PgSession.restoreNetworkTimeout(c, 0));
            } catch (DataAccessException e) {
                // a connection that cannot be reached fails its next read on its own
            }
        }
    }

    private static Violators count(DSLContext tx, P31Checks.Row row) {
        Table<?> t = DSL.table(DSL.name("nexus", row.table())).as("t");
        Field<Long> id = DSL.field(DSL.name("t", row.idColumn()), Long.class);
        if ("NOT_NULL".equals(row.kind())) {
            long n = tx.fetchCount(t, id.isNull());
            return new Violators(row.table(), row.constraint(), 0L, n, true);
        }
        Field<String> name = DSL.field(DSL.name("t", row.nameColumn()), String.class);
        if ("SUPERSEDED".equals(row.kind())) {
            // a tombstone that names no registry row takes its own id (R7): never an orphan, always still to stamp
            return new Violators(row.table(), row.constraint(), 0L, tx.fetchCount(t, name.ne("").and(id.isNull())), true);
        }
        Condition violating = DSL.nullif(name, "").isNotNull().and(id.isNull());
        Table<?> r = DSL.table(DSL.name("nexus", "catalog_collections")).as("r");
        Field<String> registryName = DSL.field(DSL.name("r", "name"), String.class);
        Condition join = registryName.eq(name);
        if (columnExists(tx, row.table(), "tenant_id")) {
            join = join.and(DSL.field(DSL.name("r", "tenant_id"), String.class)
                .eq(DSL.field(DSL.name("t", "tenant_id"), String.class)));
        }
        var rec = tx.select(
                DSL.count().filterWhere(registryName.isNull()),
                DSL.count().filterWhere(registryName.isNotNull()))
            .from(t).leftJoin(r).on(join).where(violating).fetchOne();
        return new Violators(row.table(), row.constraint(), rec.value1(), rec.value2(), true);
    }
}
