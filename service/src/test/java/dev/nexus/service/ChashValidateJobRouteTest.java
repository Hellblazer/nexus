// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.ChashConformanceConstraints;
import dev.nexus.service.db.ChashConstraintValidator;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

/**
 * nexus-f3yqd, engine 0.1.190: {@code POST /v1/admin/validate-constraints} and {@code GET .../{id}} through the real
 * {@link NexusService} and its auth filter, on a real PostgreSQL whose {@code nexus.chunks} is the real partition tree
 * (model, then tenant). Every test starts from the state the migrations leave (both CHECKs NOT VALID, and the copies on
 * every partition) and plants what it needs. The role and row-level-security side (a table owner that is not a superuser)
 * is {@code ChashConstraintValidatorOwnerRoleTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashValidateJobRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String ROOT = "tok-root-chash-val-0123456789abcdef";
    private static final String TENANT_TOKEN = "tok-tenant-chash-val-0123456789abcd";
    private static final String TENANT_A = "t-chashval-a";
    private static final String TENANT_B = "t-chashval-b";
    private static final String COLLECTION = "knowledge__chashval__bge-base-en-v15-768__v1";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final String CHUNKS_CHECK = "chunks_chash_octet_check";
    private static final String MANIFEST_CHECK = "catalog_document_chunks_chash_octet_check";
    private static final String PATH = "/v1/admin/validate-constraints";
    /** The pre-RDR-180 width: 16 bytes, which the CHECK refuses. */
    private static final String LEGACY_HEX = "a".repeat(32);

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private NexusService service;
    private final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, ROOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TENANT_TOKEN, TENANT_A, "tenant-token");
            PgContainerHelper.seedServiceToken(dsl, "tok-b-chash-val-0123456789abcdefgh", TENANT_B, "tenant-b-token");
            PgContainerHelper.ensureTenantPartitions(dsl, TENANT_A);
            PgContainerHelper.ensureTenantPartitions(dsl, TENANT_B);
            // the manifest's FK to its chunk would make a 16-byte manifest row need a 16-byte chunk beside it, and this
            // class seeds each table's bad row alone
            dsl.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("fk_catalog_chunks_chunk").execute();
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        service = new NexusService(0, ROOT, ds);
        // the admin connection: the test database's owner (a superuser here; the non-superuser owner is the other class's)
        service.chashConstraintValidator(new ChashConstraintValidator(
            new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword())));
        service.start();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void fresh() throws Exception {
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "200");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "3");
        System.setProperty(ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, "20");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraintIfExists(CHUNKS_CHECK).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(MANIFEST_CHECK).execute();
            ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
            ctx.deleteFrom(CHUNKS).execute();
            PgContainerHelper.readdChashOctetChecksNotValid(su);
        }
    }

    @AfterEach
    void clearProperties() {
        for (String p : new String[]{ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY,
            ChashConstraintValidator.RETRY_BACKOFF_PROPERTY}) {
            System.clearProperty(p);
        }
    }

    // ── fixtures ────────────────────────────────────────────────────────────

    private static byte[] sha256(String text) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    private static void insertChunk(DSLContext ctx, String tenant, byte[] chash, String text) {
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant)
            .set(CHUNKS.COLLECTION, COLLECTION)
            .set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CHUNKS.CHUNK_TEXT, text)
            .set(CHUNKS.EMBEDDING_768, Vector.of(new float[768]))
            .execute();
    }

    private static void insertManifest(DSLContext ctx, String tenant, String docId, byte[] chash) {
        ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, docId)
            .set(CATALOG_DOCUMENTS.TITLE, "doc " + docId)
            .onConflictDoNothing()
            .execute();
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, docId)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLLECTION)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .execute();
    }

    /** Valid rows for two tenants in both tables, so each tenant's leaf of the model partition holds data. */
    private void seedValid() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String tenant : new String[]{TENANT_A, TENANT_B}) {
                PgContainerHelper.insertCollection(ctx, tenant, COLLECTION);
                insertChunk(ctx, tenant, sha256("clean one " + tenant), "clean one");
                insertChunk(ctx, tenant, sha256("clean two " + tenant), "clean two");
                insertManifest(ctx, tenant, "1.1", sha256("manifest clean " + tenant));
            }
        }
    }

    /**
     * One 16-byte chash in {@code table}'s rows for {@code tenant}: the CHECKs are dropped, the row seeded and the CHECKs
     * re-added NOT VALID (a NOT VALID CHECK is still enforced on new rows, so this is the only way to get one in).
     */
    private void seedBad(String tenant, boolean intoChunks, boolean intoManifest) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraint(CHUNKS_CHECK).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraint(MANIFEST_CHECK).execute();
            try {
                byte[] legacy = HexFormat.of().parseHex(LEGACY_HEX);
                if (intoChunks) insertChunk(ctx, tenant, legacy, "poisoned legacy row");
                if (intoManifest) insertManifest(ctx, tenant, "9.9", legacy);
            } finally {
                PgContainerHelper.readdChashOctetChecksNotValid(su);
            }
        }
    }

    /** {@code n} chunks of {@code tenant} whose chash is 16 bytes, each a different one. */
    private void seedBadChunks(String tenant, int n) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraint(CHUNKS_CHECK).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraint(MANIFEST_CHECK).execute();
            try {
                for (int i = 0; i < n; i++) {
                    byte[] legacy = HexFormat.of().parseHex(LEGACY_HEX.substring(0, 30) + String.format("%02x", i));
                    insertChunk(ctx, tenant, legacy, "poisoned legacy row " + i);
                }
            } finally {
                PgContainerHelper.readdChashOctetChecksNotValid(su);
            }
        }
    }

    /** Replaces the bad chunk with a 32-byte one: what a user does before the rerun. */
    private void fixBadChunk(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.CHASH, sha256("repaired " + tenant))
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHUNK_TEXT.eq("poisoned legacy row")).execute();
        }
    }

    /** The relation name of the leaf of {@code nexus.chunks} that holds {@code tenant}'s row with this text. */
    private String leafOf(String tenant, String text) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            Long oid = ctx.select(DSL.field(DSL.name("chunks", "tableoid"), SQLDataType.BIGINT)).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.CHUNK_TEXT.eq(text)).limit(1).fetchOne(0, Long.class);
            assertThat(oid).as("a row of " + tenant + " with text " + text).isNotNull();
            return ctx.select(DSL.field(DSL.name("c", "relname"), String.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .where(DSL.field(DSL.name("c", "oid")).cast(SQLDataType.BIGINT).eq(oid)).fetchOne(0, String.class);
        }
    }

    /** relname to convalidated for every row of the target constraint names (the chunks tree and the manifest). */
    private Map<String, Boolean> validity() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Map<String, Boolean> out = new java.util.LinkedHashMap<>();
            for (var r : ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch()) {
                if (!"chunks_retired_225".equals(r.value3())) out.put(r.value3(), r.value5());
            }
            return out;
        }
    }

    /** The relation names of the partitions directly under {@code nexus.chunks} (the model partitions). */
    private List<String> modelPartitions() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rows = ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch();
            Long chunksOid = rows.stream().filter(r -> r.value2() == 0 && "chunks".equals(r.value3()))
                .map(r -> r.value1()).findFirst().orElseThrow();
            return rows.stream().filter(r -> chunksOid.equals(r.value2())).map(r -> r.value3()).toList();
        }
    }

    // ── http ────────────────────────────────────────────────────────────────

    private String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    private HttpResponse<String> post(String bearer, String body) throws Exception {
        var b = TestHttp.request(url(PATH)).header("Content-Type", "application/json");
        if (bearer != null) b.header("Authorization", "Bearer " + bearer);
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String bearer, String path) throws Exception {
        var b = TestHttp.request(url(path));
        if (bearer != null) b.header("Authorization", "Bearer " + bearer);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    private String start(boolean dryRun) throws Exception {
        var resp = post(ROOT, "{\"dry_run\":" + dryRun + "}");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(202);
        return MAPPER.readTree(resp.body()).get("job_id").asText();
    }

    private JsonNode status(String jobId) throws Exception {
        var resp = get(ROOT, PATH + "/" + jobId);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    /** Polls until the job leaves {@code running}. */
    private JsonNode await(String jobId) throws Exception {
        long deadline = System.nanoTime() + java.time.Duration.ofSeconds(120).toNanos();
        while (System.nanoTime() < deadline) {
            JsonNode s = status(jobId);
            if (!"running".equals(s.get("state").asText())) return s;
            Thread.sleep(25);
        }
        throw new AssertionError("job " + jobId + " did not finish: " + status(jobId));
    }

    private JsonNode run(boolean dryRun) throws Exception {
        return await(start(dryRun));
    }

    private JsonNode conformance() throws Exception {
        var resp = get(ROOT, "/v1/schema/chash-conformance");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    private static List<String> strings(JsonNode array) {
        List<String> out = new ArrayList<>();
        array.forEach(n -> out.add(n.asText()));
        return out;
    }

    private static List<String> tables(JsonNode entries) {
        List<String> out = new ArrayList<>();
        entries.forEach(n -> out.add(n.get("table").asText()));
        return out;
    }

    // ── locks a test holds ──────────────────────────────────────────────────

    /** An ACCESS EXCLUSIVE lock on {@code relname}, held by an uncommitted rename (DDL in an open transaction). */
    private Connection holdAccessExclusive(String relname) throws Exception {
        Connection c = pg.createConnection("");
        c.setAutoCommit(false);
        DSL.using(c, SQLDialect.POSTGRES).alterTable(DSL.table(DSL.name("nexus", relname)))
            .renameTo(DSL.name(relname + "_held")).execute();
        return c;
    }

    /** A SHARE lock on {@code relname} (an index build in an open transaction): reads and writes pass, a VALIDATE waits. */
    private Connection holdShare(String relname) throws Exception {
        Connection c = pg.createConnection("");
        c.setAutoCommit(false);
        DSL.using(c, SQLDialect.POSTGRES).createIndex(DSL.name("held_" + relname + "_idx"))
            .on(DSL.table(DSL.name("nexus", relname)), DSL.field(DSL.name("chash"))).execute();
        return c;
    }

    private static void release(Connection c) throws Exception {
        c.rollback();
        c.close();
    }

    // ── what a clean store does ─────────────────────────────────────────────

    @Test
    void aStoreWithValidRows_validatesEveryLeafThenTheParent_andTheConformanceRouteReadsAllValidated() throws Exception {
        seedValid();
        assertThat(conformance().get("all_validated").asBoolean()).as("the migrations leave both CHECKs NOT VALID").isFalse();

        JsonNode done = run(false);
        System.out.println("clean run: " + done);

        assertThat(done.get("state").asText()).as(done.toString()).isEqualTo("done");
        assertThat(done.get("refused")).isEmpty();
        assertThat(done.get("deferred")).isEmpty();
        assertThat(done.get("tables_done").asInt()).isEqualTo(done.get("tables_total").asInt());
        assertThat(done.get("all_validated").asBoolean()).isTrue();
        assertThat(conformance().get("all_validated").asBoolean()).isTrue();
        assertThat(validity().values()).as("every copy and both parents, read from pg_constraint").doesNotContain(false);

        List<String> validated = strings(done.get("validated"));
        assertThat(validated).contains("nexus.chunks", "nexus.catalog_document_chunks");
        int partitions = conformance().get("constraints").get(0).get("partitions").asInt();
        assertThat(partitions).isPositive();
        assertThat(validated).as("the parent, every copy below it and the manifest").hasSize(partitions + 2);
        // deepest first: no relation is validated before every copy below it; the parent is last of its tree
        int parentAt = validated.indexOf("nexus.chunks");
        for (String name : validated) {
            if (!name.equals("nexus.chunks") && !name.equals("nexus.catalog_document_chunks")) {
                assertThat(validated.indexOf(name)).as(name + " before the parent").isLessThan(parentAt);
            }
        }
        // the model partitions (one level below the parent) come after every tenant leaf below them
        List<String> modelPartitions = modelPartitions();
        assertThat(modelPartitions).as("nexus.chunks has model partitions").isNotEmpty();
        int lastLeaf = -1;
        int firstModel = Integer.MAX_VALUE;
        for (String name : validated) {
            String bare = name.substring("nexus.".length());
            if (modelPartitions.contains(bare)) firstModel = Math.min(firstModel, validated.indexOf(name));
            else if (!bare.equals("chunks") && !bare.equals("catalog_document_chunks")) lastLeaf = Math.max(lastLeaf, validated.indexOf(name));
        }
        assertThat(lastLeaf).as("leaves exist").isNotNegative();
        assertThat(lastLeaf).as("every leaf before every model partition").isLessThan(firstModel);
        assertThat(done.get("on_stop").asText()).contains("stays validated").contains("resumes");
        assertThat(done.get("safe_to_stop").asBoolean()).isTrue();
    }

    @Test
    void aBadChashInOneTenantsLeaf_refusesThatTableOnly_namesItsCountAndAnExample_andValidatesTheRest() throws Exception {
        seedValid();
        seedBad(TENANT_A, true, false);
        String badLeaf = leafOf(TENANT_A, "poisoned legacy row");

        JsonNode done = run(false);
        System.out.println("one bad leaf: " + done);

        assertThat(done.get("state").asText()).as("a refusal is a finished job, not a failed one").isEqualTo("done");
        assertThat(done.get("refused")).hasSize(1);
        JsonNode refusal = done.get("refused").get(0);
        assertThat(refusal.get("table").asText()).isEqualTo("nexus." + badLeaf);
        assertThat(refusal.get("constraint").asText()).isEqualTo(CHUNKS_CHECK);
        assertThat(refusal.get("reason").asText()).isEqualTo("bad_rows");
        assertThat(refusal.get("bad_rows").asLong()).isEqualTo(1);
        assertThat(refusal.get("bad_rows_capped").asBoolean()).isFalse();
        assertThat(strings(refusal.get("example_chashes"))).containsExactly(LEGACY_HEX);
        assertThat(refusal.get("precheck").asText()).isEqualTo("bad_rows");
        assertThat(done.get("all_validated").asBoolean()).isFalse();

        Map<String, Boolean> validity = validity();
        assertThat(validity.get(badLeaf)).as("the refused leaf stays NOT VALID").isFalse();
        assertThat(validity.get("catalog_document_chunks")).as("the other table is validated").isTrue();
        // every other leaf is valid; what is not is the refused leaf and the relations above it (the model partition and the parent)
        List<String> notValid = validity.entrySet().stream().filter(e -> !e.getValue()).map(Map.Entry::getKey).toList();
        assertThat(notValid).contains(badLeaf, "chunks");
        assertThat(notValid.size()).as("the refused leaf, its model partition and the parent: " + notValid).isEqualTo(3);
        // the ancestors are deferred, not refused and not attempted
        List<String> deferred = tables(done.get("deferred"));
        assertThat(deferred).contains("nexus.chunks").hasSize(2);
        for (JsonNode d : done.get("deferred")) {
            assertThat(d.get("reason").asText()).isEqualTo("descendant_not_validated");
        }
        assertThat(tables(done.get("refused"))).doesNotContain("nexus.chunks");
    }

    @Test
    void theBadRowCountStopsAtTheCap_andSaysSo() throws Exception {
        seedValid();
        System.setProperty(ChashConstraintValidator.PRECHECK_CAP_PROPERTY, "2");
        try {
            seedBadChunks(TENANT_A, 3);
            JsonNode done = run(false);
            assertThat(done.get("refused")).hasSize(1);
            JsonNode refusal = done.get("refused").get(0);
            assertThat(refusal.get("bad_rows").asLong()).as("counted up to the cap, not past it").isEqualTo(2);
            assertThat(refusal.get("bad_rows_capped").asBoolean()).isTrue();
            assertThat(refusal.get("example_chashes")).as("no more examples than the cap").hasSize(2);
        } finally {
            System.clearProperty(ChashConstraintValidator.PRECHECK_CAP_PROPERTY);
        }
    }

    @Test
    void aBadChashInTheManifest_refusesThatTableOnly_andTheChunksTreeValidates() throws Exception {
        seedValid();
        seedBad(TENANT_A, false, true);

        JsonNode done = run(false);

        assertThat(done.get("refused")).hasSize(1);
        JsonNode refusal = done.get("refused").get(0);
        assertThat(refusal.get("table").asText()).isEqualTo("nexus.catalog_document_chunks");
        assertThat(refusal.get("constraint").asText()).isEqualTo(MANIFEST_CHECK);
        assertThat(refusal.get("bad_rows").asLong()).isEqualTo(1);
        assertThat(strings(refusal.get("example_chashes"))).containsExactly(LEGACY_HEX);
        Map<String, Boolean> validity = validity();
        assertThat(validity.get("catalog_document_chunks")).isFalse();
        assertThat(validity.get("chunks")).as("the partitioned table is independent of the manifest").isTrue();
        assertThat(validity.values().stream().filter(v -> !v).count()).isEqualTo(1);
    }

    @Test
    void aRerunAfterFixingTheRow_resumesAndValidatesOnlyWhatIsLeft() throws Exception {
        seedValid();
        seedBad(TENANT_A, true, false);
        String badLeaf = leafOf(TENANT_A, "poisoned legacy row");
        JsonNode first = run(false);
        int validatedFirst = first.get("validated").size();
        assertThat(validatedFirst).isPositive();

        fixBadChunk(TENANT_A);
        JsonNode second = run(false);
        System.out.println("rerun: " + second);

        assertThat(second.get("refused")).isEmpty();
        assertThat(second.get("deferred")).isEmpty();
        assertThat(second.get("all_validated").asBoolean()).isTrue();
        assertThat(second.get("already_valid_count").asInt()).as("what the first run validated is skipped").isEqualTo(validatedFirst);
        List<String> validated = strings(second.get("validated"));
        assertThat(validated).as("the leaf that was refused, its model partition and the parent").hasSize(3)
            .contains("nexus." + badLeaf, "nexus.chunks");
        assertThat(validated).doesNotContainAnyElementsOf(strings(first.get("validated")));
        assertThat(conformance().get("all_validated").asBoolean()).isTrue();
    }

    // ── locks ───────────────────────────────────────────────────────────────

    @Test
    void aLockHeldOnALeaf_makesTheStepRetryThenSucceedWhenReleased() throws Exception {
        seedValid();
        String leaf = leafOf(TENANT_A, "clean one");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "200");
        System.setProperty(ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, "50");
        Connection held = holdShare(leaf);   // a SHARE lock: the pre-check reads, the VALIDATE cannot take its lock
        try {
            String id = start(false);
            // the job reaches the locked leaf and waits there: it is still running after several lock timeouts
            long deadline = System.nanoTime() + java.time.Duration.ofSeconds(60).toNanos();
            JsonNode s;
            do {
                Thread.sleep(25);
                s = status(id);
            } while (System.nanoTime() < deadline && !(s.get("current").isObject()
                && ("nexus." + leaf).equals(s.get("current").get("table").asText())));
            assertThat(s.get("current").isObject()).as("the job is on the locked leaf: " + s).isTrue();
            assertThat(s.get("current").get("bytes").asLong()).as("the current table's size").isPositive();
            Thread.sleep(1000);   // five lock timeouts of 200 ms
            s = status(id);
            assertThat(s.get("state").asText()).as("it retries, it does not give up: " + s).isEqualTo("running");
            assertThat(validity().get(leaf)).isFalse();
            release(held);
            held = null;
            JsonNode done = await(id);
            assertThat(done.get("state").asText()).isEqualTo("done");
            assertThat(done.get("deferred")).as(done.toString()).isEmpty();
            assertThat(done.get("refused")).isEmpty();
            assertThat(done.get("all_validated").asBoolean()).isTrue();
            assertThat(strings(done.get("validated"))).contains("nexus." + leaf);
        } finally {
            if (held != null) release(held);
        }
    }

    @Test
    void anAccessExclusiveLockOnALeaf_makesThePlanReadWait_andTheJobGoesOnWhenItIsReleased() throws Exception {
        seedValid();
        String leaf = leafOf(TENANT_A, "clean one");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "200");
        System.setProperty(ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, "50");
        Connection held = holdAccessExclusive(leaf);
        try {
            String id = start(false);
            Thread.sleep(1000);
            JsonNode s = status(id);
            assertThat(s.get("state").asText()).as("the read of pg_constraint waits behind the lock: " + s).isEqualTo("running");
            assertThat(s.get("tables_total").asInt()).isZero();
            release(held);
            held = null;
            JsonNode done = await(id);
            assertThat(done.get("state").asText()).isEqualTo("done");
            assertThat(done.get("all_validated").asBoolean()).isTrue();
        } finally {
            if (held != null) release(held);
        }
    }

    @Test
    void anAccessExclusiveLockHeldPastTheBound_failsTheJobAsTransient_changingNothing_andARerunFinishes() throws Exception {
        seedValid();
        String leaf = leafOf(TENANT_A, "clean one");
        Connection held = holdAccessExclusive(leaf);
        JsonNode failed;
        try {
            failed = run(false);
        } finally {
            release(held);
        }
        assertThat(failed.get("state").asText()).isEqualTo("failed");
        assertThat(failed.get("http_status").asInt()).isEqualTo(503);
        assertThat(failed.get("reason").asText()).isEqualTo("lock_timeout");
        assertThat(failed.get("validated")).isEmpty();
        assertThat(validity().values()).as("nothing was validated").doesNotContain(true);
        JsonNode again = run(false);
        assertThat(again.get("state").asText()).isEqualTo("done");
        assertThat(again.get("all_validated").asBoolean()).isTrue();
    }

    @Test
    void aLockThatStaysHeldPastTheBound_defersThatTableAndItsAncestors_andARerunFinishes() throws Exception {
        seedValid();
        String leaf = leafOf(TENANT_A, "clean one");
        Connection held = holdShare(leaf);   // a SHARE lock: the pre-check reads, the VALIDATE cannot take its lock
        JsonNode done;
        try {
            done = run(false);
        } finally {
            release(held);
        }
        System.out.println("held past the bound: " + done);
        assertThat(done.get("state").asText()).isEqualTo("done");
        assertThat(done.get("refused")).as("a lock is not a data problem").isEmpty();
        List<String> deferred = tables(done.get("deferred"));
        assertThat(deferred).contains("nexus." + leaf, "nexus.chunks");
        for (JsonNode d : done.get("deferred")) {
            if (("nexus." + leaf).equals(d.get("table").asText())) {
                assertThat(d.get("reason").asText()).isEqualTo("lock_timeout");
            }
        }
        assertThat(validity().get(leaf)).as("not validated while the lock was held").isFalse();
        assertThat(done.get("all_validated").asBoolean()).isFalse();

        JsonNode again = run(false);
        assertThat(again.get("deferred")).isEmpty();
        assertThat(again.get("all_validated").asBoolean()).isTrue();
        assertThat(conformance().get("all_validated").asBoolean()).isTrue();
    }

    // ── dry run, definitions ────────────────────────────────────────────────

    @Test
    void aDryRun_runsEveryPreCheckAndAltersNothing() throws Exception {
        seedValid();
        seedBad(TENANT_A, true, false);
        String badLeaf = leafOf(TENANT_A, "poisoned legacy row");
        Map<String, Boolean> before = validity();

        JsonNode done = run(true);
        System.out.println("dry run: " + done);

        assertThat(done.get("dry_run").asBoolean()).isTrue();
        assertThat(done.get("validated")).as("a dry run validates nothing").isEmpty();
        assertThat(validity()).as("and changes no constraint").isEqualTo(before);
        assertThat(tables(done.get("refused"))).containsExactly("nexus." + badLeaf);
        assertThat(done.get("refused").get(0).get("bad_rows").asLong()).isEqualTo(1);
        List<String> would = strings(done.get("would_validate"));
        assertThat(would).as("the clean tables a real run would validate").contains("nexus.catalog_document_chunks");
        assertThat(would).doesNotContain("nexus." + badLeaf, "nexus.chunks");
        assertThat(done.get("would_validate_count").asInt()).isEqualTo(would.size());
        assertThat(done.get("all_validated").asBoolean()).isFalse();
    }

    @Test
    void aSameNamedWeakerConstraint_isRefusedAndReported_neverValidated() throws Exception {
        seedValid();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-chash-weaker-not-valid.xml",
                "databasechangelog_test_chash_weaker", Map.of());
        }
        assertThat(validity().get("catalog_document_chunks")).as("planted NOT VALID").isFalse();

        JsonNode done = run(false);

        assertThat(done.get("refused")).hasSize(1);
        JsonNode refusal = done.get("refused").get(0);
        assertThat(refusal.get("table").asText()).isEqualTo("nexus.catalog_document_chunks");
        assertThat(refusal.get("reason").asText()).isEqualTo("definition_mismatch");
        assertThat(validity().get("catalog_document_chunks")).as("the weaker constraint is not validated").isFalse();
        assertThat(validity().get("chunks")).as("the genuine one is").isTrue();
        assertThat(done.get("all_validated").asBoolean()).isFalse();
        assertThat(done.toString()).as("the definition text is never returned").doesNotContain("octet_length").doesNotContain(">=");
    }

    @Test
    void anAbsentConstraint_isDeferredAsAbsent_notASilentSuccess() throws Exception {
        seedValid();
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraint(MANIFEST_CHECK).execute();
        }
        JsonNode done = run(false);
        assertThat(tables(done.get("deferred"))).containsExactly("nexus.catalog_document_chunks");
        assertThat(done.get("deferred").get(0).get("reason").asText()).isEqualTo("constraint_absent");
        assertThat(done.get("all_validated").asBoolean()).isFalse();
    }

    // ── the job and its routes ──────────────────────────────────────────────

    @Test
    void aSecondStart_joinsTheRunningJob_andTheOtherDryRunIsAConflict() throws Exception {
        seedValid();
        String leaf = leafOf(TENANT_A, "clean one");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "200");
        System.setProperty(ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, "50");
        Connection held = holdAccessExclusive(leaf);
        try {
            var first = MAPPER.readTree(post(ROOT, "{}").body());
            assertThat(first.get("created").asBoolean()).isTrue();
            String id = first.get("job_id").asText();

            var second = post(ROOT, "{\"dry_run\":false}");
            assertThat(second.statusCode()).isEqualTo(202);
            var joined = MAPPER.readTree(second.body());
            assertThat(joined.get("job_id").asText()).as("handed the running job").isEqualTo(id);
            assertThat(joined.get("created").asBoolean()).isFalse();

            var other = post(ROOT, "{\"dry_run\":true}");
            assertThat(other.statusCode()).as(other.body()).isEqualTo(409);
            assertThat(MAPPER.readTree(other.body()).get("job_id").asText()).isEqualTo(id);

            release(held);
            held = null;
            assertThat(await(id).get("state").asText()).isEqualTo("done");
            // a finished job is never reused: the next start is a new job
            var next = MAPPER.readTree(post(ROOT, "{}").body());
            assertThat(next.get("created").asBoolean()).isTrue();
            assertThat(next.get("job_id").asText()).isNotEqualTo(id);
            await(next.get("job_id").asText());
        } finally {
            if (held != null) release(held);
        }
    }

    @Test
    void onlyTheRootTokenMayStartOrReadTheJob() throws Exception {
        seedValid();
        String id = start(false);
        await(id);
        assertThat(post(TENANT_TOKEN, "{}").statusCode()).isEqualTo(403);
        var read = get(TENANT_TOKEN, PATH + "/" + id);
        assertThat(read.statusCode()).isEqualTo(403);
        assertThat(read.body()).doesNotContain("tables_total").doesNotContain("nexus.");
        assertThat(post(null, "{}").statusCode()).isEqualTo(401);
        assertThat(get(null, PATH + "/" + id).statusCode()).isEqualTo(401);
        assertThat(get(ROOT, PATH + "/" + id).statusCode()).isEqualTo(200);
    }

    /**
     * What the job validated does not decay by growth: a tenant leaf and a whole model partition created afterwards through
     * the real vectors-030 functions copy the parent's VALID constraint valid (an empty new copy needs no scan), so the
     * conformance route still reads {@code all_validated}. Kills: a job that validated the leaves it found but left the
     * parent NOT VALID (the new copies would then be born NOT VALID).
     */
    @Test
    void aLeafAndAModelPartitionCreatedAfterTheJob_areBornValid_soTheRouteStillReadsAllValidated() throws Exception {
        seedValid();
        assertThat(run(false).get("all_validated").asBoolean()).isTrue();
        int before = conformance().get("constraints").get(0).get("partitions").asInt();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(EMBEDDING_MODELS, EMBEDDING_MODELS.EMBEDDING_MODEL, EMBEDDING_MODELS.DIMENSION, EMBEDDING_MODELS.PROVIDER)
                .values("decay-added-1024", 1024, "voyage").onConflictDoNothing().execute();
            PartitionScratch.createModelPartition(ctx, "chunks", "decay-added-1024", true);
            PgContainerHelper.ensureTenantPartitions(ctx, "t-decay-new-tenant");
        }
        JsonNode chunks = conformance().get("constraints").get(0);
        assertThat(chunks.get("partitions").asInt()).as("new copies exist (non-vacuous)").isGreaterThan(before);
        assertThat(chunks.get("partitions_not_validated").asInt()).as("and are born valid").isZero();
        assertThat(conformance().get("all_validated").asBoolean()).isTrue();
    }

    /**
     * A tenant reset reaches a FINISHED job's refusal through the service's own evictor list and the real registry: a job is
     * run to a refusal over a real bad row, the evictor named ValidateConstraintsJobs runs for a tenant, and the status no
     * longer carries the example chash. Kills: an evictor whose registry wiring (evictWhere, the job's data) finds nothing.
     */
    @Test
    void aTenantResetScrubsAFinishedJobsRefusal_throughTheServicesOwnEvictorList() throws Exception {
        seedValid();
        seedBad(TENANT_A, true, false);
        String id = start(false);
        JsonNode done = await(id);
        assertThat(done.get("refused")).hasSize(1);
        assertThat(strings(done.get("refused").get(0).get("example_chashes"))).containsExactly(LEGACY_HEX);
        assertThat(done.get("refused").get(0).has("reasons")).as("the per-reason account is in the status before the reset").isTrue();
        assertThat(done.get("refused").get(0).get("chunks")).as("and the listed chunk with its tenant").isNotEmpty();

        var evictor = service.tenantKeyedState().stream().filter(t -> t.name().equals("ValidateConstraintsJobs")).findFirst()
            .orElseThrow(() -> new AssertionError("the service lists no ValidateConstraintsJobs evictor"));
        assertThat(evictor.evictTenant(TENANT_B)).as("the example chash may be any tenant's: any reset clears it (the registry "
            + "also holds this class's earlier jobs, each refused step of which is scrubbed too)").isGreaterThanOrEqualTo(1);

        JsonNode after = status(id);
        assertThat(after.get("refused").get(0).get("example_chashes")).isEmpty();
        assertThat(after.get("refused").get(0).has("reasons")).as("the per-reason account may hold any tenant's rows: gone too").isFalse();
        assertThat(after.get("refused").get(0).has("chunks")).as("and the listed chunks, with their tenants and owners").isFalse();
        assertThat(after.get("refused").get(0).get("reasons_unavailable").asBoolean()).isTrue();
        assertThat(after.get("refused").get(0).get("table").asText()).as("the job itself stays").isEqualTo(done.get("refused").get(0).get("table").asText());
        assertThat(dev.nexus.service.http.ValidateConstraintsJobs.forgetTenant(service.validateConstraintsJobsForTests(), TENANT_A))
            .as("nothing left to scrub").isZero();
    }

    @Test
    void theRoutesRejectWhatTheyDoNotTake() throws Exception {
        assertThat(post(ROOT, "null").statusCode()).as("the JSON literal null is not an object").isEqualTo(400);
        assertThat(post(ROOT, "[]").statusCode()).isEqualTo(400);
        assertThat(post(ROOT, "{\"dry_run\":\"yes\"}").statusCode()).isEqualTo(400);
        assertThat(get(ROOT, PATH + "/no-such-job").statusCode()).isEqualTo(404);
        assertThat(get(ROOT, PATH).statusCode()).as("GET on the start route").isEqualTo(405);
        var del = http.send(TestHttp.request(url(PATH + "/x")).header("Authorization", "Bearer " + ROOT).DELETE().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(del.statusCode()).isEqualTo(405);
        assertThat(get(ROOT, PATH + "x").statusCode()).as("a path that merely starts with it").isEqualTo(404);
    }

    @Test
    void theStatusCarriesTheProgressShape_andAnEmptyBodyIsARealRun() throws Exception {
        seedValid();
        var resp = post(ROOT, "");
        assertThat(resp.statusCode()).isEqualTo(202);
        String id = MAPPER.readTree(resp.body()).get("job_id").asText();
        JsonNode done = await(id);
        assertThat(done.fieldNames()).toIterable().contains("job_id", "state", "stage", "elapsed_ms", "dry_run", "tables_done",
            "tables_total", "current", "validated", "refused", "deferred", "safe_to_stop", "on_stop", "all_validated");
        assertThat(done.get("dry_run").asBoolean()).isFalse();
        assertThat(done.get("stage").asText()).isEqualTo("done");
        assertThat(done.get("current").isNull()).isTrue();
        assertThat(done.get("tables_total").asInt()).isPositive();
    }

    @Test
    void theVersionRouteAdvertisesTheJob() throws Exception {
        var resp = get(ROOT, "/version");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(resp.body()).get("chash_constraints_validate_job_supported").asBoolean()).isTrue();
    }
}
