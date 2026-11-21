// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.http.StatusHandler;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), fix round A, critique B1 and linda's R7 ruling: the completion check reports in its OWN
 * {@code /v1/status} field, {@code rdr229_completion}, and never in {@code boot_work}.
 *
 * <p>{@code boot_work} is work in progress: {@code nx upgrade}'s bundle swap (pg_bundle_refresh.engine_busy_reason) and the 3h
 * boot-write harness (upgrade_boot_writes.boot_work_settled) count every entry that is not a parked data move as work. The
 * check's old entries (rdr229-completion, rdr229-check:*, rdr229-validate:*) were waiting, not working, and stayed on every
 * install until P3.4, so an install whose bundle lacks pg_background (the one whose stamp moves are blocked on it) deferred the
 * swap that would have unblocked them. An older nx reads {@code boot_work} the same way, so the fix is that the entries are
 * never published, not that a newer nx filters them.
 *
 * <p>Each of the three states below renders the engine's real {@code /v1/status} body through {@link StatusHandler}. The body
 * is the fixture the Python idle consumers read ({@code tests/fixtures/rdr229_completion_status/}): this test fails when the
 * engine's body and a fixture differ, so the two languages cannot drift. {@code -Dnexus.rdr229.writeFixtures=true} rewrites them.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CompletionStatusFieldIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Table<?> REGISTRY = DSL.table(DSL.name("nexus", "catalog_collections"));
    private static final Table<?> REMAP = DSL.table(DSL.name("nexus", "chash_remap"));
    private static final String TENANT = "p31-csf";
    private static final String QUEUE = "aspect_extraction_queue";
    private static final Instant T0 = Instant.parse("2026-11-21T12:00:00Z");

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private DSLContext ctx;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
    }

    @AfterAll
    void stop() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void emptyRegistry() {
        ctx.deleteFrom(MOVES).execute();
    }

    // ---- fixtures ------------------------------------------------------------------------------------------------------

    private static Field<String> text(String name) {
        return DSL.field(DSL.name(name), String.class);
    }

    private void register(String label, String drivingTable, boolean complete) {
        ctx.insertInto(MOVES)
            .set(text("label"), label).set(text("template"), "t")
            .set(text("driving_schema"), "nexus").set(text("driving_table"), drivingTable)
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), 100)
            .set(DSL.field(DSL.name("sleep_ms"), Integer.class), 0)
            .set(DSL.field(DSL.name("statement_timeout_ms"), Integer.class), 60_000)
            .set(text("verification"), "v").set(text("changeset_id"), "test")
            .set(DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE), complete ? OffsetDateTime.now() : null)
            .execute();
    }

    private static final class Live implements CompletionBootWork.Reader {
        private final DSLContext ctx;

        Live(DSLContext ctx) {
            this.ctx = ctx;
        }

        @Override
        public CompletionCheck.Result read() {
            return CompletionCheck.evaluate(ctx, CompletionCheck.ReadSet.p31(), CompletionCheck.Gate3Source.NOT_INSTALLED,
                CompletionCheck.DIRECT);
        }

        @Override
        public void abort() { }

        @Override
        public void close() { }
    }

    /** The engine's real body: a bare server bound to {@link StatusHandler}, wired the way NexusService wires it. */
    private JsonNode status(BootWorkRegistry registry) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status", new StatusHandler(null, null)
            .bootWork(registry::snapshot)
            .rdr229Completion(registry::rdr229Completion));
        server.start();
        try {
            HttpResponse<String> resp = dev.nexus.service.TestHttp.client().send(
                dev.nexus.service.TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/status").GET().build(),
                HttpResponse.BodyHandlers.ofString());
            assertThat(resp.statusCode()).isEqualTo(200);
            ObjectNode body = (ObjectNode) MAPPER.readTree(resp.body());
            body.put("process_start_time", "1970-01-01T00:00:00Z");   // the one field that is the process's own clock
            return body;
        } finally {
            server.stop(0);
        }
    }

    private static Path repoRoot() {
        Path p = Path.of("").toAbsolutePath();
        while (p != null && !Files.isDirectory(p.resolve("service/src/main/java"))) {
            p = p.getParent();
        }
        assertThat(p).as("the repository root above the working directory").isNotNull();
        return p;
    }

    /** The body equals the fixture the Python consumers read; {@code -Dnexus.rdr229.writeFixtures=true} rewrites it. */
    private void assertFixture(String name, JsonNode body) throws Exception {
        Path file = repoRoot().resolve("tests/fixtures/rdr229_completion_status").resolve(name + ".json");
        if (Boolean.getBoolean("nexus.rdr229.writeFixtures")) {
            Files.createDirectories(file.getParent());
            Files.writeString(file, MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(body) + "\n", StandardCharsets.UTF_8);
        }
        assertThat(file).as("the fixture the Python idle-consumer tests read").exists();
        assertThat(MAPPER.readTree(Files.readString(file))).as("fixture " + name + " is the engine's body").isEqualTo(body);
    }

    private static List<String> bootWorkIds(JsonNode body) {
        List<String> ids = new ArrayList<>();
        body.get("boot_work").forEach(e -> ids.add(e.get("id").asText()));
        return ids;
    }

    private CompletionBootWork newWork(BootWorkRegistry registry) {
        return new CompletionBootWork(registry, new Live(ctx));
    }

    // ---- the three states ----------------------------------------------------------------------------------------------

    @Test
    @Order(1)
    void stampsBlocked_theCheckAddsNoBootWorkEntry_andTheFieldCarriesEveryOpenLabel() throws Exception {
        // an install whose bundle lacks pg_background: the 15 stamp moves are registered and open, the move reporter says
        // "blocked:" for each (parked, which the idle rules discount); the completion check adds nothing to boot_work
        var registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));
        int opened = 0;
        for (var table : P31Checks.checksByTable().keySet()) {
            if (table.equals(QUEUE)) {
                continue;
            }
            register(StampLabels.labelFor(table), table, false);
            opened++;
        }
        assertThat(opened).as("non-vacuity: 15 open stamp moves").isEqualTo(15);
        String id = MoveBootWork.ID_PREFIX + StampLabels.labelFor("chunks");
        registry.register(id, "blocked: " + StampLabels.labelFor("chunks") + ": pg_background is not installed", 0L, null,
            MoveBootWork.UNIT, true, MoveBootWork.ON_STOP);
        registry.update(id, "blocked: " + StampLabels.labelFor("chunks") + ": pg_background is not installed", 0L, null, null,
            "missing_prerequisite");
        var work = newWork(registry);
        assertThat(work.publish()).as("the field reports the gates, and one label per open move").isPositive();

        JsonNode body = status(registry);
        assertThat(bootWorkIds(body)).as("only the parked data move is boot_work").containsExactly(id);
        JsonNode f = body.get("rdr229_completion");
        assertThat(f.get("gate1").asBoolean()).isFalse();
        assertThat(f.get("gates_holding").asInt()).isZero();
        assertThat(f.get("gates_total").asInt()).isEqualTo(3);
        assertThat(f.get("labels_total").asInt()).isEqualTo(16);
        assertThat(f.get("labels_incomplete")).as("15 open labels; the queue's label is not applicable").hasSize(15);
        assertThat(f.get("labels_incomplete").get(0).fieldNames()).toIterable().containsExactly("label", "table", "why");
        assertThat(f.get("summary").asText()).startsWith("gate 1 waits (15 of 16 stamp moves incomplete)");
        assertFixture("stamps_blocked", body);
    }

    @Test
    @Order(2)
    void supersededByCheckMissing_theCheckAddsNoBootWorkEntry_andTheFieldSaysHowManyRowsHoldItBack() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT, "tomb-p31-csf");
        P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        try {
            ctx.update(REGISTRY).set(text("superseded_by"), "successor-gone").set(text("model_version"), "")
                .where(text("name").eq("tomb-p31-csf")).execute();
            var registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));
            newWork(registry).publish();

            JsonNode body = status(registry);
            assertThat(bootWorkIds(body)).as("no rdr229 entry of any kind").isEmpty();
            JsonNode f = body.get("rdr229_completion");
            assertThat(f.get("superseded_by_waiting").asInt()).isEqualTo(1);
            assertThat(f.get("checks_missing")).singleElement().satisfies(c -> {
                assertThat(c.get("table").asText()).isEqualTo("catalog_collections");
                assertThat(c.get("constraint").asText()).isEqualTo(CompletionCheck.SUPERSEDED_BY_CHECK);
                assertThat(c.get("what").asText()).startsWith("waits: " + CompletionCheck.SUPERSEDED_BY_CHECK)
                    .contains("1 registry row(s) with a blank model_version and no superseded_by_id");
            });
            assertThat(f.get("summary").asText()).contains("the superseded_by CHECK is missing");
            assertFixture("superseded_by_check_missing", body);
        } finally {
            ctx.deleteFrom(REGISTRY).where(text("name").eq("tomb-p31-csf")).execute();
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        }
    }

    @Test
    @Order(99)
    void allStampsDone_gate3Waits_theCheckAddsNoBootWorkEntry_theWholeStateIsInTheField() throws Exception {
        int done = 0;
        for (var e : P31Checks.checksByTable().entrySet()) {
            if (e.getKey().equals(QUEUE)) {
                continue;
            }
            register(StampLabels.labelFor(e.getKey()), e.getKey(), true);
            done++;
            for (P31Checks.Row row : e.getValue()) {
                PgContainerHelper.validateConstraint(su, DSL.table(DSL.name("nexus", e.getKey())), row.constraint());
            }
        }
        assertThat(done).as("non-vacuity: 15 completed stamp moves").isEqualTo(15);
        var registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));
        newWork(registry).publish();

        JsonNode body = status(registry);
        assertThat(bootWorkIds(body)).as("gate 3 waits by design until P3.4: that is not work in progress").isEmpty();
        JsonNode f = body.get("rdr229_completion");
        assertThat(f.get("gate1").asBoolean()).isTrue();
        assertThat(f.get("gate2").asBoolean()).isTrue();
        assertThat(f.get("gate3").asBoolean()).isFalse();
        assertThat(f.get("gates_holding").asInt()).isEqualTo(2);
        assertThat(f.get("gate3_why").asText()).contains("P3.4");
        assertThat(f.get("labels_incomplete")).isEmpty();
        assertThat(f.get("checks_missing")).isEmpty();
        assertThat(f.get("validate_waits")).isEmpty();
        assertFixture("all_stamps_done_gate3_waiting", body);
    }

    @Test
    @Order(3)
    void theFieldIsAbsentBeforeTheFirstRead_aClientReadsThatAsNotKnownYet() throws Exception {
        var registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));
        newWork(registry);   // constructed, not yet sampled
        assertThat(status(registry).has("rdr229_completion")).isFalse();
    }

    // ---- an orphan on a two-name table names every NOT VALID constraint (critique S1) ----------------------------------

    @Test
    @Order(4)
    void aTableWithTwoNameColumns_listsEveryNotValidConstraint_withTheSummedCounts_andNoCollectionOrTenant() throws Exception {
        // chash_remap carries a CHECK per name column. A move that is done and an orphan on the TARGET column leaves BOTH
        // CHECKs NOT VALID (the stamp leaves any row with an orphan name alone); the field names both constraints.
        PgContainerHelper.insertCollection(ctx, TENANT, "code__p31csf__bge-base-en-v15-768__v1");
        P31CheckReplay.lift(ctx, "vectors-068-14");
        try {
            ctx.insertInto(REMAP).set(text("tenant_id"), TENANT).set(text("source_collection"), "code__p31csf__bge-base-en-v15-768__v1")
                .set(text("old_id"), "o").set(DSL.field(DSL.name("new_chash"), byte[].class), new byte[32]).set(text("target_collection"), "ghost-target-p31-csf")
                .set(DSL.field(DSL.name("created_at"), SQLDataType.TIMESTAMPWITHTIMEZONE), OffsetDateTime.now())
                .set(text("provenance"), "test").execute();
        } finally {
            P31CheckReplay.restore(ctx, "vectors-068-14");
        }
        try {
            register(StampLabels.labelFor("chash_remap"), "chash_remap", true);
            var registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));
            newWork(registry).publish();
            JsonNode body = status(registry);
            assertThat(bootWorkIds(body)).isEmpty();
            JsonNode waits = body.get("rdr229_completion").get("validate_waits");
            assertThat(waits).singleElement().satisfies(w -> {
                assertThat(w.get("table").asText()).isEqualTo("chash_remap");
                List<String> constraints = new ArrayList<>();
                w.get("constraints").forEach(c -> constraints.add(c.asText()));
                assertThat(constraints).containsExactlyInAnyOrder("chash_remap_source_collection_id_check",
                    "chash_remap_target_collection_id_check");
                assertThat(w.get("orphans").asLong()).isEqualTo(1);
                assertThat(w.get("unstamped").asLong()).isEqualTo(1);
                assertThat(w.get("counted").asBoolean()).isTrue();
                assertThat(w.get("what").asText()).contains("chash_remap_source_collection_id_check, chash_remap_target_collection_id_check");
            });
            assertThat(body.toString()).doesNotContain("ghost-target-p31-csf").doesNotContain(TENANT);
        } finally {
            ctx.deleteFrom(REMAP).where(text("tenant_id").eq(TENANT)).execute();
            ctx.deleteFrom(REGISTRY).where(text("tenant_id").eq(TENANT)).execute();
        }
    }

    // ---- R7: the pre-flight count of dangling tombstones, split by content type ------------------------------------------

    private void tombstone(String name, String supersededBy, boolean selfId) {
        PgContainerHelper.insertCollection(ctx, TENANT, name);
        ctx.update(REGISTRY).set(text("superseded_by"), supersededBy)
            .set(DSL.field(DSL.name("superseded_by_id"), Long.class), selfId
                ? DSL.field(DSL.name("collection_id"), Long.class) : DSL.val((Long) null, Long.class))
            .where(text("tenant_id").eq(TENANT)).and(text("name").eq(name)).execute();
    }

    @Test
    @Order(5)
    void danglingTombstones_areCountedPerContentType_andTheSelfIdRowsApart_neverNamed() throws Exception {
        // one unstamped dangling tombstone per content type (the CHECK is lifted: an upgraded install has rows the stamp has
        // not reached), one the stamp already resolved to its fallback (superseded_by_id = its own collection_id), and one
        // tombstone whose successor exists, which is not dangling
        P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        try {
            tombstone("code__p31csfd__bge-base-en-v15-768__v1", "gone-code-p31-csf", false);
            tombstone("docs__p31csfd__bge-base-en-v15-768__v1", "gone-docs-p31-csf", false);
            tombstone("rdr__p31csfd__bge-base-en-v15-768__v1", "gone-rdr-p31-csf", false);
            tombstone("docs__p31csfe__bge-base-en-v15-768__v1", "gone-docs-self-p31-csf", true);
            PgContainerHelper.insertCollection(ctx, TENANT, "knowledge__p31csfd__bge-base-en-v15-768__v1");
            tombstone("knowledge__p31csfs__bge-base-en-v15-768__v1", "knowledge__p31csfd__bge-base-en-v15-768__v1", false);

            var registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));
            newWork(registry).publish();
            JsonNode body = status(registry);
            JsonNode d = body.get("rdr229_completion").get("dangling_tombstones");
            assertThat(d.get("total").asLong()).as("code, docs, rdr, and the docs one the stamp resolved").isEqualTo(4);
            assertThat(d.get("by_content_type").get("code").asLong()).isEqualTo(1);
            assertThat(d.get("by_content_type").get("docs").asLong()).isEqualTo(2);
            assertThat(d.get("by_content_type").get("rdr").asLong()).isEqualTo(1);
            assertThat(d.get("by_content_type").has("knowledge")).as("a tombstone with a live successor is not dangling").isFalse();
            assertThat(d.get("self_id").get("total").asLong()).isEqualTo(1);
            assertThat(d.get("self_id").get("by_content_type").get("docs").asLong()).isEqualTo(1);
            assertThat(d.get("self_id").get("by_content_type").has("code")).isFalse();
            assertThat(d.get("registry_rows").asLong()).as("the registry was read").isGreaterThanOrEqualTo(6);
            assertThat(body.toString()).as("/v1/status is unauthenticated: counts only").doesNotContain("gone-").doesNotContain(TENANT)
                .doesNotContain("p31csf");
        } finally {
            ctx.deleteFrom(REGISTRY).where(text("tenant_id").eq(TENANT)).execute();
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        }
    }

    @Test
    @Order(6)
    void anInstallWithNoDanglingTombstone_reportsZero_afterReadingTheRegistry() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT, "code__p31csfz__bge-base-en-v15-768__v1");
        try {
            var registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));
            newWork(registry).publish();
            JsonNode d = status(registry).get("rdr229_completion").get("dangling_tombstones");
            assertThat(d.get("total").asLong()).isZero();
            assertThat(d.get("by_content_type")).isEmpty();
            assertThat(d.get("self_id").get("total").asLong()).isZero();
            assertThat(d.get("registry_rows").asLong()).as("non-vacuity: it read a registry that has a row").isGreaterThanOrEqualTo(1);
        } finally {
            ctx.deleteFrom(REGISTRY).where(text("tenant_id").eq(TENANT)).execute();
        }
    }
}
