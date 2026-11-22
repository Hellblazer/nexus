// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.6 (nexus-p2m1x.26), the label rule at the wire: a collection registration and a rename target are
 * refused with a 400 when the label is of the minted form, holds a control character, runs over 255 UTF-8 bytes or
 * holds a slash (Sam's ruling, T2 nexus/sam-ruling-rdr229-label-slash-2026-11-21), and a label the ChromaDB shape used to refuse
 * (100 characters, a space, a percent sign, a colon, a non-ASCII letter) registers, lists and renames. The rule's own
 * table is {@code CollectionLabelTest}; this is the mapping that test cannot reach (a refusal that reached the
 * generic catch would read as a 500) and the engine half of the Windows label test (Cross-Cutting, Windows).
 * Rows are registered the way {@code HttpCatalogClient.register_collection} registers a non-conformant name.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionLabelRuleRouteTest {

    private static final String TOKEN = "collection-label-rule-token-p36";
    private static final String SVC_ROLE = "svc_label_rule";
    private static final String SVC_PASS = "svc_label_rule_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String MODEL = "voyage-context-3";
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    /** The label of the Windows test: a space, '%', ':' and a non-ASCII character. */
    private static final String WINDOWS_LABEL = "notes 50%: café";
    private static final String WINDOWS_LABEL_RENAMED = "notes 60%: café";

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new CatalogRepository(new TenantScope(svcDs));
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = TestHttp.client();
        // Burn the once-per-process ghost sweep (it deletes registered but chunkless collections) before registering
        // anything, as CatalogHandlerForTupleTest does.
        http.send(request("/v1/catalog/collections/list").GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private HttpRequest.Builder request(String path) {
        return TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT);
    }

    private HttpResponse<String> post(String path, Map<String, Object> body) throws Exception {
        return http.send(request(path).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        return http.send(request(path).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    /** One owner per label, so no two labels share a live tuple (RDR-229 P3.8's guard answers 409 to that). */
    private static String ownerFor(String label) {
        return "label-notes-" + Integer.toHexString(label.hashCode());
    }

    private HttpResponse<String> register(String label) throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("name", label);
        body.put("content_type", "knowledge");
        body.put("owner_id", ownerFor(label));
        body.put("embedding_model", MODEL);
        body.put("model_version", "v1");
        body.put("display_name", "");
        body.put("legacy_grandfathered", true);
        return post("/v1/catalog/collections/upsert", body);
    }

    private HttpResponse<String> rename(String from, String to) throws Exception {
        return post("/v1/catalog/collections/rename", Map.of("old_name", from, "new_name", to));
    }

    private List<String> listedNames() throws Exception {
        var resp = get("/v1/catalog/collections/list");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        var rows = (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("collections");
        return rows.stream().map(r -> (String) r.get("name")).toList();
    }

    private boolean isRegistered(String label) throws Exception {
        var resp = get("/v1/catalog/collections/get?name=" + URLEncoder.encode(label, StandardCharsets.UTF_8));
        return resp.statusCode() == 200;
    }

    // ── registration ────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void registerRefusesAMintedFormLabel_with400_andWritesNothing() throws Exception {
        for (String label : List.of("c1", "c007", "c12345")) {
            var resp = register(label);
            assertThat(resp.statusCode()).as(label + ": " + resp.body()).isEqualTo(400);
            assertThat(resp.body()).contains("minted");
            assertThat(isRegistered(label)).as(label + " was written").isFalse();
        }
    }

    @Test
    void registerRefusesAControlCharacterLabel_with400() throws Exception {
        for (String label : List.of("a\u0000b", "a\nb", "a\u001fb", "a\u007fb", "a b")) {
            var resp = register(label);
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(400);
            assertThat(resp.body()).contains("control");
        }
    }

    @Test
    void registerRefusesALabelOver255Bytes_andAcceptsExactly255MultiByte() throws Exception {
        assertThat(register("é".repeat(128)).statusCode()).isEqualTo(400);
        assertThat(register("a".repeat(256)).statusCode()).isEqualTo(400);
        String exactly255 = "é".repeat(127) + "a";
        var ok = register(exactly255);
        assertThat(ok.statusCode()).as(ok.body()).isEqualTo(200);
        assertThat(isRegistered(exactly255)).isTrue();
    }

    @Test
    void registerRefusesASlash() throws Exception {
        var resp = register("a/b");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(400);
    }

    @Test
    void registerAcceptsA100CharacterLabelAndANonConformantOne() throws Exception {
        for (String label : List.of("l".repeat(100), "My Notes", "c", "C1", "c1a")) {
            var resp = register(label);
            assertThat(resp.statusCode()).as(label + ": " + resp.body()).isEqualTo(200);
            assertThat(listedNames()).contains(label);
        }
    }

    @Test
    void reRegisteringAnExistingRowIsNotARegistrationOfANewLabel() throws Exception {
        // The rule gates the creation of a label. An existing row re-registers (the upsert's idempotent arm) whatever
        // its label, so a row the engine minted (P3.8) is never locked out of its own re-registration.
        assertThat(register("idempotent label").statusCode()).isEqualTo(200);
        assertThat(register("idempotent label").statusCode()).isEqualTo(200);
    }

    @Test
    void anExistingMintedFormRowStillReRegisters_theRuleGatesTheCreationOfALabel() throws Exception {
        // A row of the minted form the engine wrote itself (P3.8 will; seeded here), then the client's idempotent
        // re-registration of it: 200, not the 400 a NEW c7 gets.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, "c7");
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("name", "c7");
        body.put("content_type", "unknown");
        body.put("owner_id", TENANT);
        body.put("embedding_model", "bge-base-en-v15-768");
        body.put("model_version", "v1");
        var resp = post("/v1/catalog/collections/upsert", body);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        // while a NEW minted-form label is refused
        assertThat(register("c8").statusCode()).isEqualTo(400);
    }

    @Test
    void anExistingRowThatBreaksTheRuleStillReRegisters_theShapeClausesGateANewLabelOnly() throws Exception {
        // RDR-229 item 8: existing collections keep their names. Rows seeded directly, as an old install holds them.
        for (String legacy : List.of("legacy/slash/label", "é".repeat(130), "legacy\u0001control")) {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, legacy);
            }
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("name", legacy);
            body.put("content_type", "unknown");
            body.put("owner_id", TENANT);
            body.put("embedding_model", "bge-base-en-v15-768");
            body.put("model_version", "v1");
            var resp = post("/v1/catalog/collections/upsert", body);
            assertThat(resp.statusCode()).as(legacy + ": " + resp.body()).isEqualTo(200);
        }
    }

    @Test
    void aNulIsA400BeforeAnyRead_noDatabaseError() throws Exception {
        // the clause that stands before the first database read: the database cannot store a NUL. (A lone surrogate
        // cannot be sent: Jackson writes it as '?'. CollectionLabelTest.requireStorable covers it.)
        var resp = register("a\u0000b");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(400);
        assertThat(resp.body()).doesNotContain("database");
    }

    @Test
    void registerWithANonStringName_is400() throws Exception {
        // Before r2 these reached the INSERT. A missing, null or blank name is P3.8's mint branch (CollectionMint), so
        // only a present non-string name is refused here.
        for (Object bad : new Object[] {5, true, List.of("a")}) {
            Map<String, Object> body = new LinkedHashMap<>(Map.of("content_type", "knowledge", "model_version", "v1"));
            body.put("name", bad);
            var resp = post("/v1/catalog/collections/upsert", body);
            assertThat(resp.statusCode()).as(body + " -> " + resp.body()).isEqualTo(400);
            assertThat(resp.body()).contains("name must be a string");
        }
    }

    @Test
    void renamingBackOntoATombstoneWhoseLabelBreaksTheRule_isNotRefused() throws Exception {
        // RDR-229 item 8: an existing row keeps its label. The undo of a rename names the tombstone the rename left;
        // that label is an old install's ('/'), and the rule must not block the way back.
        String legacy = "legacy/undo/label";
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, legacy);
        }
        var forward = rename(legacy, "undo forward label");
        assertThat(forward.statusCode()).as(forward.body()).isEqualTo(200);

        var back = rename("undo forward label", legacy);

        assertThat(back.statusCode()).as(back.body()).isEqualTo(200);
        assertThat(isRegistered(legacy)).isTrue();
        // while a NEW target that breaks the rule is still refused
        assertThat(rename(legacy, "new/slash").statusCode()).isEqualTo(400);
    }

    // ── import (ETL): a new row's shape, never its minted form, never an existing row ───────────────────────

    private Map<String, Object> importRow(String label) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("name", label);
        row.put("content_type", "knowledge");
        row.put("owner_id", ownerFor(label));
        row.put("embedding_model", MODEL);
        row.put("model_version", "v1");
        return row;
    }

    /** The batch report's {@code refused} entries, by label: label -> rule. */
    @SuppressWarnings("unchecked")
    private Map<String, String> refusedRules(HttpResponse<String> resp) throws Exception {
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        var out = new LinkedHashMap<String, String>();
        for (var entry : (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("refused")) {
            assertThat(entry.get("reason")).as("the entry says why: " + entry).isInstanceOf(String.class);
            out.put((String) entry.get("name"), (String) entry.get("rule"));
        }
        return out;
    }

    @Test
    void importRefusesOnlyTheRowsWhoseLabelBreaksTheShape_theOthersLand() throws Exception {
        // RDR-229 item 10: "refuses that collection; the rest of the bundle imports". Five bad labels, one per
        // clause the route can carry, around a good row and a minted-form row (never refused on import).
        String overlong = "\u00e9".repeat(130);
        var resp = post("/v1/catalog/import/collection", Map.of("rows", List.of(
            importRow("import good first"), importRow("import/bad"), importRow("import\u0007bell"),
            importRow(overlong), importRow("import,comma"), importRow("import edge "), importRow("c4343"),
            importRow("import good last"))));
        assertThat(refusedRules(resp)).containsExactly(
            Map.entry("import/bad", "no_slash"), Map.entry("import\u0007bell", "no_control_character"),
            Map.entry(overlong, "within_byte_limit"), Map.entry("import,comma", "no_comma"),
            Map.entry("import edge ", "no_edge_whitespace"));
        assertThat(resp.body()).as("imported counts the rows that were not refused").contains("\"imported\":3");
        for (String landed : List.of("import good first", "c4343", "import good last")) {
            assertThat(isRegistered(landed)).as(landed).isTrue();
        }
        for (String refused : List.of("import/bad", "import\u0007bell", overlong, "import,comma", "import edge ")) {
            assertThat(isRegistered(refused)).as("refused: " + refused.length()).isFalse();
        }
    }

    @Test
    void importNamesAnEmptyLabelAndAWholeBatchOfRefusals_with200() throws Exception {
        var resp = post("/v1/catalog/import/collection", Map.of("rows", List.of(importRow(""), importRow("all/bad"))));
        assertThat(refusedRules(resp)).containsExactly(Map.entry("", "non_empty"), Map.entry("all/bad", "no_slash"));
        assertThat(resp.body()).contains("\"imported\":0");
    }

    @Test
    void importRefusesANulAndALoneSurrogatePerRow_beforeAnyRead() throws Exception {
        // requireStorable's two clauses stand before the first database read; they too refuse the row only. The lone
        // surrogate is sent as a \\ud800 escape: Jackson would write the raw character as '?'.
        String json = "{\"rows\":[" + rawRow("import fine nul-batch", "import fine nul-batch") + ","
            + rawRow("nul\\u0000row", "nul-owner") + "," + rawRow("surrogate\\ud800row", "surrogate-owner") + "]}";
        var resp = http.send(request("/v1/catalog/import/collection").header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json)).build(), HttpResponse.BodyHandlers.ofString());
        var rules = refusedRules(resp);
        // the NUL is a legal JSON escape and comes back as sent; a lone surrogate cannot be written, so it is named by
        // its escape (it would encode as '?', and two different labels would read alike)
        assertThat(rules).containsExactly(Map.entry("nul\u0000row", "no_control_character"),
            Map.entry("surrogate\\uD800row", "valid_utf8"));
        assertThat(resp.body()).doesNotContain("database").contains("\"imported\":1");
        assertThat(isRegistered("import fine nul-batch")).isTrue();
    }

    @Test
    void aRowRefusedForItsLabelIsJudgedByNoOtherCheck() throws Exception {
        // a second defect on a row the label rule refuses (no content_type, a state that disagrees with the name, a
        // missing name key) must not sink the batch: the row is refused for its label, the rest land
        Map<String, Object> noType = importRow("second/defect-type");
        noType.put("content_type", "");
        Map<String, Object> badState = importRow("second/defect-state");
        badState.put("lifecycle_state", "quarantine");
        Map<String, Object> nameless = importRow("x");
        nameless.remove("name");
        var resp = post("/v1/catalog/import/collection",
            Map.of("rows", List.of(importRow("second good"), noType, badState, nameless)));
        assertThat(refusedRules(resp)).containsExactly(Map.entry("second/defect-type", "no_slash"),
            Map.entry("second/defect-state", "no_slash"), Map.entry("", "non_empty"));
        assertThat(resp.body()).contains("\"imported\":1");
        assertThat(isRegistered("second good")).isTrue();
        // while a row with a good label and no content_type still refuses the whole batch
        Map<String, Object> goodNoType = importRow("second fine label");
        goodNoType.put("content_type", "");
        assertThat(post("/v1/catalog/import/collection", Map.of("rows", List.of(importRow("second good 2"), goodNoType)))
            .statusCode()).isEqualTo(422);
        assertThat(isRegistered("second good 2")).isFalse();
    }

    private static String rawRow(String escapedLabel, String owner) {
        return "{\"name\":\"" + escapedLabel + "\",\"content_type\":\"knowledge\",\"owner_id\":\"" + owner
            + "\",\"embedding_model\":\"" + MODEL + "\",\"model_version\":\"v1\"}";
    }

    @Test
    void importOfASingleBadRowBody_isAlsoReportedNotA400() throws Exception {
        // a body with no "rows" key is one row, and takes the batch's path: the one collection is refused, named
        var resp = post("/v1/catalog/import/collection", importRow("single body/bad"));
        assertThat(refusedRules(resp)).containsExactly(Map.entry("single body/bad", "no_slash"));
        assertThat(resp.body()).contains("\"imported\":0");
    }

    @Test
    void aCleanBatchReportsNoRefusals_andAnExistingBadLabelIsNotOne() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, "clean/existing");
        }
        var resp = post("/v1/catalog/import/collection",
            Map.of("rows", List.of(importRow("import clean"), importRow("clean/existing"))));
        assertThat(refusedRules(resp)).isEmpty();
        assertThat(resp.body()).contains("\"imported\":2");
    }

    @Test
    void theRepositoryReportNamesTheRuleAndTheLabel() {
        var result = repo.importCollectionsBatchReport(TENANT,
            List.of(importRow("repo good"), importRow("repo/bad"), importRow("repo/bad")));
        assertThat(result.imported()).as("a repeated refused row is two rows sent, both refused").isEqualTo(1);
        assertThat(result.refused()).hasSize(1);
        assertThat(result.refused().get(0).name()).isEqualTo("repo/bad");
        assertThat(result.refused().get(0).rule()).isEqualTo("no_slash");
        assertThat(result.refused().get(0).reason()).contains("repo/bad").contains("'/'");
    }

    @Test
    void importKeepsAMintedFormRowAndAnExistingRowThatBreaksTheRule() throws Exception {
        // another install's engine-minted label imports as it is (the minted-form clause is for a user's own label)
        var minted = post("/v1/catalog/import/collection", Map.of("rows", List.of(importRow("c4242"))));
        assertThat(minted.statusCode()).as(minted.body()).isEqualTo(200);
        assertThat(isRegistered("c4242")).isTrue();
        // an existing row is skipped (ON CONFLICT DO NOTHING), whatever its label
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, "import/existing");
        }
        var existing = post("/v1/catalog/import/collection", Map.of("rows", List.of(importRow("import/existing"))));
        assertThat(existing.statusCode()).as(existing.body()).isEqualTo(200);
    }

    @Test
    void importOfASingleRowAtTheRepositoryRefusesTheShapeToo() {
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.importCollection(TENANT, importRow("single/bad")))
            .isInstanceOf(IllegalArgumentException.class);
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.importCollection(TENANT, importRow("single\u0000bad")))
            .isInstanceOf(IllegalArgumentException.class);
        repo.importCollection(TENANT, importRow("c5151"));
    }

    // ── rename ──────────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void renameRefusesAMintedFormTarget_with400_andLeavesTheSourceLive() throws Exception {
        assertThat(register("rename source minted").statusCode()).isEqualTo(200);
        var resp = rename("rename source minted", "c9");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(400);
        assertThat(resp.body()).contains("minted");
        assertThat(isRegistered("c9")).isFalse();
        assertThat(isRegistered("rename source minted")).isTrue();
    }

    @Test
    void renameRefusesAControlCharacterTarget_with400() throws Exception {
        assertThat(register("rename source control").statusCode()).isEqualTo(200);
        var resp = rename("rename source control", "new\u0000label");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(400);
        assertThat(resp.body()).contains("control");
    }

    @Test
    void renameRefusesAnOverlongAndASlashTarget_with400() throws Exception {
        assertThat(register("rename source long").statusCode()).isEqualTo(200);
        assertThat(rename("rename source long", "é".repeat(128)).statusCode()).isEqualTo(400);
        assertThat(rename("rename source long", "a/b").statusCode()).isEqualTo(400);
    }

    @Test
    void renameToANonConformantLabelOver63Characters_succeeds() throws Exception {
        assertThat(register("rename source ok").statusCode()).isEqualTo(200);
        String target = "Renamed Notes " + "r".repeat(100);
        var resp = rename("rename source ok", target);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertThat(isRegistered(target)).isTrue();
    }

    // ── the Windows label (Cross-Cutting, Windows), engine half ─────────────────────────────────────────────

    @Test
    void aLabelHoldingASpacePercentColonAndNonAscii_registersListsAndRenames() throws Exception {
        var reg = register(WINDOWS_LABEL);
        assertThat(reg.statusCode()).as(reg.body()).isEqualTo(200);
        assertThat(listedNames()).contains(WINDOWS_LABEL);
        var got = get("/v1/catalog/collections/get?name=" + URLEncoder.encode(WINDOWS_LABEL, StandardCharsets.UTF_8));
        assertThat(got.statusCode()).as(got.body()).isEqualTo(200);
        assertThat(mapper.readValue(got.body(), MAP_T).get("name")).isEqualTo(WINDOWS_LABEL);

        var ren = rename(WINDOWS_LABEL, WINDOWS_LABEL_RENAMED);
        assertThat(ren.statusCode()).as(ren.body()).isEqualTo(200);
        assertThat(listedNames()).contains(WINDOWS_LABEL_RENAMED);
        assertThat(isRegistered(WINDOWS_LABEL_RENAMED)).isTrue();
    }
}
