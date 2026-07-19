package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.ResultSet;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-186 bead nexus-146xx.4 — RemapHandler endpoint integration tests.
 *
 * <p>The bead's TDD contract, end to end over HTTP:
 * <ol>
 *   <li>record_batch writes facts; the live-membership function REFLECTS them</li>
 *   <li>clear_leg removes the leg's rows; membership reads nothing owed (0/0)</li>
 *   <li>batch bound: &gt;300 entries → 400 (chroma_quotas heritage cap)</li>
 *   <li>RLS through the HTTP layer: another tenant's facts are invisible</li>
 *   <li>upsert: re-recording the same old_id replaces the fact (idempotent resume)</li>
 *   <li>64-char chunk_text_hash normalizes to [:32]; malformed length → 400</li>
 *   <li>auth: 401 without a bearer token</li>
 * </ol>
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires
 * Docker. Distinct collection names per test — order-independent.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RemapHandlerTest {

    private static final String TOKEN = "remap-handler-test-token-abc123";
    private static final String OTHER_TOKEN = "remap-handler-test-token-def456";
    private static final String SVC_ROLE = "svc_remap_handler_test";
    private static final String SVC_PASS = "svc_remap_handler_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String OTHER_TENANT = "remap-other-tenant";

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chash_remap TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.chunks_384, nexus.chunks_768, nexus.chunks_1024 TO " + SVC_ROLE);
            // RDR-180: remap_membership() now chains through chash_alias to resolve
            // legacy-era facts against a rekeyed store (rdr180-002 comment).
            su.createStatement().execute(
                "GRANT SELECT ON nexus.chash_alias TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.remap_membership(text, text) TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.service_tokens, nexus.session_tokens TO " + SVC_ROLE);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN)
                + "', '" + TENANT + "', 'test-bound') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(OTHER_TOKEN)
                + "', '" + OTHER_TENANT + "', 'test-bound-other') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null)   svcDs.close();
        if (pg != null)      pg.stop();
    }

    // ── Test 1: record_batch → membership reflects it ────────────────────────

    @Test
    void recordBatch_thenMembershipReflectsIt() throws Exception {
        String src = "legacy__h1__src";
        String tgt = "knowledge__h1__tgt";
        seedTargetChunks(tgt, "h1map", 2);

        var resp = post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"legacy-1","new_chash":"%s","target_collection":"%s","provenance":"test"},
              {"old_id":"legacy-2","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(src, chash("h1map1"), tgt, chash("h1map2"), tgt));
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("recorded")).isEqualTo(2);

        var m = membership(TOKEN, TENANT, src, tgt);
        assertThat(m.get("mapped_total"))
            .as("the live-membership function must reflect the batch just written")
            .isEqualTo(2);
        assertThat(m.get("present_count"))
            .as("both claimed chashes were seeded in the target — converged")
            .isEqualTo(2);
    }

    // ── Test 2: clear_leg → membership reads nothing owed ────────────────────

    @Test
    void clearLeg_thenMembershipReadsNothingOwed() throws Exception {
        String src = "legacy__h2__src";
        String tgt = "knowledge__h2__tgt";
        seedTargetChunks(tgt, "h2map", 1);
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"legacy-1","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(src, chash("h2map1"), tgt));
        assertThat(membership(TOKEN, TENANT, src, tgt).get("mapped_total")).isEqualTo(1);

        var resp = post("/v1/remap/clear_leg", TOKEN, TENANT,
            "{\"source_collection\":\"" + src + "\",\"target_collection\":\"" + tgt + "\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("deleted")).isEqualTo(1);

        var after = membership(TOKEN, TENANT, src, tgt);
        assertThat(after.get("mapped_total"))
            .as("after clear_leg the leg owes nothing — the D2 absence-encoding")
            .isEqualTo(0);
        assertThat(after.get("present_count")).isEqualTo(0);
    }

    @Test
    void clearLeg_withoutTargetCollection_rejected400() throws Exception {
        // A leg is the (source, target) PAIR: the wide whole-source clear was
        // rejected at review (co-residency footgun — it would silently delete a
        // sibling leg's still-valid claims). No default, no fallback.
        var resp = post("/v1/remap/clear_leg", TOKEN, TENANT,
            "{\"source_collection\":\"legacy__h2c__src\"}");
        assertThat(resp.statusCode())
            .as("clear_leg without target_collection must be rejected — never a wide clear")
            .isEqualTo(400);
        assertThat(resp.body()).contains("target_collection");
    }

    // ── Test 2b: scoped clear_leg — co-resident targets survive ──────────────

    @Test
    void clearLeg_withTargetFilter_leavesOtherLegUntouched() throws Exception {
        // Co-residency shape (.18): ONE source migrated to TWO targets (two
        // embedding models). Rolling back one leg must not clear the other's
        // claims — the scoped (source, target) clear is the safe form.
        String src = "legacy__h2b__src";
        String tgtA = "knowledge__h2b__tgtA";
        String tgtB = "knowledge__h2b__tgtB";
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"legacy-1","new_chash":"%s","target_collection":"%s","provenance":"test"},
              {"old_id":"legacy-2","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(src, chash("h2bA1"), tgtA, chash("h2bB1"), tgtB));
        assertThat(membership(TOKEN, TENANT, src, tgtA).get("mapped_total")).isEqualTo(1);
        assertThat(membership(TOKEN, TENANT, src, tgtB).get("mapped_total")).isEqualTo(1);

        var resp = post("/v1/remap/clear_leg", TOKEN, TENANT,
            "{\"source_collection\":\"" + src + "\",\"target_collection\":\"" + tgtA + "\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("deleted"))
            .as("scoped clear removes only the (source, tgtA) leg's row")
            .isEqualTo(1);

        assertThat(membership(TOKEN, TENANT, src, tgtA).get("mapped_total"))
            .as("cleared leg owes nothing").isEqualTo(0);
        assertThat(membership(TOKEN, TENANT, src, tgtB).get("mapped_total"))
            .as("the co-resident sibling leg's claim MUST survive a scoped clear")
            .isEqualTo(1);
    }

    // ── Test 3: batch bound — >300 entries rejected 400 ──────────────────────

    @Test
    void recordBatch_over300Entries_rejected400() throws Exception {
        String tgt = "knowledge__h3__tgt";
        StringBuilder entries = new StringBuilder();
        for (int i = 0; i < 301; i++) {
            if (i > 0) entries.append(',');
            entries.append("{\"old_id\":\"legacy-").append(i)
                   .append("\",\"new_chash\":\"").append(chash("h3map" + i))
                   .append("\",\"target_collection\":\"").append(tgt)
                   .append("\",\"provenance\":\"test\"}");
        }
        var resp = post("/v1/remap/record_batch", TOKEN, TENANT,
            "{\"source_collection\":\"legacy__h3__src\",\"entries\":[" + entries + "]}");
        assertThat(resp.statusCode())
            .as("301 entries must be rejected (MAX_BATCH=300, chroma_quotas heritage)")
            .isEqualTo(400);
        assertThat(resp.body()).contains("batch too large");
    }

    // ── Test 4: RLS through the HTTP layer ───────────────────────────────────

    @Test
    void rls_otherTenantsFactsInvisible() throws Exception {
        String src = "legacy__h4__src";
        String tgt = "knowledge__h4__tgt";
        // OTHER_TENANT records 3 facts on the same collection names.
        var writeResp = post("/v1/remap/record_batch", OTHER_TOKEN, OTHER_TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"o-1","new_chash":"%s","target_collection":"%s","provenance":"test"},
              {"old_id":"o-2","new_chash":"%s","target_collection":"%s","provenance":"test"},
              {"old_id":"o-3","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(src, chash("h4a"), tgt, chash("h4b"), tgt, chash("h4c"), tgt));
        assertThat(writeResp.statusCode()).isEqualTo(200);

        var m = membership(TOKEN, TENANT, src, tgt);
        assertThat(m.get("mapped_total"))
            .as("RLS: the default tenant must not see the other tenant's 3 facts")
            .isEqualTo(0);
    }

    // ── Test 5: upsert — re-recording replaces the fact ──────────────────────

    @Test
    void recordBatch_reRecordingSameOldId_replacesFact() throws Exception {
        String src = "legacy__h5__src";
        String tgt = "knowledge__h5__tgt";
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"legacy-1","new_chash":"%s","target_collection":"%s","provenance":"first"}
            ]}""".formatted(src, chash("h5v1"), tgt));
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"legacy-1","new_chash":"%s","target_collection":"%s","provenance":"resume"}
            ]}""".formatted(src, chash("h5v2"), tgt));

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT new_chash, provenance, COUNT(*) OVER () AS total " +
                "FROM nexus.chash_remap WHERE tenant_id = '" + TENANT + "' " +
                "AND source_collection = '" + src + "' AND old_id = 'legacy-1'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("total"))
                .as("upsert on the natural key — one row, not two")
                .isEqualTo(1);
            assertThat(rs.getString("new_chash"))
                .as("re-recording replaces the fact (deterministic resume)")
                .isEqualTo(chash("h5v2"));
            assertThat(rs.getString("provenance")).isEqualTo("resume");
        }
    }

    // ── Test 6: chash validation — full 64-hex stored as-is, legacy 32-hex
    //            rejected, malformed rejected ────────────────────────────────
    //
    // POLARITY NOTE: pre-RDR-180 a 64-char chunk_text_hash form normalized to
    // its [:32] prefix (RDR-108 D1) and bare 32-hex was the canonical accept.
    // RDR-180 inverts that: new_chash is validated through Chash.requireCanonical
    // (nexus-jxizy.7), so only the FULL 64-lowercase-hex digest is a valid NEW
    // fact — never truncated — and a bare 32-hex value is a legacy reference
    // that must be resolved through nexus.chash_alias, not minted fresh.

    @Test
    void recordBatch_full64CharChash_storedAsIs() throws Exception {
        String src = "legacy__h6__src";
        String tgt = "knowledge__h6__tgt";
        String full = chash("h6full");
        var resp = post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"legacy-1","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(src, full, tgt));
        assertThat(resp.statusCode()).isEqualTo(200);

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT new_chash FROM nexus.chash_remap WHERE tenant_id = '" + TENANT + "' " +
                "AND source_collection = '" + src + "'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("new_chash"))
                .as("RDR-180: the full 64-hex digest is the canonical chash — no [:32] truncation")
                .isEqualTo(full);
        }
    }

    @Test
    void recordBatch_legacy32CharChash_rejected400() throws Exception {
        // THE INVERSION: pre-flip a bare 32-hex value was the canonical accept;
        // post-flip it is a legacy reference — never truncatable/paddable/mintable
        // as a new fact.
        var resp = post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"legacy__h6b__src","entries":[
              {"old_id":"legacy-1","new_chash":"%s","target_collection":"t","provenance":"test"}
            ]}""".formatted(chash("h6full").substring(0, 32)));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("new_chash");
    }

    @Test
    void recordBatch_malformedChashLength_rejected400() throws Exception {
        var resp = post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"legacy__h7__src","entries":[
              {"old_id":"legacy-1","new_chash":"abc123","target_collection":"t","provenance":"test"}
            ]}""");
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("new_chash");
    }

    // ── Test 6b: read endpoints — pairs / entries / source_collections ───────
    //
    // The .6 client demotion of chash_remap.db to read-only source moves the
    // cascade (all_pairs), rollback (entries_with_targets), and the
    // prior-collections probe onto these reads — the facts must be readable
    // from where they are written. Raw facts only (RF-186-1).

    @Test
    void readEndpoints_pairsEntriesAndSourceCollections() throws Exception {
        String srcA = "legacy__h8__srcA";
        String srcB = "legacy__h8__srcB";
        String tgt = "knowledge__h8__tgt";
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"a-1","new_chash":"%s","target_collection":"%s","provenance":"test"},
              {"old_id":"a-2","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(srcA, chash("h8a1"), tgt, chash("h8a2"), tgt));
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"b-1","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(srcB, chash("h8b1"), tgt));

        // entries: scoped to one source collection, full fact rows.
        var entriesResp = get("/v1/remap/entries?source_collection=" + srcA, TOKEN, TENANT);
        assertThat(entriesResp.statusCode()).isEqualTo(200);
        var entriesBody = mapper.readValue(entriesResp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        var entries = (java.util.List<Map<String, Object>>) entriesBody.get("entries");
        assertThat(entries)
            .as("entries must return srcA's 2 facts (old_id, new_chash, target_collection)")
            .hasSize(2);
        assertThat(entries)
            .extracting(e -> e.get("old_id"))
            .containsExactlyInAnyOrder("a-1", "a-2");
        assertThat(entries.get(0))
            .containsKeys("old_id", "new_chash", "target_collection");

        // pairs: the cascade's global view, paged.
        var pairsResp = get("/v1/remap/pairs?limit=2&offset=0", TOKEN, TENANT);
        assertThat(pairsResp.statusCode()).isEqualTo(200);
        var pairsBody = mapper.readValue(pairsResp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        var page1 = (java.util.List<java.util.List<String>>) pairsBody.get("pairs");
        assertThat(page1).as("page 1 honors limit").hasSize(2);
        var pairsResp2 = get("/v1/remap/pairs?limit=2&offset=2", TOKEN, TENANT);
        @SuppressWarnings("unchecked")
        var page2 = (java.util.List<java.util.List<String>>) mapper
            .readValue(pairsResp2.body(), MAP_T).get("pairs");
        // Pages are disjoint and together cover at least this test's 3 facts
        // (other tests' facts may interleave — assert on OUR ids only).
        var allOldIds = new java.util.ArrayList<String>();
        page1.forEach(p -> allOldIds.add(p.get(0)));
        page2.forEach(p -> allOldIds.add(p.get(0)));
        // fetch remaining pages until empty to collect every pair
        int offset = 4;
        while (true) {
            @SuppressWarnings("unchecked")
            var page = (java.util.List<java.util.List<String>>) mapper.readValue(
                get("/v1/remap/pairs?limit=2&offset=" + offset, TOKEN, TENANT).body(),
                MAP_T).get("pairs");
            if (page.isEmpty()) break;
            page.forEach(p -> allOldIds.add(p.get(0)));
            offset += 2;
        }
        assertThat(allOldIds)
            .as("paged pairs must cover every fact exactly once")
            .contains("a-1", "a-2", "b-1")
            .doesNotHaveDuplicates();

        // source_collections: the prior-collections probe input.
        var scResp = get("/v1/remap/source_collections", TOKEN, TENANT);
        assertThat(scResp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        var sources = (java.util.List<String>) mapper
            .readValue(scResp.body(), MAP_T).get("source_collections");
        assertThat(sources).contains(srcA, srcB).doesNotHaveDuplicates();
    }

    @Test
    void countEndpoint_totalAndPerSource() throws Exception {
        String srcA = "legacy__h10__srcA";
        String srcB = "legacy__h10__srcB";
        String tgt = "knowledge__h10__tgt";
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"c-1","new_chash":"%s","target_collection":"%s","provenance":"test"},
              {"old_id":"c-2","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(srcA, chash("h10a1"), tgt, chash("h10a2"), tgt));
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"c-3","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(srcB, chash("h10b1"), tgt));

        // Per-source counts are exact (this test owns these collections).
        var perSourceA = mapper.readValue(
            get("/v1/remap/count?source_collection=" + srcA, TOKEN, TENANT).body(), MAP_T);
        assertThat(((Number) perSourceA.get("total")).longValue()).isEqualTo(2);
        var perSourceB = mapper.readValue(
            get("/v1/remap/count?source_collection=" + srcB, TOKEN, TENANT).body(), MAP_T);
        assertThat(((Number) perSourceB.get("total")).longValue()).isEqualTo(1);

        // Tenant-wide total covers at least this test's 3 facts (other tests'
        // facts may interleave in the shared container) and moves with writes:
        // the probe-before-fetch contract is "count changed => rescan".
        long before = ((Number) mapper.readValue(
            get("/v1/remap/count", TOKEN, TENANT).body(), MAP_T).get("total")).longValue();
        assertThat(before).isGreaterThanOrEqualTo(3);
        post("/v1/remap/record_batch", TOKEN, TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"c-4","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(srcB, chash("h10b2"), tgt));
        long after = ((Number) mapper.readValue(
            get("/v1/remap/count", TOKEN, TENANT).body(), MAP_T).get("total")).longValue();
        assertThat(after)
            .as("count must move with writes — the probe-before-fetch signal")
            .isEqualTo(before + 1);
    }

    @Test
    void readEndpoints_rlsScoped() throws Exception {
        String src = "legacy__h9__src";
        String tgt = "knowledge__h9__tgt";
        post("/v1/remap/record_batch", OTHER_TOKEN, OTHER_TENANT, """
            {"source_collection":"%s","entries":[
              {"old_id":"o-1","new_chash":"%s","target_collection":"%s","provenance":"test"}
            ]}""".formatted(src, chash("h9o1"), tgt));

        var entries = (java.util.List<?>) mapper.readValue(
            get("/v1/remap/entries?source_collection=" + src, TOKEN, TENANT).body(),
            MAP_T).get("entries");
        assertThat(entries)
            .as("RLS: default tenant must not see the other tenant's facts via reads")
            .isEmpty();

        // Same isolation for the other two read shapes (each endpoint gets its
        // own RLS assertion — the established bar for this file).
        @SuppressWarnings("unchecked")
        var pairs = (java.util.List<java.util.List<String>>) mapper.readValue(
            get("/v1/remap/pairs?limit=1000&offset=0", TOKEN, TENANT).body(),
            MAP_T).get("pairs");
        assertThat(pairs)
            .extracting(p -> p.get(0))
            .as("RLS: /pairs must not leak the other tenant's old_ids")
            .doesNotContain("o-1");

        @SuppressWarnings("unchecked")
        var sources = (java.util.List<String>) mapper.readValue(
            get("/v1/remap/source_collections", TOKEN, TENANT).body(),
            MAP_T).get("source_collections");
        assertThat(sources)
            .as("RLS: /source_collections must not leak the other tenant's sources")
            .doesNotContain(src);
    }

    // ── Test 7: auth — 401 without bearer ────────────────────────────────────

    @Test
    void noAuth_rejected401() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort()
                + "/v1/remap/membership?source_collection=a&target_collection=b"))
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private Map<String, Object> membership(String token, String tenant,
                                           String src, String tgt) throws Exception {
        var resp = get("/v1/remap/membership?source_collection=" + src
            + "&target_collection=" + tgt, token, tenant);
        assertThat(resp.statusCode()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    /** Seed target chunk rows (superuser) so membership can find the claims. */
    private void seedTargetChunks(String collection, String seedPrefix, int count) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "VALUES ('" + TENANT + "', '" + collection + "') ON CONFLICT DO NOTHING");
            for (int i = 1; i <= count; i++) {
                su.createStatement().execute(
                    "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) " +
                    "VALUES ('" + TENANT + "', '" + collection + "', decode('" + chash(seedPrefix + i) + "', 'hex'), " +
                    "'text', ('[1" + ",0".repeat(1023) + "]')::vector) " +
                    "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
            }
        }
    }

    /** Deterministic 64-hex chash from a seed string — the FULL sha256
     *  digest (RDR-180: the pre-flip [:16-byte] truncation is retired). */
    private static String chash(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    private HttpResponse<String> post(String path, String token, String tenant, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path, String token, String tenant) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
