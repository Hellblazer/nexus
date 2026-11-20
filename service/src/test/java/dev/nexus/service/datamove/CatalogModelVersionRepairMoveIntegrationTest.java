// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.DrivingTableIsolationCheck;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.DriverManager;
import java.time.Clock;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.stream.Collectors;

/**
 * nexus-f3yqd.2.23 (RDR-246 Activation Step 2, the first registered move; RDR-229 Phase 1 decision D2, Sam 2026-11-20):
 * the move {@value #LABEL} sets {@code catalog_collections.model_version} from the collection's conformant name where the
 * column is {@code ''}. These tests run the REAL changelog (the base walks it as the production migrating role) and the REAL
 * worker with its production failure handler and finisher on the pg_background image, against rows seeded the way the
 * census found them. The move is registered by the walk itself; the base empties the registry before each scenario, so
 * {@link #captureRegistration} reads the row the walk wrote and {@link #registerTheMove} puts it back unchanged.
 *
 * <p>Falsifiers, by hand: drop {@code t.model_version = ''} from the template and the "already set" row is rewritten (its
 * xmin moves); loosen the name pattern to {@code ~ '__v[0-9]+$'} and {@code gate-probe-124}-shaped names with a suffix are
 * rewritten; stop filtering by tenant in the verification and a tenant's check reads another's rows; put the INSERT into a
 * changeset that also UPDATEs {@code catalog_collections} and the walk test sees every row's xmin move.
 */
class CatalogModelVersionRepairMoveIntegrationTest extends DataMoveIntegrationBase {

    static final String LABEL = "repair-catalog-collection-model-version";
    private static final String REGISTER_CHANGESET = "vectors-064-1";
    private static final String POLICY_CHANGESET = "vectors-065-1";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final Field<String> CC_TENANT = CATALOG_COLLECTIONS.TENANT_ID;
    private static final Field<String> CC_NAME = CATALOG_COLLECTIONS.NAME;
    private static final Field<String> CC_VERSION = CATALOG_COLLECTIONS.MODEL_VERSION;
    private static final Field<String> XMIN = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
    private static final Field<String> CHANGESET_ID = DSL.field(DSL.name("id"), String.class);
    private static final Field<java.time.OffsetDateTime> FAILED_AT =
        DSL.field(DSL.name("failed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);

    private static final String ACME = "acme";
    private static final String GLOBEX = "globex";
    private static final String ORPHAN = "orphan";   // holds rows and has no service_tokens row: only the probe finds it
    private static final List<String> TENANTS = List.of(ACME, GLOBEX, ORPHAN);

    private static final String A_V1 = "code__1-2__voyage-code-3__v1";
    private static final String A_V3 = "docs__arcaneum-2ad2825c__voyage-context-3__v3";
    private static final String A_UNDERSCORE_OWNER = "knowledge__nexus_rdr__voyage-context-3__v12";
    private static final String A_OWNER_V = "docs__vv__voyage-context-3__v5";
    private static final String A_ALREADY_SET = "docs__1-3__voyage-context-3__v2";
    private static final String NON_CONFORMANT = "gate-probe-124";
    private static final String LEGACY_TWO_SEGMENT = "docs__default";
    private static final String QUARANTINE = "quarantine-code__1-2__voyage-code-3__v1";
    private static final String SUFFIX_NOT_A_VERSION = "docs__1-6__voyage-context-3__v1x";
    private static final String G_ALREADY_SET = "rdr__1-4__voyage-context-3__v4";
    private static final String O_V1 = "docs__1-5__voyage-context-3__v1";

    /** The registry row exactly as the walk wrote it, and how many rows the walk left. */
    private static Map<String, Object> registered;
    private static int registeredRowsAtBoot;
    private static int movesAtBoot;

    @BeforeAll
    void captureRegistration() {
        movesAtBoot = ctx().fetchCount(table("nexus", "data_moves"));
        registeredRowsAtBoot = ctx().fetchCount(table("nexus", "data_moves"), LABEL_FIELD.eq(LABEL));
        Record row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL_FIELD.eq(LABEL)).fetchOne();
        registered = row == null ? null : new LinkedHashMap<>(row.intoMap());
    }

    private static final Field<String> LABEL_FIELD = DSL.field(DSL.name("label"), String.class);

    @BeforeEach
    void registerTheMove() {
        removeSeededRowsAndRuns();
        assertThat(registered).as("the walk registers the move " + LABEL).isNotNull();
        insertRegistration(registered);
    }

    @AfterEach
    void removeSeededRows() {
        removeSeededRowsAndRuns();
    }

    /**
     * The base empties the registry between scenarios but not dml_utils' run records, and every scenario here moves the same
     * label: a run left complete by an earlier scenario would make the next CALL return at once, moving nothing. Each
     * scenario starts from no run of the label (nor of its copy) and no seeded row.
     */
    private void removeSeededRowsAndRuns() {
        ctx().deleteFrom(CATALOG_COLLECTIONS).where(CC_TENANT.in(TENANTS)).execute();
        for (String move : List.of(LABEL, "copy-of-" + LABEL)) {
            for (String run : new ArrayList<>(activeRuns(move))) {
                ctx().select(DSL.function(DSL.name("dml_utils", "archive_migration_run"), SQLDataType.BIGINT, DSL.val(run))).fetch();
            }
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private void insertRegistration(Map<String, Object> row) {
        List<Field<?>> columns = new ArrayList<>();
        List<Object> values = new ArrayList<>();
        row.forEach((k, v) -> {
            columns.add(DSL.field(DSL.name(k)));
            values.add(v);
        });
        ctx().insertInto(table("nexus", "data_moves")).columns(columns).values(values).execute();
    }

    /** A catalog_collections row as the census found them: {@code model_version} {@code ''} unless a version is given. */
    private void seed(String tenant, String name, String modelVersion) {
        PgContainerHelper.insertCollection(ctx(), tenant, name);
        if (!modelVersion.isEmpty()) {
            ctx().update(CATALOG_COLLECTIONS).set(CC_VERSION, modelVersion)
                .where(CC_TENANT.eq(tenant)).and(CC_NAME.eq(name)).execute();
        }
    }

    /** The census's shape: empties on three tenants, a row already set, and names that are not conformant. */
    private void seedCensusShape() {
        seed(ACME, A_V1, "");
        seed(ACME, A_V3, "");
        seed(ACME, A_UNDERSCORE_OWNER, "");
        seed(ACME, A_OWNER_V, "");
        seed(ACME, A_ALREADY_SET, "v7");   // the row's own value, which the name's v2 must not overwrite
        seed(ACME, NON_CONFORMANT, "");
        seed(ACME, LEGACY_TWO_SEGMENT, "");
        seed(ACME, QUARANTINE, "");
        seed(ACME, SUFFIX_NOT_A_VERSION, "");
        seed(GLOBEX, A_V1, "");            // the same name as acme's row, on another tenant
        seed(GLOBEX, G_ALREADY_SET, "v9");
        seed(ORPHAN, O_V1, "");
    }

    private Map<String, String> versions(String tenant) {
        return ctx().select(CC_NAME, CC_VERSION).from(CATALOG_COLLECTIONS).where(CC_TENANT.eq(tenant))
            .fetch().stream().collect(Collectors.toMap(r -> r.get(CC_NAME), r -> r.get(CC_VERSION), (a, b) -> a, TreeMap::new));
    }

    /** {@code tenant/name} to the row's xmin, the transaction that last wrote it: an UPDATE, even to the same value, moves it. */
    private Map<String, String> xmins() {
        return ctx().select(CC_TENANT, CC_NAME, XMIN).from(CATALOG_COLLECTIONS).where(CC_TENANT.in(TENANTS))
            .fetch().stream().collect(Collectors.toMap(r -> r.get(CC_TENANT) + "/" + r.get(CC_NAME), r -> r.get(XMIN),
                (a, b) -> a, TreeMap::new));
    }

    private DataMoveWorker productionWorker() {
        return worker(installed(), null, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC()),
            new VerifyingFinisher());
    }

    private void runToCompletion() {
        assertThat(productionWorker().runCycle()).as("the one registered move is worked").isEqualTo(1);
        assertThat(completedAt(LABEL)).as("and completes").isNotNull();
    }

    // ── 1. the repair ───────────────────────────────────────────────────────────────────────────────────────

    @Test
    void conformantEmptiesCarryTheirNamesVersion_aSetRowIsUnchanged_aNonConformantNameStaysEmpty() {
        seedCensusShape();
        var before = xmins();

        runToCompletion();

        var acme = versions(ACME);
        assertThat(acme.get(A_V1)).isEqualTo("v1");
        assertThat(acme.get(A_V3)).isEqualTo("v3");
        assertThat(acme.get(A_UNDERSCORE_OWNER)).as("an owner with a single underscore").isEqualTo("v12");
        assertThat(acme.get(A_OWNER_V)).as("an owner that starts with v: the suffix is the last segment").isEqualTo("v5");
        assertThat(acme.get(A_ALREADY_SET)).as("a row already set keeps its own value").isEqualTo("v7");
        assertThat(acme.get(NON_CONFORMANT)).as("not conformant: left as found").isEmpty();
        assertThat(acme.get(LEGACY_TWO_SEGMENT)).as("legacy two-segment name: left as found").isEmpty();
        assertThat(acme.get(QUARANTINE)).as("quarantine-prefixed: not conformant under corpus.py's rule, left as found").isEmpty();
        assertThat(acme.get(SUFFIX_NOT_A_VERSION)).as("__v1x is not a version suffix").isEmpty();
        assertThat(versions(GLOBEX)).containsEntry(A_V1, "v1").containsEntry(G_ALREADY_SET, "v9");
        assertThat(versions(ORPHAN)).containsEntry(O_V1, "v1");

        // the move wrote the repaired rows and nothing else: every row it left alone keeps its xmin
        var after = xmins();
        for (String untouched : List.of(ACME + "/" + A_ALREADY_SET, ACME + "/" + NON_CONFORMANT, ACME + "/" + LEGACY_TWO_SEGMENT,
                ACME + "/" + QUARANTINE, ACME + "/" + SUFFIX_NOT_A_VERSION, GLOBEX + "/" + G_ALREADY_SET)) {
            assertThat(after.get(untouched)).as("%s was not written", untouched).isEqualTo(before.get(untouched));
        }
        for (String repaired : List.of(ACME + "/" + A_V1, ACME + "/" + A_V3, GLOBEX + "/" + A_V1, ORPHAN + "/" + O_V1)) {
            assertThat(after.get(repaired)).as("%s was written", repaired).isNotEqualTo(before.get(repaired));
        }

        // reported: what the move leaves is exactly the non-conformant empties, which is the census's "unusable" count
        var leftEmpty = new TreeSet<String>();
        versions(ACME).forEach((name, v) -> {
            if (v.isEmpty()) leftEmpty.add(name);
        });
        assertThat(leftEmpty).containsExactlyInAnyOrder(NON_CONFORMANT, LEGACY_TWO_SEGMENT, QUARANTINE, SUFFIX_NOT_A_VERSION);
    }

    // ── 2. registered once, and the walk writes no catalog_collections row ──────────────────────────────────

    @Test
    void theMoveIsRegisteredExactlyOnce_withTheParametersRdr246Prescribes() {
        assertThat(movesAtBoot).as("the walk registers one move and no other").isEqualTo(1);
        assertThat(registeredRowsAtBoot).isEqualTo(1);
        assertThat(registered.get("label").toString()).doesNotContain("/").isEqualTo(LABEL);
        assertThat(registered).containsEntry("driving_schema", "nexus").containsEntry("driving_table", "catalog_collections")
            .containsEntry("changeset_id", REGISTER_CHANGESET)
            .containsEntry("on_complete", null).containsEntry("supersedes", null)
            .containsEntry("completed_at", null).containsEntry("failed_at", null);
        assertThat(registered.get("template").toString()).contains("<driving_table>").contains("<chunking_clause>");
        assertThat((Integer) registered.get("chunk_size")).isBetween(1, 1_000_000);
        assertThat((Integer) registered.get("statement_timeout_ms")).isPositive();
        // the walk is idempotent: a second walk registers nothing more
        installed();
        assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL_FIELD.eq(LABEL))).isEqualTo(1);
    }

    @Test
    void theBootWalkWritesNoCatalogCollectionsRow() {
        seedCensusShape();
        var before = xmins();
        var valuesBefore = new TreeMap<String, Map<String, String>>();
        TENANTS.forEach(t -> valuesBefore.put(t, versions(t)));
        // as on an upgrade: the walk meets a populated table and runs the two changesets it has not run
        ctx().deleteFrom(table("nexus", "data_moves")).where(LABEL_FIELD.eq(LABEL)).execute();
        ctx().deleteFrom(table("public", "databasechangelog"))
            .where(CHANGESET_ID.in(REGISTER_CHANGESET, POLICY_CHANGESET)).execute();

        installed();

        assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL_FIELD.eq(LABEL)))
            .as("the walk ran the registering changeset: one registry row").isEqualTo(1);
        assertThat(ctx().fetchCount(table("public", "databasechangelog"), CHANGESET_ID.in(REGISTER_CHANGESET, POLICY_CHANGESET)))
            .as("and both changesets are recorded").isEqualTo(2);
        assertThat(xmins()).as("no catalog_collections row was written (xmin is the last writer's transaction)").isEqualTo(before);
        TENANTS.forEach(t -> assertThat(versions(t)).as("values of %s", t).isEqualTo(valuesBefore.get(t)));
        assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL_FIELD.eq(LABEL).and(COMPLETED_AT.isNull())))
            .as("nothing ran at boot: the move is still open").isEqualTo(1);
    }

    // ── 3. re-running changes nothing ───────────────────────────────────────────────────────────────────────

    @Test
    void aCompletedMoveIsNotWorkedAgain_andTheSameTemplateUnderANewLabelWritesNoRow() {
        seedCensusShape();
        runToCompletion();
        var afterFirst = xmins();
        var completed = completedAt(LABEL);

        assertThat(productionWorker().runCycle()).as("a completed move is not worked again").isZero();
        assertThat(completedAt(LABEL)).isEqualTo(completed);

        // a fresh run of the same template from its first chunk, as a superseding move would: nothing is left for it to write
        var copy = new LinkedHashMap<>(registered);
        copy.put("label", "copy-of-" + LABEL);
        insertRegistration(copy);
        assertThat(productionWorker().runCycle()).isEqualTo(1);
        assertThat(completedAt("copy-of-" + LABEL)).isNotNull();
        assertThat(xmins()).as("the second move wrote no row").isEqualTo(afterFirst);
        assertThat(versions(ACME).get(A_V1)).isEqualTo("v1");
    }

    // ── 4. /v1/status shows the move and its progress ───────────────────────────────────────────────────────

    @Test
    void v1StatusShowsTheMove_queued_thenRunsComplete_withItsChunkProgress_andNothingOnceItIsComplete() throws Exception {
        seedCensusShape();
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var service = new NexusService(0, "model-version-repair-token", adminPool);
        service.bootWork(registry);
        service.start();
        HttpClient http = TestHttp.client();
        MoveBootWork reporter = null;
        try {
            var runsOnly = worker(installed(), null);
            reporter = new MoveBootWork(registry, runsOnly.states(), adminConnection, WORKER_NAME + "-bootwork", Clock.systemUTC(),
                java.time.Duration.ofMinutes(15));

            reporter.publish();
            JsonNode queued = entry(http, service, LABEL);
            assertThat(queued).as("an entry for the open move").isNotNull();
            assertThat(queued.get("what").asText()).startsWith("queued: " + LABEL);

            assertThat(runsOnly.runCycle()).isEqualTo(1);
            reporter.publish();
            JsonNode runs = entry(http, service, LABEL);
            assertThat(runs.get("what").asText()).startsWith("runs complete: " + LABEL);
            assertThat(runs.get("unit").asText()).isEqualTo("chunks");
            assertThat(runs.get("safe_to_stop").asBoolean()).isTrue();
            assertThat(runs.get("on_stop").asText()).isEqualTo("resumes from the last committed batch");
            assertThat(runs.get("total").asLong()).as("one chunk for each tenant that holds rows").isGreaterThanOrEqualTo(3);
            assertThat(runs.get("done").asLong()).isEqualTo(runs.get("total").asLong());

            var finishing = productionWorker();
            assertThat(finishing.runCycle()).isEqualTo(1);
            reporter.publish();
            assertThat(entry(http, service, LABEL)).as("a completed move is no longer reported").isNull();
        } finally {
            if (reporter != null) reporter.close();
            service.stop();
        }
    }

    private static JsonNode entry(HttpClient http, NexusService service, String label) throws Exception {
        HttpResponse<String> resp = http.send(
            TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/status").GET().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        for (JsonNode e : MAPPER.readTree(resp.body()).get("boot_work")) {
            if (e.get("id").asText().equals(MoveBootWork.ID_PREFIX + label)) {
                return e;
            }
        }
        return null;
    }

    // ── 5. tenant isolation ─────────────────────────────────────────────────────────────────────────────────

    @Test
    void eachTenantsRowsAreRepairedByThatTenantsOwnRun_andNoRunIsTableWideOrPartitionWise() {
        seedCensusShape();
        var before = xmins();

        runToCompletion();

        assertThat(activeRuns(LABEL)).as("a run per tenant that holds rows, orphan included (no service_tokens row)")
            .contains(LABEL + "/" + ACME, LABEL + "/" + GLOBEX, LABEL + "/" + ORPHAN)
            .allMatch(run -> run.startsWith(LABEL + "/"), "no table-wide run of a tenant-scoped table");
        assertThat(partitionWiseRuns(LABEL)).isZero();

        // one UPDATE statement per tenant's chunk, under that tenant's setting: the rows it repaired share one writer
        // transaction, and no transaction wrote rows of two tenants
        var after = xmins();
        var writerOf = new LinkedHashMap<String, TreeSet<String>>();   // writer transaction -> tenants it wrote
        before.forEach((key, was) -> {
            if (!after.get(key).equals(was)) {
                writerOf.computeIfAbsent(after.get(key), k -> new TreeSet<>()).add(key.substring(0, key.indexOf('/')));
            }
        });
        assertThat(writerOf).as("three tenants were repaired, each by its own transaction").hasSize(3);
        writerOf.forEach((xid, tenants) -> assertThat(tenants).as("transaction %s", xid).hasSize(1));

        // the same name on two tenants: each tenant's row got its own repair
        assertThat(versions(ACME).get(A_V1)).isEqualTo("v1");
        assertThat(versions(GLOBEX).get(A_V1)).isEqualTo("v1");
        assertThat(versions(GLOBEX).get(G_ALREADY_SET)).as("another tenant's already-set row").isEqualTo("v9");
    }

    // ── 6. the verification gates completion ────────────────────────────────────────────────────────────────

    @Test
    void aConformantEmptyRowPutBackAfterTheRuns_failsTheMove_andANonConformantOneDoesNot() {
        seedCensusShape();
        runToCompletion();
        // reopen the move and put a conformant empty back in a chunk that is already done
        ctx().update(table("nexus", "data_moves")).set(COMPLETED_AT, (java.time.OffsetDateTime) null)
            .where(LABEL_FIELD.eq(LABEL)).execute();
        seed(ACME, "code__1-9__voyage-code-3__v1", "");

        assertThat(productionWorker().runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL)).as("a verification that finds the row does not complete the move").isNull();
        assertThat(ctx().select(FAILED_AT).from(table("nexus", "data_moves")).where(LABEL_FIELD.eq(LABEL))
            .fetchOne(FAILED_AT)).as("it fails it, permanently").isNotNull();
    }

    // ── 7. the service role is not widened, and the boot check passes ───────────────────────────────────────

    @Test
    void theServiceRoleStillSeesOnlyItsOwnTenantsCollections_andTheBootCheckAcceptsTheRegisteredMove() throws Exception {
        seedCensusShape();
        int acmeRows = ctx().fetchCount(CATALOG_COLLECTIONS, CC_TENANT.eq(ACME));
        assertThat(acmeRows).as("non-vacuity: acme holds rows").isPositive();

        assertThat(rowsSeenBy(PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD, ACME))
            .as("the owner-read policy is the migrating role's: nexus_svc under acme sees acme's rows and no others")
            .isEqualTo(acmeRows);
        assertThat(rowsSeenBy(PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD, null))
            .as("and unstamped, none").isZero();

        try (var admin = AdminDdlSession.open(adminConnection,
                new AdminDdlSession.Settings(WORKER_NAME + "-iso", 0L, "test", "nexus", "data_moves", "none"), false)) {
            assertThat(DrivingTableIsolationCheck.registeredDrivingTables(admin.ctx()))
                .as("non-vacuity: the boot check has the move's table to ask about")
                .contains(new DrivingTableIsolationCheck.Table("nexus", "catalog_collections"));
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        try (var svc = new HikariDataSource(cfg)) {
            DrivingTableIsolationCheck.verifyAtStartup(svc, adminConnection);   // throws if a policy reaches the serving role
        }
    }

    private int rowsSeenBy(String role, String password, String tenant) throws Exception {
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), role, password)) {
            var db = DSL.using(c, SQLDialect.POSTGRES);
            if (tenant != null) {
                db.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("nexus.tenant"), DSL.val(tenant),
                    DSL.inline(false))).fetch();
            }
            return db.fetchCount(CATALOG_COLLECTIONS);
        }
    }
}
