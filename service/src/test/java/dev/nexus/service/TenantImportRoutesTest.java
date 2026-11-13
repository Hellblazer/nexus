// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.ImportTestEnv.IMPORT;
import static dev.nexus.service.ImportTestEnv.json;
import static dev.nexus.service.ImportTestEnv.sha256;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.nexus.service.db.ExportTestClock;
import dev.nexus.service.db.TenantImportSessions;
import dev.nexus.service.db.TokenStore;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.io.OutputStream;
import java.net.Socket;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * The tenant import routes through the real service and its auth filter on a real PostgreSQL (RDR-248 Step 5, bead
 * nexus-csayy.6): what an open refuses and what it costs, the part protocol, who may see a session, the bounds an
 * upload keeps, and what a client that drops or loses a token sees. The load itself (ids, soft references, the
 * seeded rows) is {@code TenantImportRoundTripTest}'s and {@code TenantImportSoftReferencesTest}'s; the locks and
 * the abort are {@code TenantImportConcurrencyTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantImportRoutesTest {

    private static final String SRC = "t-imp-rt-source";

    private ImportTestEnv env;
    private ImportTestEnv.BackupFile file;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
        String srcToken = env.newTenant(SRC);
        List<String> failures = new ArrayList<>();
        env.su(ctx -> {
            TenantExportBatteryTest.Seed.rows(ctx, SRC, 1, failures);
            TenantExportBatteryTest.Seed.fillNulls(ctx, SRC, failures);
            ctx.update(DSL.table(DSL.name("nexus", "memory"))).set(DSL.field(DSL.name("access_count")), 5)
                .where(DSL.field(DSL.name("tenant_id"), String.class).eq(SRC)).execute();
            return null;
        });
        assertThat(failures).as("the seed").isEmpty();
        file = env.export(srcToken);
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    @BeforeEach
    void freshSessions() {
        env.freshSessions();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────

    private JsonNode openOk(String token, ImportTestEnv.BackupFile f) throws Exception {
        HttpResponse<String> r = env.open(token, f);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return json(r);
    }

    /** Uploads and closes every table the open answer lists that comes before {@code table}. */
    private void uploadBefore(String token, String id, JsonNode opened, String table) throws Exception {
        for (JsonNode t : opened.get("tables")) {
            if (t.asText().equals(table)) return;
            env.uploadTable(token, id, file, t.asText(), 100);
        }
    }

    private String commitAndAwait(String token, String id) throws Exception {
        HttpResponse<String> c = env.call("POST", IMPORT + "/" + id + "/commit", token);
        assertThat(c.statusCode()).as(c.body()).isEqualTo(202);
        return env.awaitEnd(token, id, Duration.ofMinutes(2)).get("status").asText();
    }

    private void assertNothingWritten(String tenant) {
        for (var t : dev.nexus.service.db.TenantExport.TABLES) {
            if (t.exported() && !t.name().equals("catalog_meta")) {
                assertThat(env.count(t.table(), tenant)).as(t.name() + " of " + tenant).isZero();
            }
        }
    }

    // ── open: refusals ──────────────────────────────────────────────────────────────────────────

    @Test
    void aNonEmptyTargetIsRefusedByTheOpenAndNothingIsWritten() throws Exception {
        String tenant = "t-imp-nonempty";
        String token = env.newTenant(tenant);
        env.su(ctx -> ctx.insertInto(DSL.table(DSL.name("nexus", "memory")),
                DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("project")), DSL.field(DSL.name("title")),
                DSL.field(DSL.name("content")), DSL.field(DSL.name("timestamp")))
            .values(tenant, "p", "mine", "already here", OffsetDateTime.now()).execute());

        HttpResponse<String> r = env.open(token, file);

        assertThat(r.statusCode()).isEqualTo(409);
        assertThat(json(r).get("reason").asText()).isEqualTo("tenant_not_empty");
        assertThat(json(r).get("table").asText()).isEqualTo("memory");
        assertThat(env.count("memory", tenant)).as("the tenant's own row, untouched").isEqualTo(1);
        assertThat(env.count("chunks", tenant)).isZero();
        assertThat(env.count("catalog_documents", tenant)).isZero();
        // the refusal opened nothing: no session holds the tenant's place
        assertThat(env.call("POST", "/v1/tenant/export", token).statusCode()).as("an export may open").isEqualTo(200);
    }

    @Test
    void aWriteDuringTheUploadRefusesTheLoadAndNothingIsWritten() throws Exception {
        String tenant = "t-imp-raced";
        String token = env.newTenant(tenant);
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        env.upload(token, id, file, opened, 100);

        env.su(ctx -> ctx.insertInto(DSL.table(DSL.name("nexus", "memory")),
                DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("project")), DSL.field(DSL.name("title")),
                DSL.field(DSL.name("content")), DSL.field(DSL.name("timestamp")))
            .values(tenant, "p", "landed during the upload", "c", OffsetDateTime.now()).execute());

        assertThat(commitAndAwait(token, id)).isEqualTo("failed: tenant not empty: memory");
        assertThat(env.count("memory", tenant)).as("only the write that landed").isEqualTo(1);
        assertThat(env.count("chunks", tenant)).as("nothing of the file").isZero();
        assertThat(env.count("catalog_documents", tenant)).isZero();
        assertThat(json(env.call("GET", "/v1/tenant/import-marker", token)).get("present").asBoolean()).isFalse();
    }

    @Test
    void aNewerFileWithAColumnTheTargetLacksIsRefusedNamingTheColumn() throws Exception {
        String tenant = "t-imp-newer";
        String token = env.newTenant(tenant);
        ImportTestEnv.BackupFile newer = file.withColumn("memory", "from_a_newer_engine", "text", "x".getBytes(StandardCharsets.UTF_8)).seal();

        HttpResponse<String> r = env.open(token, newer);

        assertThat(r.statusCode()).isEqualTo(422);
        assertThat(json(r).get("reason").asText()).isEqualTo("file_not_loadable");
        assertThat(json(r).get("error").asText()).contains("memory.from_a_newer_engine");
        assertThat(json(r).get("problems").toString()).contains("memory.from_a_newer_engine");
        assertNothingWritten(tenant);
        openOk(token, file);   // the refusal left no session behind
    }

    @Test
    void aNewerFileWhoseColumnsAllMatchLoadsAndTheCountDifferenceIsShown() throws Exception {
        String tenant = "t-imp-newer-count";
        String token = env.newTenant(tenant);
        long engineCount = file.header.get("schema_changeset_count").asLong();
        ImportTestEnv.BackupFile newer = file.copy();
        newer.header.put("schema_changeset_count", engineCount + 7);
        newer.seal();
        env.importSessions(TenantImportSessions.Config.defaults(), List.of(), () -> engineCount);   // what /version reports

        HttpResponse<String> opened = env.open(token, newer);

        assertThat(opened.statusCode()).as(opened.body()).isEqualTo(200);
        assertThat(json(opened).get("notes").toString()).contains("schema_changeset_count is " + (engineCount + 7))
            .contains("this engine's is " + engineCount);
        String id = json(opened).get("session_id").asText();
        env.upload(token, id, newer, json(opened), 100);
        assertThat(commitAndAwait(token, id)).isEqualTo("committed");
        assertThat(env.count("memory", tenant)).isEqualTo(env.count("memory", SRC));
        assertThat(env.count("chunks", tenant)).isEqualTo(env.count("chunks", SRC));
        assertThat(json(env.call("GET", IMPORT + "/" + id, token)).get("notes").toString())
            .as("the status keeps the note").contains("this engine's is " + engineCount);
    }

    @Test
    void aChildTableWhoseReassignedIdParentIsNotInTheFileIsRefusedNamingBoth() throws Exception {
        String token = env.newTenant("t-imp-noparent");
        ImportTestEnv.BackupFile f = file.copy();
        var tables = f.header.withArray("tables");
        for (int i = tables.size() - 1; i >= 0; i--) {
            if (tables.get(i).get("name").asText().equals("topics")) tables.remove(i);
        }
        f.pages.remove("topics");
        f.seal();

        HttpResponse<String> r = env.open(token, f);

        assertThat(r.statusCode()).isEqualTo(422);
        assertThat(json(r).get("reason").asText()).isEqualTo("file_not_loadable");
        assertThat(json(r).get("problems").toString()).contains("topic_assignments").contains("topics").contains("by id");
        openOk(token, file);   // no session was left behind
    }

    @Test
    void aChildRowWhoseParentRowIsNotInTheFileFailsTheLoadNamingTheColumnAndWritesNothing() throws Exception {
        String tenant = "t-imp-orphanrow";
        String token = env.newTenant(tenant);
        ImportTestEnv.BackupFile f = file.copy();
        f.pages.put("topics", List.of());   // the table is in the file and holds no rows, while its children point at some
        f.seal();

        JsonNode end = env.restore(token, f);

        assertThat(end.get("status").asText()).startsWith("failed: ").contains("topic_assignments.topic_id")
            .contains("topics row the file does not hold");
        assertNothingWritten(tenant);
    }

    @Test
    void anErrorThatEndsALoadLeavesItFailedAndNeverCommittedAndWritesNoMarker() throws Exception {
        String tenant = "t-imp-error";
        String token = env.newTenant(tenant);
        var sessions = env.importSessions(TenantImportSessions.Config.defaults(), List.of());
        sessions.loadHookForTests(text -> {
            if (text.equals("loading memory")) throw new AssertionError("an Error, not an Exception, in the middle of the load");
        });
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        env.upload(token, id, file, opened, 100);

        assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(202);
        JsonNode end = env.awaitEnd(token, id, Duration.ofMinutes(1));

        assertThat(end.get("state").asText()).isEqualTo("failed");
        assertThat(end.get("status").asText()).startsWith("failed: ").contains("an Error");
        assertNothingWritten(tenant);
        assertThat(json(env.call("GET", "/v1/tenant/import-marker", token)).get("present").asBoolean()).isFalse();
        assertThat(env.ds.getHikariPoolMXBean().getActiveConnections()).isZero();
        sessions.loadHookForTests(null);
    }

    @Test
    void aSweepDoesNotEndASessionWhileARequestOfItIsRunning() throws Exception {
        String token = env.newTenant("t-imp-sweep-busy");
        ExportTestClock clock = new ExportTestClock(java.time.Instant.parse("2026-11-13T12:00:00Z"));
        env.exportSessions(dev.nexus.service.db.TenantExportSessions.Config.defaults().withMaxSessions(6), clock);
        var sessions = env.importSessions(TenantImportSessions.Config.defaults(), List.of());
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        String first = opened.get("tables").get(0).asText();
        byte[] part = BinaryCopyTestSupport.split(file.pages.get(first).get(0), 100).get(0);
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        sessions.partHookForTests(table -> {
            inside.countDown();
            try {
                release.await(60, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        });
        CompletableFuture<HttpResponse<String>> running = CompletableFuture.supplyAsync(() -> {
            try {
                return env.putPart(token, id, first, 0, part, sha256(part));
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(inside.await(60, TimeUnit.SECONDS)).isTrue();

        clock.advance(Duration.ofMinutes(61));   // past the total bound, with a part still running
        assertThat(sessions.sweep()).as("a request is running: it is not ended under it").isZero();
        release.countDown();
        assertThat(running.get(60, TimeUnit.SECONDS).statusCode()).as("the part completes").isEqualTo(200);
        sessions.partHookForTests(null);

        assertThat(sessions.sweep()).as("and the next sweep ends it, for its own reason").isEqualTo(1);
        assertThat(json(env.call("GET", IMPORT + "/" + id, token)).get("status").asText()).startsWith("failed: total bound");
    }

    @Test
    void aMarkerRowThatIsNotJsonIsAnUnreadableMarkerNotAServerError() throws Exception {
        // not JSON at all, and valid JSON that is not an object (confirm round, 2026-11-13): neither names a file
        String[] values = {"this is {not json", "\"abc\"", "123"};
        for (int i = 0; i < values.length; i++) {
            String tenant = "t-imp-badmarker-" + i;
            String token = env.newTenant(tenant);
            String value = values[i];
            env.su(ctx -> ctx.insertInto(DSL.table(DSL.name("nexus", "catalog_meta")),
                    DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("key")), DSL.field(DSL.name("value")))
                .values(tenant, "rdr248_restore", value).execute());

            HttpResponse<String> r = env.call("GET", "/v1/tenant/import-marker", token);

            assertThat(r.statusCode()).as(value).isEqualTo(200);
            assertThat(json(r).get("present").asBoolean()).as(value).isTrue();
            assertThat(json(r).get("committed").asBoolean()).as(value).isFalse();
            assertThat(json(r).get("reason").asText()).as(value).contains("readable marker");
            assertThat(json(r).has("marker")).as(value).isFalse();
        }
    }

    @Test
    void aFileMissingAColumnTheTargetRequiresIsRefusedNamingTheColumn() throws Exception {
        String token = env.newTenant("t-imp-missing");
        HttpResponse<String> r = env.open(token, file.withoutColumn("memory", "title").seal());
        assertThat(r.statusCode()).isEqualTo(422);
        assertThat(json(r).get("error").asText()).contains("memory.title").contains("NOT NULL");
    }

    @Test
    void anOlderFileLoadsAndTheColumnsItLacksTakeTheirDefaults() throws Exception {
        String tenant = "t-imp-older";
        String token = env.newTenant(tenant);
        assertThat((long) env.<Integer>su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "memory")),
            DSL.field(DSL.name("tenant_id"), String.class).eq(SRC).and(DSL.field(DSL.name("access_count"), Integer.class).eq(5)))))
            .as("the source's rows carry access_count 5 (the control)").isEqualTo(env.count("memory", SRC));

        JsonNode end = env.restore(token, file.withoutColumn("memory", "access_count").seal());

        assertThat(end.get("status").asText()).isEqualTo("committed");
        assertThat(env.count("memory", tenant)).isEqualTo(env.count("memory", SRC));
        assertThat((long) env.<Integer>su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "memory")),
            DSL.field(DSL.name("tenant_id"), String.class).eq(tenant).and(DSL.field(DSL.name("access_count"), Integer.class).eq(0)))))
            .as("every restored row took the column's default").isEqualTo(env.count("memory", tenant));
    }

    @Test
    void aMissingEmbeddingModelIsRefusedBeforeAnyWriteNamingTheModel() throws Exception {
        String tenant = "t-imp-nomodel";
        String token = env.newTenant(tenant);
        ImportTestEnv.BackupFile f = file.copy();
        f.header.withArray("embedding_models").addObject().put("embedding_model", "no-such-model-v9");
        f.seal();

        HttpResponse<String> r = env.open(token, f);

        assertThat(r.statusCode()).isEqualTo(422);
        assertThat(json(r).get("reason").asText()).isEqualTo("missing_embedding_model");
        assertThat(json(r).get("error").asText()).contains("no-such-model-v9");
        assertNothingWritten(tenant);
        openOk(token, file);   // no session was opened
    }

    @Test
    void aDeclaredSizeOverTheCapIsRefusedByTheOpen() throws Exception {
        String token = env.newTenant("t-imp-big");
        env.importSessions(TenantImportSessions.Config.defaults().withMaxBytes(1000), List.of());
        HttpResponse<String> r = env.open(token, file);   // declares the file's real row bytes
        assertThat(r.statusCode()).isEqualTo(413);
        assertThat(json(r).get("reason").asText()).isEqualTo("import_too_large");
    }

    @Test
    void aFormatVersionTheEngineDoesNotReadIsRefused() throws Exception {
        String token = env.newTenant("t-imp-format");
        ImportTestEnv.BackupFile f = file.copy();
        f.header.put("format_version", 2);
        HttpResponse<String> r = env.open(token, f.seal());
        assertThat(r.statusCode()).isEqualTo(422);
        assertThat(json(r).get("reason").asText()).isEqualTo("unsupported_format_version");
    }

    @Test
    void anEngineThatDoesNotHoldTheSingletonLockRefusesToOpenNamingTheLock() throws Exception {
        String token = env.newTenant("t-imp-nolock");
        env.service.importWiring().declareSingletonLockHeld(false);
        try {
            HttpResponse<String> r = env.open(token, file);
            assertThat(r.statusCode()).isEqualTo(503);
            assertThat(json(r).get("reason").asText()).isEqualTo("singleton_lock_not_held");
            assertThat(json(r).get("error").asText()).contains("singleton lock");
        } finally {
            env.service.importWiring().declareSingletonLockHeld(true);
        }
        openOk(token, file);   // nothing was opened or held back
    }

    // ── sessions: one per tenant, who may see one ───────────────────────────────────────────────

    @Test
    void anOpenWhileTheTenantsEarlierSessionIsOpenAnswers409WithItsIdWhichDeleteCloses() throws Exception {
        String token = env.newTenant("t-imp-twice");
        String first = openOk(token, file).get("session_id").asText();

        HttpResponse<String> again = env.open(token, file);
        assertThat(again.statusCode()).isEqualTo(409);
        assertThat(json(again).get("reason").asText()).isEqualTo("session_exists");
        assertThat(json(again).get("session_id").asText()).isEqualTo(first);
        assertThat(json(again).get("session_kind").asText()).isEqualTo("import");
        HttpResponse<String> exportOpen = env.call("POST", "/v1/tenant/export", token);
        assertThat(exportOpen.statusCode()).as("an export shares the rule").isEqualTo(409);
        assertThat(json(exportOpen).get("session_id").asText()).as("the import session's id").isEqualTo(first);
        assertThat(json(exportOpen).get("session_kind").asText()).as("which routes close it").isEqualTo("import");
        assertThat(env.call("DELETE", "/v1/tenant/export/" + first, token).statusCode())
            .as("the export route cannot close an import session: the kind says where to send the DELETE").isEqualTo(404);

        assertThat(env.call("DELETE", IMPORT + "/" + first, token).statusCode()).isEqualTo(200);
        openOk(token, file);
    }

    @Test
    void anOpenExportBlocksAnImportOfTheSameTenantAndTheEngineCapIsShared() throws Exception {
        String a = env.newTenant("t-imp-cap-a");
        String b = env.newTenant("t-imp-cap-b");
        env.exportSessions(dev.nexus.service.db.TenantExportSessions.Config.defaults().withMaxSessions(1), java.time.Clock.systemUTC());
        assertThat(env.call("POST", "/v1/tenant/export", a).statusCode()).isEqualTo(200);

        HttpResponse<String> same = env.open(a, file);
        assertThat(same.statusCode()).isEqualTo(409);
        assertThat(json(same).has("session_id")).isTrue();
        assertThat(json(same).get("session_kind").asText()).isEqualTo("export");
        HttpResponse<String> other = env.open(b, file);
        assertThat(other.statusCode()).as("the engine's one place is the export's").isEqualTo(503);
        assertThat(json(other).get("reason").asText()).isEqualTo("import_capacity_full");
        assertThat(other.headers().firstValue("Retry-After")).isPresent();
    }

    @Test
    void aTenantResetEndsTheImportSessionAndFreesItsPlace() throws Exception {
        String tenant = "t-imp-reset";
        String token = env.newTenant(tenant);
        String id = openOk(token, file).get("session_id").asText();
        var evictor = env.service.tenantKeyedState().stream().filter(s -> s.name().equals("TenantImportSessions")).findFirst();
        assertThat(evictor).isPresent();

        assertThat(evictor.get().evictTenant("t-imp-someone-else")).isZero();
        assertThat(evictor.get().evictTenant(tenant)).isEqualTo(1);

        assertThat(evictor.get().evictTenant(tenant)).as("and again: nothing left").isZero();
        assertThat(env.call("GET", IMPORT + "/" + id, token).statusCode()).as("the reset also drops the recorded outcome").isEqualTo(404);
        assertThat(env.open(token, file).statusCode()).as("the tenant's place is free").isEqualTo(200);
    }

    @Test
    void anotherTenantsTokenOnEveryRouteOfASessionGetsNoSuchSession() throws Exception {
        String owner = env.newTenant("t-imp-owner");
        String stranger = env.newTenant("t-imp-stranger");
        JsonNode opened = openOk(owner, file);
        String id = opened.get("session_id").asText();
        String firstTable = opened.get("tables").get(0).asText();
        byte[] part = BinaryCopyTestSupport.split(file.pages.get(firstTable).get(0), 100).get(0);

        for (HttpResponse<String> r : List.of(
                env.call("GET", IMPORT + "/" + id, stranger),
                env.call("DELETE", IMPORT + "/" + id, stranger),
                env.call("POST", IMPORT + "/" + id + "/commit", stranger),
                env.call("POST", IMPORT + "/" + id + "/close?table=" + firstTable + "&rows=0", stranger),
                env.putPart(stranger, id, firstTable, 0, part, sha256(part)),
                env.call("GET", IMPORT + "/00000000-0000-4000-8000-000000000000", owner),
                env.call("GET", IMPORT + "/not-a-uuid", owner))) {
            assertThat(r.statusCode()).as(r.body()).isEqualTo(404);
            assertThat(json(r).get("reason").asText()).isEqualTo("no_such_session");
        }
        // and a root token is not a tenant's
        assertThat(env.call("POST", IMPORT, ImportTestEnv.ROOT).statusCode()).isEqualTo(403);
        assertThat(env.call("GET", IMPORT + "/" + id, owner).statusCode()).as("the owner's session is untouched").isEqualTo(200);
    }

    @Test
    void anExpiredTokenMidUploadGets401AndANewTokenOfTheSameTenantContinuesTheSession() throws Exception {
        String tenant = "t-imp-expiry";
        String live = env.newTenant(tenant);
        String shortLived = "tok-short-" + tenant + "-0123456789abcdefghijklmnopqrstuvwxyz";
        env.su(ctx -> {
            PgContainerHelper.seedServiceToken(ctx, shortLived, tenant, "short", TokenStore.SCOPE_DATA,
                OffsetDateTime.now().plusSeconds(10), null);
            return null;
        });
        JsonNode opened = openOk(shortLived, file);
        String id = opened.get("session_id").asText();
        String firstTable = opened.get("tables").get(0).asText();
        env.uploadTable(shortLived, id, file, firstTable, 100);

        long deadline = System.nanoTime() + Duration.ofSeconds(90).toNanos();
        while (env.call("GET", IMPORT + "/" + id, shortLived).statusCode() != 401) {
            assertThat(System.nanoTime()).as("the token expired").isLessThan(deadline);
            Thread.sleep(250);
        }
        assertThat(env.call("GET", IMPORT + "/" + id, live).statusCode()).as("the session is the tenant's, not the token's").isEqualTo(200);
        for (int i = 1; i < opened.get("tables").size(); i++) env.uploadTable(live, id, file, opened.get("tables").get(i).asText(), 100);
        assertThat(commitAndAwait(live, id)).isEqualTo("committed");
        assertThat(env.count("memory", tenant)).isEqualTo(env.count("memory", SRC));
    }

    // ── the part protocol ───────────────────────────────────────────────────────────────────────

    @Test
    void aPartSentTwiceStagesOnceAWrongHashAndAGapAreRefusedAndTheResendStagesOnce() throws Exception {
        String tenant = "t-imp-parts";
        String token = env.newTenant(tenant);
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        uploadBefore(token, id, opened, "memory");
        List<byte[]> parts = BinaryCopyTestSupport.split(file.pages.get("memory").get(0), 1);
        assertThat(parts.size()).as("one row to a part").isEqualTo((int) file.rows("memory")).isGreaterThan(2);

        HttpResponse<String> first = env.putPart(token, id, "memory", 0, parts.get(0), sha256(parts.get(0)));
        assertThat(json(first).get("staged").asBoolean()).isTrue();
        HttpResponse<String> repeat = env.putPart(token, id, "memory", 0, parts.get(0), sha256(parts.get(0)));
        assertThat(repeat.statusCode()).isEqualTo(200);
        assertThat(json(repeat).get("repeat").asBoolean()).as("a repeat of the last part stages nothing").isTrue();
        HttpResponse<String> other = env.putPart(token, id, "memory", 0, parts.get(1), sha256(parts.get(1)));
        assertThat(other.statusCode()).isEqualTo(409);
        assertThat(json(other).get("reason").asText()).isEqualTo("part_sha_conflict");
        HttpResponse<String> gap = env.putPart(token, id, "memory", 2, parts.get(2), sha256(parts.get(2)));
        assertThat(gap.statusCode()).isEqualTo(409);
        assertThat(json(gap).get("reason").asText()).isEqualTo("part_gap");
        HttpResponse<String> cut = env.putPart(token, id, "memory", 1, parts.get(1), sha256(parts.get(0)));   // a body that is not the hashed one
        assertThat(cut.statusCode()).isEqualTo(400);
        assertThat(json(cut).get("reason").asText()).isEqualTo("part_sha256_mismatch");
        assertThat(env.putPart(token, id, "memory", 1, parts.get(1), null).statusCode()).as("the hash is required").isEqualTo(400);
        for (int k = 1; k < parts.size(); k++) {
            assertThat(env.putPart(token, id, "memory", k, parts.get(k), sha256(parts.get(k))).statusCode()).isEqualTo(200);
        }
        HttpResponse<String> behind = env.putPart(token, id, "memory", 0, parts.get(0), sha256(parts.get(0)));
        assertThat(behind.statusCode()).as("a part before the last staged one, though it was staged once").isEqualTo(409);
        assertThat(json(behind).get("reason").asText()).isEqualTo("part_already_staged");
        HttpResponse<String> badClose = env.call("POST", IMPORT + "/" + id + "/close?table=memory&rows=" + (parts.size() + 1), token);
        assertThat(badClose.statusCode()).isEqualTo(409);
        assertThat(json(badClose).get("reason").asText()).isEqualTo("row_count_mismatch");
        assertThat(json(badClose).get("staged").asLong()).as("staged once each").isEqualTo(parts.size());
        assertThat(json(badClose).get("expected").asLong()).isEqualTo(parts.size() + 1);
        assertThat(env.call("POST", IMPORT + "/" + id + "/close?table=memory&rows=" + parts.size(), token).statusCode()).isEqualTo(200);
        assertThat(env.putPart(token, id, "memory", parts.size(), parts.get(0), sha256(parts.get(0))).statusCode())
            .as("a closed table takes no part").isEqualTo(409);
        boolean after = false;
        for (JsonNode t : opened.get("tables")) {
            if (after) env.uploadTable(token, id, file, t.asText(), 100);
            if (t.asText().equals("memory")) after = true;
        }

        assertThat(commitAndAwait(token, id)).isEqualTo("committed");
        assertThat(env.count("memory", tenant)).as("memory rows are not doubled by the resends").isEqualTo(env.count("memory", SRC));
    }

    @Test
    void aTableOutOfOrderIsRefused() throws Exception {
        String token = env.newTenant("t-imp-order");
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        byte[] part = BinaryCopyTestSupport.split(file.pages.get("memory").get(0), 100).get(0);
        HttpResponse<String> r = env.putPart(token, id, "memory", 0, part, sha256(part));
        assertThat(r.statusCode()).isEqualTo(409);
        assertThat(json(r).get("reason").asText()).isEqualTo("table_out_of_order");
        HttpResponse<String> commit = env.call("POST", IMPORT + "/" + id + "/commit", token);
        assertThat(commit.statusCode()).as("every table must be closed").isEqualTo(409);
        assertThat(json(commit).get("reason").asText()).isEqualTo("tables_open");
    }

    @Test
    void aMalformedPartWhoseHashMatchesFailsTheSessionAndFreesItsPlace() throws Exception {
        String tenant = "t-imp-malformed";
        String token = env.newTenant(tenant);
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        uploadBefore(token, id, opened, "memory");
        byte[] garbage = "this is not a binary COPY stream at all".getBytes(StandardCharsets.UTF_8);

        HttpResponse<String> r = env.putPart(token, id, "memory", 0, garbage, sha256(garbage));

        assertThat(r.statusCode()).isEqualTo(422);
        assertThat(json(r).get("reason").asText()).isEqualTo("part_failed");
        JsonNode status = json(env.call("GET", IMPORT + "/" + id, token));
        assertThat(status.get("state").asText()).isEqualTo("failed");
        assertThat(status.get("status").asText()).startsWith("failed: part memory/0:");
        assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(200);   // the outcome, not a load
        assertNothingWritten(tenant);
        assertThat(env.ds.getHikariPoolMXBean().getActiveConnections()).as("the held connection is gone").isZero();
        openOk(token, file);   // the tenant's place is free: start over
    }

    @Test
    void aPartSentWhileTheSamePartIsStillInFlightAnswers409PartInFlight() throws Exception {
        String token = env.newTenant("t-imp-inflight");
        var sessions = env.importSessions(TenantImportSessions.Config.defaults(), List.of());
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        String first = opened.get("tables").get(0).asText();
        byte[] part = BinaryCopyTestSupport.split(file.pages.get(first).get(0), 100).get(0);
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        sessions.partHookForTests(table -> {
            inside.countDown();
            try {
                release.await(60, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        });
        CompletableFuture<HttpResponse<String>> running = CompletableFuture.supplyAsync(() -> {
            try {
                return env.putPart(token, id, first, 0, part, sha256(part));
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        assertThat(inside.await(60, TimeUnit.SECONDS)).isTrue();

        HttpResponse<String> second = env.putPart(token, id, first, 0, part, sha256(part));

        assertThat(second.statusCode()).isEqualTo(409);
        assertThat(json(second).get("reason").asText()).isEqualTo("part_in_flight");
        assertThat(second.headers().firstValue("Retry-After")).contains("1");
        HttpResponse<String> close = env.call("POST", IMPORT + "/" + id + "/close?table=" + first + "&rows=" + file.rows(first), token);
        assertThat(close.statusCode()).as("a close must not race a part of the same table").isEqualTo(409);
        assertThat(json(close).get("reason").asText()).isEqualTo("part_in_flight");
        release.countDown();
        assertThat(running.get(60, TimeUnit.SECONDS).statusCode()).isEqualTo(200);
        sessions.partHookForTests(null);
    }

    @Test
    void partsPastTheByteCapAreRefusedWhateverTheOpenDeclared() throws Exception {
        String token = env.newTenant("t-imp-cap-bytes");
        env.importSessions(TenantImportSessions.Config.defaults().withMaxBytes(20_000), List.of());
        ImportTestEnv.BackupFile small = file.copy();
        small.header.put("row_bytes", 100);   // the file declares far less than it carries
        small.header.put("header_sha256", file.headerSha());
        JsonNode opened = openOk(token, small);
        String id = opened.get("session_id").asText();
        for (JsonNode t : opened.get("tables")) {
            if (t.asText().equals("chunks")) break;
            env.uploadTable(token, id, file, t.asText(), 100);
        }
        byte[] chunks = BinaryCopyTestSupport.split(file.pages.get("chunks").get(0), 100).get(0);
        assertThat(chunks.length).as("the chunks part is past the cap").isGreaterThan(20_000);

        HttpResponse<String> r = env.putPart(token, id, "chunks", 0, chunks, sha256(chunks));

        assertThat(r.statusCode()).isEqualTo(413);
        assertThat(json(r).get("reason").asText()).isEqualTo("import_too_large");
    }

    @Test
    void aPartOverEightMebibytesIsRefused() throws Exception {
        String token = env.newTenant("t-imp-bigpart");
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        byte[] big = new byte[TenantImportSessions.PART_BYTES + 1];
        HttpResponse<String> r = env.putPart(token, id, opened.get("tables").get(0).asText(), 0, big, sha256(big));
        assertThat(r.statusCode()).isEqualTo(413);
        assertThat(json(r).get("reason").asText()).isEqualTo("part_too_large");
    }

    // ── commit: repeats, a dropped client, a restart ────────────────────────────────────────────

    @Test
    void aRepeatedCommitAnswersTheStatusAndStartsNoSecondLoad() throws Exception {
        String tenant = "t-imp-recommit";
        String token = env.newTenant(tenant);
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        env.upload(token, id, file, opened, 100);
        assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(202);

        HttpResponse<String> again = env.call("POST", IMPORT + "/" + id + "/commit", token);   // the 202 was lost: just commit again
        assertThat(again.statusCode()).isEqualTo(200);
        env.awaitEnd(token, id, Duration.ofMinutes(2));
        HttpResponse<String> after = env.call("POST", IMPORT + "/" + id + "/commit", token);
        assertThat(after.statusCode()).as("after it ended too").isEqualTo(200);
        assertThat(json(after).get("status").asText()).isEqualTo("committed");
        assertThat(env.count("memory", tenant)).as("loaded once").isEqualTo(env.count("memory", SRC));
    }

    @Test
    void aClientThatDropsDuringTheCommitDoesNotStopTheLoadAndTheStatusReportsIt() throws Exception {
        String tenant = "t-imp-dropped";
        String token = env.newTenant(tenant);
        JsonNode opened = openOk(token, file);
        String id = opened.get("session_id").asText();
        env.upload(token, id, file, opened, 100);

        try (Socket s = new Socket("127.0.0.1", env.service.getPort())) {
            OutputStream out = s.getOutputStream();
            out.write(("POST " + IMPORT + "/" + id + "/commit HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer " + token
                + "\r\nContent-Length: 0\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
            out.flush();
        }   // closed without reading the answer

        JsonNode end = env.awaitEnd(token, id, Duration.ofMinutes(2));
        assertThat(end.get("status").asText()).isEqualTo("committed");
        assertThat(env.count("memory", tenant)).isEqualTo(env.count("memory", SRC));
    }

    @Test
    void afterAnEngineRestartTheMarkerNamesTheFileThatCommittedAndNoOtherLoad() throws Exception {
        String done = "t-imp-marker-done";
        String doneToken = env.newTenant(done);
        String rolledBack = "t-imp-marker-rolledback";
        String rolledBackToken = env.newTenant(rolledBack);
        assertThat(env.restore(doneToken, file).get("status").asText()).isEqualTo("committed");
        // a load that rolled back: a write landed during its upload
        JsonNode opened = openOk(rolledBackToken, file);
        String id = opened.get("session_id").asText();
        env.upload(rolledBackToken, id, file, opened, 100);
        env.su(ctx -> ctx.insertInto(DSL.table(DSL.name("nexus", "memory")),
                DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("project")), DSL.field(DSL.name("title")),
                DSL.field(DSL.name("content")), DSL.field(DSL.name("timestamp")))
            .values(rolledBack, "p", "w", "c", OffsetDateTime.now()).execute());
        assertThat(commitAndAwait(rolledBackToken, id)).startsWith("failed:");

        env.importSessions(TenantImportSessions.Config.defaults(), List.of());   // the engine restarts: the memory of sessions is gone

        assertThat(env.call("GET", IMPORT + "/" + id, rolledBackToken).statusCode()).isEqualTo(404);
        JsonNode marker = json(env.call("GET", "/v1/tenant/import-marker", doneToken));
        assertThat(marker.get("present").asBoolean()).isTrue();
        assertThat(marker.get("marker").get("header_sha256").asText()).as("the load committed exactly when the marker names this file")
            .isEqualTo(file.headerSha());
        assertThat(marker.get("marker").get("row_counts").get("memory").asLong()).isEqualTo(env.count("memory", SRC));
        assertThat(json(env.call("GET", "/v1/tenant/import-marker", rolledBackToken)).get("present").asBoolean())
            .as("no marker: the load rolled back").isFalse();
        assertThat(json(env.call("GET", "/v1/tenant/import-marker", env.newTenant("t-imp-marker-none"))).get("present").asBoolean()).isFalse();
    }

    @Test
    void theStatusOfAFinishedImportIsKeptAndReportsTheLoad() throws Exception {
        String token = env.newTenant("t-imp-status");
        JsonNode end = env.restore(token, file);
        assertThat(end.get("state").asText()).isEqualTo("committed");
        ObjectNode report = (ObjectNode) end.get("report");
        assertThat(report.get("rows_inserted").asLong()).isPositive();
        assertThat(report.get("tables").size()).isEqualTo(26);
        String id = end.get("session_id").asText();
        assertThat(json(env.call("GET", IMPORT + "/" + id, token)).get("status").asText()).isEqualTo("committed");
        assertThat(env.call("DELETE", IMPORT + "/" + id, token).statusCode()).as("an ended session cannot be aborted").isEqualTo(404);
    }
}
