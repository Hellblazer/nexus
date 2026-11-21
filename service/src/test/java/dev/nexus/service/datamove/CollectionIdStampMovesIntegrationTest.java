// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.P31TestSql;
import dev.nexus.service.PgContainerHelper;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

/**
 * RDR-229 P3.1 stage 3 (nexus-p2m1x.17): the per-table stamp moves, run for real. The base walks the REAL changelog as the production
 * migrating role on the pg_background image, so the moves are registered by the walk itself, and the REAL worker runs them with its
 * production failure handler and finisher. The base empties the registry before each scenario; {@link #captureRegistrations} reads the
 * rows the walk wrote and {@link #register} puts one back unchanged.
 *
 * <p>Per table (the registry, whose rule is R7, is {@link CollectionIdTombstoneStampMoveIntegrationTest}):
 * <ul>
 *   <li>the move stamps the rows whose name is set and whose id is NULL, with the id of the registry row of the same tenant and name,
 *       leaves a stamped row and a {@code ''} row unwritten (same xmin), and its verification then finds nothing, per tenant and table-wide;</li>
 *   <li>on a table whose name has no registry foreign key, an orphan name (no registry row) keeps its NULL twin, is not rewritten, and is not a
 *       verification failure: the move walks past it (a source that resolves beside a target that does not is stamped on its own column);</li>
 *   <li>a run killed after some chunks committed resumes at the next one, stamps the rest, and does not write a row the first run already stamped;</li>
 *   <li>the move completes, which validates the table's CHECKs (the on_complete), and every listed constraint is then validated.</li>
 * </ul>
 * The upgrade boot registers every move and rewrites no row of a table that grows.
 */
class CollectionIdStampMovesIntegrationTest extends DataMoveIntegrationBase {

    private static final Field<String> TENANT = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> FAILURE = DSL.field(DSL.name("failure"), String.class);
    private static final Field<java.time.OffsetDateTime> FAILED_AT = DSL.field(DSL.name("failed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<String> CHANGESET_ID = DSL.field(DSL.name("id"), String.class);
    private static final Field<String> FILENAME = DSL.field(DSL.name("filename"), String.class);

    /** The registry rows the walk wrote, by table, exactly as written. */
    private static final Map<String, Map<String, Object>> registered = new LinkedHashMap<>();
    private static int movesAtBoot;

    private final List<String> tenantsUsed = new ArrayList<>();

    @BeforeAll
    void captureRegistrations() throws Exception {
        movesAtBoot = ctx().fetchCount(table("nexus", "data_moves"));
        for (var spec : StampMoveSpecs.SPECS) {
            Record row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(spec.label())).fetchOne();
            registered.put(spec.table(), row == null ? null : new LinkedHashMap<>(row.intoMap()));
        }
    }

    @AfterEach
    void removeSeeds() {
        for (String t : tenantsUsed) {
            StampMoveSeeds.clean(ctx(), t);
            for (var spec : StampMoveSpecs.SPECS) {
                for (String run : new ArrayList<>(activeRuns(spec.label()))) {
                    ctx().select(DSL.function(DSL.name("dml_utils", "archive_migration_run"), SQLDataType.BIGINT, DSL.val(run))).fetch();
                }
            }
        }
        tenantsUsed.clear();
    }

    /** Every table but the registry, which has its own class. */
    Stream<String> tables() {
        return StampMoveSpecs.SPECS.stream().map(StampMoveSpecs.Spec::table).filter(t -> !t.equals("catalog_collections"));
    }

    Stream<String> freeNameTables() {
        return tables().filter(StampMoveSeeds.FREE_NAME_TABLES::contains);
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private String tenantFor(String table, String scenario) {
        String t = "p31-" + scenario + "-" + table.replace('_', '-');
        tenantsUsed.add(t);
        return t;
    }

    private Map<String, Object> registration(String table) {
        Map<String, Object> row = registered.get(table);
        assertThat(row).as("the walk registers the move rdr229-stamp-" + table).isNotNull();
        return row;
    }

    /** Puts the registration back as the walk wrote it, or with {@code changes} (a throttle for a test that has to see a chunk at a time). */
    private void register(String table, Map<String, Object> changes) {
        Map<String, Object> row = new LinkedHashMap<>(registration(table));
        row.putAll(changes);
        List<Field<?>> columns = new ArrayList<>();
        List<Object> values = new ArrayList<>();
        row.forEach((k, v) -> {
            columns.add(DSL.field(DSL.name(k)));
            values.add(v);
        });
        ctx().insertInto(table("nexus", "data_moves")).columns(columns).values(values).execute();
    }

    private DataMoveWorker productionWorker(Integer sleepOverrideMs) {
        return worker(installed(), sleepOverrideMs, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC()),
            new VerifyingFinisher());
    }

    /** How many rows the move's verification returns for {@code tenant} (null: table-wide), run on the superuser connection that reads every row. */
    private long verificationRows(String table, String tenant) {
        String verification = registration(table).get("verification").toString();
        long[] n = new long[1];
        ctx().connection(c -> n[0] = MoveSql.countRows(c, verification, tenant, VerifyingFinisher.COUNT_CAP));
        return n[0];
    }

    /**
     * The table-wide run of the verification as the worker makes it: on the migrating role's own connection (not the superuser's, which
     * reads every row whatever the policies say), with no tenant. It sees the old-shape rows only where that role may read every tenant's rows
     * of the driving table and of the registry the verification joins, which is what the owner-read policies are for.
     */
    private long adminTableWideRows(String table) throws Exception {
        String verification = registration(table).get("verification").toString();
        try (var c = adminPool.getConnection()) {
            return MoveSql.countRows(c, verification, null, VerifyingFinisher.COUNT_CAP);
        }
    }

    private List<Long> expectedIds(String table, String tenant, String key, int n) {
        var ids = new ArrayList<Long>();
        ids.add(StampMoveSeeds.id(ctx(), tenant, key));
        if (StampMoveSpecs.idColumns(table).size() == 2) ids.add(StampMoveSeeds.id(ctx(), tenant, StampMoveSeeds.target(tenant, n)));
        return ids;
    }

    /** Waits until the run has committed a chunk that stamped one of {@code keys}: the kill then lands with the move part way through its rows. */
    private void awaitAnOldRowStamped(String table, String tenant, List<String> keys) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofMinutes(2).toNanos();
        while (System.nanoTime() < deadline) {
            var now = StampMoveSeeds.rows(ctx(), table, tenant);
            if (keys.stream().anyMatch(k -> now.get(k).ids().values().stream().allMatch(java.util.Objects::nonNull))) return;
            Thread.sleep(20);
        }
        throw new AssertionError("the run stamped no row of " + table + " in two minutes");
    }

    private String failure(String table) {
        return ctx().select(FAILURE).from(table("nexus", "data_moves")).where(LABEL.eq(StampMoveSpecs.LABEL_PREFIX + table)).fetchOne(FAILURE);
    }

    // ── 1. the walk registers the moves, and writes no row ──────────────────────────────────────────────────

    @Test
    void theWalkRegistersExactlyOneOpenMovePerCheckGuardedTable_inTheOrderTheWorkerTakesThem() {
        // the other moves the product changelog registers at boot (RDR-229 D2's repair, the re-own, P2.10's residue move) are read from the master, not counted here
        var shipped = ShippedMoveRegistrations.labels();
        var otherShipped = shipped.stream().filter(l -> !l.startsWith(StampMoveSpecs.LABEL_PREFIX)).toList();
        assertThat(otherShipped).as("non-vacuity: develop's registered moves are in the walk").isNotEmpty();
        assertThat(shipped.stream().filter(l -> l.startsWith(StampMoveSpecs.LABEL_PREFIX)).toList())
            .as("the stamp moves the changelog registers are the table list's, no more").containsExactlyInAnyOrderElementsOf(
                StampMoveSpecs.SPECS.stream().map(StampMoveSpecs.Spec::label).toList());
        assertThat(movesAtBoot).as("the walk registers the stamp moves and the other shipped moves, no more")
            .isEqualTo(StampMoveSpecs.SPECS.size() + otherShipped.size());
        var oldestFirst = registered.values().stream()
            .sorted(java.util.Comparator.comparing((Map<String, Object> r) -> (java.time.OffsetDateTime) r.get("registered_at"))
                .thenComparing(r -> r.get("label").toString()))
            .map(r -> r.get("label").toString()).toList();
        assertThat(oldestFirst).as("registered_at orders the worker's pass: one changeset per move, so one transaction time each")
            .containsExactlyElementsOf(StampMoveSpecs.SPECS.stream().map(StampMoveSpecs.Spec::label).toList());
        for (var spec : StampMoveSpecs.SPECS) {
            var row = registration(spec.table());
            assertThat(row).containsEntry("driving_schema", "nexus").containsEntry("driving_table", spec.table())
                .containsEntry("completed_at", null).containsEntry("failed_at", null).containsEntry("paused_at", null)
                .containsEntry("supersedes", null);
            assertThat(row.get("verify_statement_timeout_ms")).as("the default bound on the first verification pass").isEqualTo(60_000);
        }
    }

    @Test
    void theUpgradeBootRegistersTheMovesAndRewritesNoRowOfATableThatGrows() {
        // rows of the tables that grow with user data, as the census found them: a name and no id
        String tenant = tenantFor("walk", "boot");
        Map<String, Map<String, StampMoveSeeds.RowState>> before = new LinkedHashMap<>();
        for (String table : List.of("chunks", "catalog_document_chunks", "catalog_documents", "topic_assignments", "chunk_orphaned_at")) {
            StampMoveSeeds.seed(ctx(), table, tenant + "-" + table, StampMoveSeeds.Plan.clean(2));
            tenantsUsed.add(tenant + "-" + table);
            before.put(table, StampMoveSeeds.rows(ctx(), table, tenant + "-" + table));
        }
        // as on an upgrade: the walk meets populated tables and runs the changesets it has not run
        ctx().deleteFrom(table("nexus", "data_moves")).where(LABEL.like(StampMoveSpecs.LABEL_PREFIX + "%")).execute();
        int recordedBefore = ctx().fetchCount(table("public", "databasechangelog"), FILENAME.eq(StampMoveSpecs.CHANGELOG));
        assertThat(recordedBefore).as("non-vacuity: the changelog's changesets were recorded by the first walk").isPositive();
        ctx().deleteFrom(table("public", "databasechangelog")).where(FILENAME.eq(StampMoveSpecs.CHANGELOG)).execute();

        installed();

        assertThat(ctx().fetchCount(table("public", "databasechangelog"), FILENAME.eq(StampMoveSpecs.CHANGELOG)))
            .as("the walk ran every changeset again").isEqualTo(recordedBefore);
        assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL.like(StampMoveSpecs.LABEL_PREFIX + "%").and(COMPLETED_AT.isNull())))
            .as("one open move per table, registered by the walk and run by nothing").isEqualTo(StampMoveSpecs.SPECS.size());
        before.forEach((table, rows) -> {
            var after = StampMoveSeeds.rows(ctx(), table, tenant + "-" + table);
            assertThat(after).as("%s: no row written (xmin is the last writer's transaction) and no id filled at boot", table).isEqualTo(rows);
        });
    }

    // ── 2. the move, table by table ─────────────────────────────────────────────────────────────────────────

    @ParameterizedTest(name = "{0}")
    @MethodSource("tables")
    void stampsTheOldRows_leavesAStampedAndAnEmptyRowAlone_verifiesCleanAndCompletes(String table) throws Exception {
        String tenant = tenantFor(table, "stamp");
        boolean free = StampMoveSeeds.FREE_NAME_TABLES.contains(table);
        var seeded = StampMoveSeeds.seed(ctx(), table, tenant, new StampMoveSeeds.Plan(3, true, free, false));
        var before = StampMoveSeeds.rows(ctx(), table, tenant);
        assertThat(verificationRows(table, tenant)).as("non-vacuity: the three old rows are what the verification looks for").isEqualTo(3);
        assertThat(adminTableWideRows(table)).as("non-vacuity: and the migrating role's table-wide run sees them, so the owner-read policies reach the table and the registry")
            .isEqualTo(3);
        register(table, Map.of());

        assertThat(productionWorker(0).runCycle()).as("the one registered move is worked").isEqualTo(1);

        assertThat(completedAt(StampMoveSpecs.LABEL_PREFIX + table)).as("it completes: verified, and on_complete ran (failure: %s)", failure(table)).isNotNull();
        var after = StampMoveSeeds.rows(ctx(), table, tenant);
        for (int n = 0; n < 3; n++) {
            String key = seeded.old().get(n);
            assertThat(after.get(key).ids().values()).as("%s row %d: its id, from the registry row of its tenant and name", table, n)
                .containsExactlyElementsOf(expectedIds(table, tenant, key, n));
            assertThat(after.get(key).xmin()).as("%s row %d was written", table, n).isNotEqualTo(before.get(key).xmin());
        }
        assertThat(after.get(seeded.stamped())).as("%s: a row the dual write stamped is not written again", table).isEqualTo(before.get(seeded.stamped()));
        if (free) {
            assertThat(after.get("")).as("%s: a '' name is unset, needs no id and is not written", table).isEqualTo(before.get(""));
            assertThat(after.get("").ids().values()).containsOnlyNulls();
        }
        assertThat(verificationRows(table, tenant)).as("verify, per tenant").isZero();
        assertThat(verificationRows(table, null)).as("verify, table-wide").isZero();
        assertThat(adminTableWideRows(table)).as("verify, table-wide, as the migrating role").isZero();
        for (String constraint : StampMoveSpecs.constraintsByTable().get(table)) {
            assertThat(PgCatalogProbes.constraintValidated(ctx(), constraint)).as("on_complete validated %s", constraint).isTrue();
        }
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("freeNameTables")
    void anOrphanRowKeepsItsNullTwin_isNotRewritten_andIsNotAVerificationFailure(String table) {
        String tenant = tenantFor(table, "orphan");
        var seeded = StampMoveSeeds.seed(ctx(), table, tenant, new StampMoveSeeds.Plan(2, true, false, true));
        var before = StampMoveSeeds.rows(ctx(), table, tenant);
        assertThat(verificationRows(table, tenant)).as("non-vacuity: the two old rows are what the verification looks for, the orphan row is not").isEqualTo(2);
        register(table, Map.of());

        assertThat(productionWorker(0).runCycle()).isEqualTo(1);

        var after = StampMoveSeeds.rows(ctx(), table, tenant);
        for (int n = 0; n < 2; n++) {
            assertThat(after.get(seeded.old().get(n)).ids().values()).as("%s row %d is stamped", table, n)
                .containsExactlyElementsOf(expectedIds(table, tenant, seeded.old().get(n), n));
        }
        // a row with any name no registry row carries is left whole: an UPDATE of it would be refused by that name's own CHECK (name set, id NULL)
        // whatever the update sets, so on chash_remap and reembed_jobs the row whose source resolves beside an orphan target is not stamped on its source either
        assertThat(after.get(seeded.orphan())).as("%s: the orphan row is not rewritten, and its twin stays NULL", table).isEqualTo(before.get(seeded.orphan()));
        assertThat(after.get(seeded.orphan()).ids().values()).containsOnlyNulls();
        assertThat(verificationRows(table, tenant)).as("what the verification looks for is gone, the orphan is not it").isZero();
        // the move did not fail in its walk or its verification; if it ends with a failure at all, it is the VALIDATE of the orphan's CHECK in on_complete,
        // which the orphan path (counted and reported, the VALIDATE waits) takes over
        String failure = failure(table);
        assertThat(failure == null || failure.contains("on_complete")).as("%s: failure is %s", table, failure).isTrue();
        assertThat(failure == null || !failure.contains("old-shape rows")).isTrue();
    }

    /**
     * Seam 1 (tamsin, 2026-11-21): an orphan row (a name with no registry row in its tenant) must not fail the move. Its on_complete validates a
     * table's CHECKs only while the table has no orphan row, so the move COMPLETES with the CHECKs NOT VALID, the completion check says the VALIDATE
     * waits and counts the orphan, and once the operator deletes the orphan and validates, the label completes.
     */
    @ParameterizedTest(name = "{0}")
    @MethodSource("freeNameTables")
    void anOrphanLetsTheMoveCompleteWithItsChecksNotValid_theLabelWaitsAndCountsIt_thenCompletesOnceTheOperatorValidates(String table) throws Exception {
        String tenant = tenantFor(table, "orphan-wait");
        String label = StampMoveSpecs.LABEL_PREFIX + table;
        var seeded = StampMoveSeeds.seed(ctx(), table, tenant, new StampMoveSeeds.Plan(2, true, false, true));
        register(table, Map.of());

        assertThat(productionWorker(0).runCycle()).isEqualTo(1);

        assertThat(completedAt(label)).as("%s: the orphan does not fail the move (failure: %s)", table, failure(table)).isNotNull();
        assertThat(failure(table)).as("%s: no failure is recorded", table).isNull();
        var after = StampMoveSeeds.rows(ctx(), table, tenant);
        for (String key : seeded.old()) {
            assertThat(after.get(key).ids().values()).as("%s: non-vacuity, the resolvable rows are stamped", table).doesNotContainNull();
        }
        for (String constraint : StampMoveSpecs.constraintsByTable().get(table)) {
            assertThat(PgCatalogProbes.constraintValidated(ctx(), constraint)).as("%s: the VALIDATE waits while an orphan is there", constraint).isFalse();
        }
        var readSet = new CompletionCheck.ReadSet(List.of(label), List.of());
        var waiting = CompletionCheck.evaluate(ctx(), readSet, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
        var state = waiting.labels().get(0);
        assertThat(state.status()).as("%s: the label shows VALIDATE waits", table).isEqualTo(CompletionCheck.Status.INCOMPLETE);
        assertThat(state.why()).contains("is NOT VALID");
        assertThat(waiting.violators().stream().mapToLong(CompletionCheck.Violators::orphans).sum())
            .as("%s: the check counts exactly the one orphan (the same predicate on_complete guards on)", table).isEqualTo(1);

        // the operator deletes the orphan and validates
        String firstName = StampMoveSpecs.checks().stream().filter(c -> c.table().equals(table) && c.kind().equals("NAME_ID")).findFirst().orElseThrow().nameColumn();
        assertThat(ctx().deleteFrom(table("nexus", table)).where(TENANT.eq(tenant)).and(DSL.field(DSL.name(firstName), String.class).eq(seeded.orphan())).execute()).isEqualTo(1);
        for (String constraint : StampMoveSpecs.constraintsByTable().get(table)) {
            P31TestSql.validate(su, table("nexus", table), constraint);
        }
        var done = CompletionCheck.evaluate(ctx(), readSet, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
        assertThat(done.labels().get(0).status()).as("%s: the label completes once validated", table).isEqualTo(CompletionCheck.Status.COMPLETE);
        assertThat(done.violators()).isEmpty();
    }

    /**
     * Fix round A (critique S1, code review F4): the way back from an orphan. Registering the missing collection stamps nothing (the move is
     * complete and does not run again); nexus.rdr229_restamp registers the move once more, the worker stamps the row that has become
     * resolvable, and the VALIDATE the first run left waiting goes through.
     */
    @ParameterizedTest(name = "{0}")
    @MethodSource("freeNameTables")
    void anOrphanIsStampedAndValidatedByTheRerun_onceItsCollectionIsRegistered(String table) throws Exception {
        String tenant = tenantFor(table, "restamp");
        String label = StampMoveSpecs.LABEL_PREFIX + table;
        var seeded = StampMoveSeeds.seed(ctx(), table, tenant, new StampMoveSeeds.Plan(2, true, false, true));
        register(table, Map.of());
        assertThat(productionWorker(0).runCycle()).isEqualTo(1);
        assertThat(completedAt(label)).as("%s: the first run completes (failure: %s)", table, failure(table)).isNotNull();
        for (String constraint : StampMoveSpecs.constraintsByTable().get(table)) {
            assertThat(PgCatalogProbes.constraintValidated(ctx(), constraint)).as("non-vacuity: %s waits on the orphan", constraint).isFalse();
        }
        String refused = ctx().select(DSL.function(DSL.name("nexus", "rdr229_restamp"), SQLDataType.VARCHAR, DSL.val(table))).fetchOne(0, String.class);
        assertThat(refused).as("%s: the orphan is still there", table).startsWith("not registered:").contains("orphan");

        // the operator registers the collection the orphan names; the stamp does not run again by itself
        // (the orphan name: the row's own on a one-name table, its target's on chash_remap and reembed_jobs, where the source resolves)
        PgContainerHelper.insertCollection(ctx(), tenant, StampMoveSeeds.orphan(tenant, 4));
        assertThat(StampMoveSeeds.rows(ctx(), table, tenant).get(seeded.orphan()).ids().values()).as("%s: registering stamps nothing", table).containsOnlyNulls();
        assertThat(productionWorker(0).runCycle()).as("nothing is open").isZero();

        String answer = ctx().select(DSL.function(DSL.name("nexus", "rdr229_restamp"), SQLDataType.VARCHAR, DSL.val(table))).fetchOne(0, String.class);
        assertThat(answer).isEqualTo("registered: " + label + "-rerun-1");
        assertThat(productionWorker(0).runCycle()).as("the re-run is worked").isEqualTo(1);

        assertThat(completedAt(label + "-rerun-1")).as("%s: the re-run completes (failure: %s)", table, failure(table + "-rerun-1")).isNotNull();
        assertThat(StampMoveSeeds.rows(ctx(), table, tenant).get(seeded.orphan()).ids().values()).as("%s: the row is stamped", table).doesNotContainNull();
        for (String constraint : StampMoveSpecs.constraintsByTable().get(table)) {
            assertThat(PgCatalogProbes.constraintValidated(ctx(), constraint)).as("%s validated by the re-run's on_complete", constraint).isTrue();
        }
        var done = CompletionCheck.evaluate(ctx(), new CompletionCheck.ReadSet(List.of(label), List.of()), CompletionCheck.Gate3Source.NOT_INSTALLED,
            CompletionCheck.DIRECT);
        assertThat(done.labels().get(0).status()).as("%s: the label completes", table).isEqualTo(CompletionCheck.Status.COMPLETE);
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("tables")
    void aRunKilledAfterSomeChunksResumesAtTheNextChunk_andDoesNotWriteWhatItAlreadyStamped(String table) throws Exception {
        String tenant = tenantFor(table, "kill");
        var seeded = StampMoveSeeds.seed(ctx(), table, tenant, StampMoveSeeds.Plan.clean(8));
        // one row a chunk, a pause between chunks: the run is in flight for seconds
        register(table, Map.of("chunk_size", 1));
        int baselineFree = MovePreflight.freeWorkerSlots(ctx());
        var first = worker(installed(), 300);   // the engine that dies: the runs only, as the base's kill test builds it (a production handler would back the move off)
        var cycle = async(first::runCycle);
        awaitAnOldRowStamped(table, tenant, seeded.old());
        int pid = -1;
        for (int i = 0; i < 100 && pid < 0; i++) {
            pid = callPid();
            Thread.sleep(20);
        }
        ctx().select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, DSL.val(pid))).fetch();   // the engine dies: its coordinator goes with it
        cycle.get(2, TimeUnit.MINUTES);
        first.close();
        long drain = System.nanoTime() + Duration.ofMinutes(1).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) < baselineFree && System.nanoTime() < drain) {
            Thread.sleep(50);
        }
        var midway = StampMoveSeeds.rows(ctx(), table, tenant);
        var stamped = new TreeSet<String>();
        for (String key : seeded.old()) {
            if (midway.get(key).ids().values().stream().allMatch(java.util.Objects::nonNull)) stamped.add(key);
        }
        assertThat(stamped).as("%s: some chunks committed before the kill, and not all", table).isNotEmpty().hasSizeLessThan(8);
        assertThat(completedAt(StampMoveSpecs.LABEL_PREFIX + table)).as("the killed run did not finish the move").isNull();

        assertThat(productionWorker(0).runCycle()).as("the next boot works the same move").isEqualTo(1);

        assertThat(completedAt(StampMoveSpecs.LABEL_PREFIX + table)).as("and completes it (failure: %s)", failure(table)).isNotNull();
        var end = StampMoveSeeds.rows(ctx(), table, tenant);
        for (int n = 0; n < 8; n++) {
            String key = seeded.old().get(n);
            assertThat(end.get(key).ids().values()).as("%s row %d", table, n).containsExactlyElementsOf(expectedIds(table, tenant, key, n));
            if (stamped.contains(key)) {
                assertThat(end.get(key).xmin()).as("%s row %d was stamped before the kill and is not written by the resumed run", table, n)
                    .isEqualTo(midway.get(key).xmin());
            }
        }
        assertThat(verificationRows(table, tenant)).isZero();
    }
}
