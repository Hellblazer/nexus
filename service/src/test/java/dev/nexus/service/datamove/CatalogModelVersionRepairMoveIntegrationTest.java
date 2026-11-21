// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
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
import org.jooq.DSLContext;
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
import java.lang.reflect.Method;
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
 * <p>The red run is the registration: before vectors-064 existed every scenario failed at "the walk registers the move".
 * What each assertion is meant to catch, not run by hand: drop {@code t.model_version = ''} from the template and the
 * "already set" row is rewritten (its xmin moves); loosen the name pattern to {@code ~ '__v[0-9]+$'} and the negative fixtures
 * ({@code docs____m__v1}, {@code docs__o__1m__v1}, {@code docs__a__b__m__v1}) are rewritten; drop its end anchor and
 * {@code __v1x} is rewritten; put the INSERT into a changeset that also UPDATEs {@code catalog_collections} and the walk test
 * sees every row's xmin move. The two quarantine scenarios of nexus-f3yqd.2.23 review round 1 were red before the template
 * took the {@code quarantine-} prefix (the sibling stayed '' and stopped matching its origin); their negative halves, like the
 * negative fixtures, pass by construction and are guarded by the loosening above.
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
    // names the grammar must refuse: an empty owner, a model that starts with a digit, an owner with a doubled underscore run
    private static final String EMPTY_OWNER = "docs____m__v1";
    private static final String DIGIT_MODEL = "docs__o__1m__v1";
    private static final String DOUBLED_UNDERSCORE_OWNER = "docs__a__b__m__v1";
    private static final String Q_ORIGIN = "docs__1-7__bge-base-en-v15-768__v1";
    // a registered quarantine sibling of Q_ORIGIN by row attributes, under a name that is not the reaper's (quarantine- + origin)
    private static final String Q_SIBLING = "quarantine-docs__sib-7__bge-base-en-v15-768__v1";
    private static final String G_ALREADY_SET = "rdr__1-4__voyage-context-3__v4";
    private static final String O_V1 = "docs__1-5__voyage-context-3__v1";

    /** The registry row exactly as the walk wrote it, and how many rows the walk left. */
    private static Map<String, Object> registered;
    private static int registeredRowsAtBoot;
    private static java.util.Set<String> labelsAtBoot;

    @BeforeAll
    void captureRegistration() {
        labelsAtBoot = new java.util.TreeSet<>(ctx().select(LABEL_FIELD).from(table("nexus", "data_moves")).fetch(LABEL_FIELD));
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
        ctx().deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.in(TENANTS)).execute();
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
        seed(ACME, EMPTY_OWNER, "");
        seed(ACME, DIGIT_MODEL, "");
        seed(ACME, DOUBLED_UNDERSCORE_OWNER, "");
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
        assertThat(acme.get(QUARANTINE)).as("a quarantine- sibling of a conformant name is repaired with its origin").isEqualTo("v1");
        assertThat(acme.get(SUFFIX_NOT_A_VERSION)).as("__v1x is not a version suffix").isEmpty();
        assertThat(acme.get(EMPTY_OWNER)).as("an empty owner is not conformant").isEmpty();
        assertThat(acme.get(DIGIT_MODEL)).as("a model that starts with a digit is not conformant").isEmpty();
        assertThat(acme.get(DOUBLED_UNDERSCORE_OWNER)).as("an owner with a doubled underscore run is not conformant").isEmpty();
        assertThat(versions(GLOBEX)).containsEntry(A_V1, "v1").containsEntry(G_ALREADY_SET, "v9");
        assertThat(versions(ORPHAN)).containsEntry(O_V1, "v1");

        // the move wrote the repaired rows and nothing else: every row it left alone keeps its xmin
        var after = xmins();
        for (String untouched : List.of(ACME + "/" + A_ALREADY_SET, ACME + "/" + NON_CONFORMANT, ACME + "/" + LEGACY_TWO_SEGMENT,
                ACME + "/" + SUFFIX_NOT_A_VERSION, ACME + "/" + EMPTY_OWNER, ACME + "/" + DIGIT_MODEL,
                ACME + "/" + DOUBLED_UNDERSCORE_OWNER, GLOBEX + "/" + G_ALREADY_SET)) {
            assertThat(after.get(untouched)).as("%s was not written", untouched).isEqualTo(before.get(untouched));
        }
        for (String repaired : List.of(ACME + "/" + A_V1, ACME + "/" + A_V3, ACME + "/" + QUARANTINE, GLOBEX + "/" + A_V1, ORPHAN + "/" + O_V1)) {
            assertThat(after.get(repaired)).as("%s was written", repaired).isNotEqualTo(before.get(repaired));
        }

        // reported: what the move leaves is exactly the non-conformant empties, which is the census's "unusable" count
        var leftEmpty = new TreeSet<String>();
        versions(ACME).forEach((name, v) -> {
            if (v.isEmpty()) leftEmpty.add(name);
        });
        assertThat(leftEmpty).containsExactlyInAnyOrder(NON_CONFORMANT, LEGACY_TWO_SEGMENT, SUFFIX_NOT_A_VERSION, EMPTY_OWNER, DIGIT_MODEL,
            DOUBLED_UNDERSCORE_OWNER);
    }

    // ── 2. registered once, and the walk writes no catalog_collections row ──────────────────────────────────

    @Test
    void theMoveIsRegisteredExactlyOnce_withTheParametersRdr246Prescribes() {
        // Every registered move the product changelog ships, by label, read from the master's registering includes: a new
        // registration is picked up, and a label registered by anything else (a stray row) still fails this test.
        assertThat(labelsAtBoot).as("the walk registers exactly the shipped moves").containsExactlyInAnyOrderElementsOf(
            ShippedMoveRegistrations.labels());
        assertThat(ShippedMoveRegistrations.labels()).as("the repair move is among them").contains(LABEL);
        assertThat(registeredRowsAtBoot).isEqualTo(1);
        assertThat(registered.get("label").toString()).doesNotContain("/").isEqualTo(LABEL);
        assertThat(registered).containsEntry("driving_schema", "nexus").containsEntry("driving_table", "catalog_collections")
            .containsEntry("changeset_id", REGISTER_CHANGESET)
            .containsEntry("on_complete", null).containsEntry("supersedes", null)
            .containsEntry("completed_at", null).containsEntry("failed_at", null);
        assertThat(registered.get("template").toString()).contains("<driving_table>").contains("<chunking_clause>");
        assertThat((Integer) registered.get("chunk_size"))
            .as("one chunk per tenant in practice, so a quarantine pair is repaired in one transaction").isEqualTo(100_000);
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
    void aNonConformantEmptyRowPutBackDoesNotFailTheMove_aConformantOneDoes() {
        seedCensusShape();
        runToCompletion();

        // reopen the move and put a NON-conformant empty back: the verification does not look for it
        reopen();
        seed(ACME, "gate-probe-125", "");
        assertThat(productionWorker().runCycle()).isEqualTo(1);
        assertThat(completedAt(LABEL)).as("a non-conformant empty is not an old-shape row: the move completes").isNotNull();
        assertThat(failedAt()).isNull();

        // reopen it and put a conformant empty back in a chunk that is already done
        reopen();
        seed(ACME, "code__1-9__voyage-code-3__v1", "");
        assertThat(productionWorker().runCycle()).isEqualTo(1);
        assertThat(completedAt(LABEL)).as("a verification that finds the row does not complete the move").isNull();
        assertThat(failedAt()).as("it fails it, permanently").isNotNull();
    }

    private void reopen() {
        ctx().update(table("nexus", "data_moves")).set(COMPLETED_AT, (java.time.OffsetDateTime) null)
            .where(LABEL_FIELD.eq(LABEL)).execute();
    }

    private java.time.OffsetDateTime failedAt() {
        return ctx().select(FAILED_AT).from(table("nexus", "data_moves")).where(LABEL_FIELD.eq(LABEL)).fetchOne(FAILED_AT);
    }

    // ── 6b. quarantine siblings are repaired with their origins ─────────────────────────────────────────────

    @Test
    void anOriginAndItsQuarantineSiblingAtEmpty_endAtTheSameVersion_andStillMatchThroughQuarantineOrigin() throws Exception {
        seed(ACME, Q_ORIGIN, "");
        seed(ACME, Q_SIBLING, "");
        // the sibling is the origin's by row attributes (owner, model, version, content type), not by the reaper's name
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.OWNER_ID, "1-7")
            .where(CC_TENANT.eq(ACME)).and(CC_NAME.eq(Q_SIBLING)).execute();
        PgContainerHelper.insertChunks(ctx(), ACME, Q_SIBLING, List.of("ab".repeat(32)), List.of("quarantined"),
            List.of(new float[768]), List.of(Map.of()));
        assertThat(quarantineRowsOf(ACME, Q_ORIGIN)).as("non-vacuity: before the move both are '' and match").isEqualTo(1);

        runToCompletion();

        assertThat(versions(ACME)).containsEntry(Q_ORIGIN, "v1").containsEntry(Q_SIBLING, "v1");
        assertThat(quarantineRowsOf(ACME, Q_ORIGIN)).as("after the move the pair still matches").isEqualTo(1);
    }

    @Test
    void aQuarantineSiblingWhoseOriginPartIsNotConformant_staysEmpty() {
        seed(ACME, "quarantine-gate-probe-124", "");
        seed(ACME, "quarantine-" + EMPTY_OWNER, "");
        seed(ACME, "quarantine-" + DIGIT_MODEL, "");
        seed(ACME, A_V1, "");

        runToCompletion();

        assertThat(versions(ACME)).containsEntry(A_V1, "v1")
            .containsEntry("quarantine-gate-probe-124", "")
            .containsEntry("quarantine-" + EMPTY_OWNER, "")
            .containsEntry("quarantine-" + DIGIT_MODEL, "");
    }

    @Test
    void aSiblingWhoseNameCarriesAnotherVersionThanItsOrigin_isRepairedToItsOwnNamesVersion_andStopsMatchingByAttributes()
            throws Exception {
        String origin = "docs__1-8__bge-base-en-v15-768__v1";
        String sibling = "quarantine-docs__sib-8__bge-base-en-v15-768__v2";   // its name says v2, its origin's says v1
        seed(ACME, origin, "");
        seed(ACME, sibling, "");
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.OWNER_ID, "1-8")
            .where(CC_TENANT.eq(ACME)).and(CC_NAME.eq(sibling)).execute();
        PgContainerHelper.insertChunks(ctx(), ACME, sibling, List.of("cd".repeat(32)), List.of("quarantined"),
            List.of(new float[768]), List.of(Map.of()));
        assertThat(quarantineRowsOf(ACME, origin)).as("non-vacuity: both at '' they match by attributes").isEqualTo(1);

        runToCompletion();

        // what the move does here, stated: each row takes the version its own name carries, so the pair no longer shares
        // a model_version and QuarantineOrigin's attribute arm (and the reaper-named arm, since the sibling is not
        // quarantine- + origin) no longer finds the sibling. The census found no quarantine row at '' on either production
        // tenant; vectors-064's header says the same.
        assertThat(versions(ACME)).containsEntry(origin, "v1").containsEntry(sibling, "v2");
        assertThat(quarantineRowsOf(ACME, origin)).as("the pair stops matching").isZero();
    }

    @Test
    void aQuarantineSiblingPutBackAtEmptyAfterTheRuns_failsTheMove() {
        seedCensusShape();
        runToCompletion();
        reopen();
        seed(ACME, "quarantine-code__1-9__voyage-code-3__v1", "");

        assertThat(productionWorker().runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL)).as("an empty conformant quarantine sibling is an old-shape row").isNull();
        assertThat(failedAt()).isNotNull();
    }

    /** {@code QuarantineOrigin.countRowsOf}, package-private in the engine: the quarantine rows that are {@code origin}'s. */
    private int quarantineRowsOf(String tenant, String origin) throws Exception {
        Method m = Class.forName("dev.nexus.service.db.QuarantineOrigin")
            .getDeclaredMethod("countRowsOf", DSLContext.class, String.class, String.class);
        m.setAccessible(true);
        return (Integer) m.invoke(null, ctx(), tenant, origin);
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
