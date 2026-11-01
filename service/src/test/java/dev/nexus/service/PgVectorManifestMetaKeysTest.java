// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.29 (RDR-236 Phase 3, engine): a search {@code where} on the eight catalog-meta keys ({@code tags},
 * {@code category}, {@code ttl_days}, {@code source_agent}, {@code extraction_method}, {@code
 * quality_gate_overridden}, {@code extraction_source}, {@code frecency_score}) is answered per owning document from
 * {@code catalog_documents.metadata}, with the chunk row as the fallback while the document's meta lacks the key.
 *
 * <p>Fixture (collection {@code SMALL}). Chunk S1 is shared by A and C; the shared row carries C's values, the last
 * writer's. PA is A's alone, PB is B's, PC is C's, PD is D's. A is stamped in full (meta: tags as an array, every
 * other key). B is a note whose meta holds the LEGACY comma string for tags and authoritative EMPTY values (category
 * {@code ""}, ttl_days 0, quality_gate_overridden false) over chunk rows that say otherwise. C is legacy: its meta
 * holds none of the keys, so its answers are the chunk row's (tags as the comma string, ttl_days as the TEXT
 * {@code "14"}). D's meta holds an empty tag array over a row that says {@code stale-d}.
 *
 * <p>Fixture (collection {@code BIG}). 300 filler chunks of document F rank before the one chunk of document R, whose
 * meta holds every key; each rare value is held by one chunk that is not in the nearest 40, so only the in-SQL
 * EXISTS can find it at a limit of 10.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorManifestMetaKeysTest {

    private static final String SVC_ROLE = "svc_meta_keys";
    private static final String SVC_PASS = "svc_meta_keys_pass";

    private static final String TENANT = "meta-keys";
    private static final String SMALL = "docs__meta-keys__minilm-l6-v2-384__v1";
    private static final String BIG = "docs__meta-big__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String QUERY = "metaprobe";
    private static final int FILLERS = 300;

    private static final String DOC_A = "mk-doc-a";
    private static final String DOC_B = "mk-doc-b";
    private static final String DOC_C = "mk-doc-c";
    private static final String DOC_D = "mk-doc-d";
    private static final String DOC_F = "mk-doc-f";
    private static final String DOC_R = "mk-doc-r";

    private static final List<String> SMALL_NAMES = List.of("S1", "PA", "PB", "PC", "PD");

    private static String chash(String name) {
        return Chash.ofText("mk-" + name).toHex();
    }

    private static String text(String name) {
        return "metaprobe chunk " + name;
    }

    private static String fill(int i) {
        return "FILL" + i;
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository vec;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        var tenantScope = new TenantScope(svcDs);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        // Query (1, 0). SMALL: S1 < PA < PB < PC < PD. BIG: FILL0 < ... < FILL299 < RARE.
        for (int i = 0; i < SMALL_NAMES.size(); i++) {
            double theta = (Math.PI / 2.0) * i / (SMALL_NAMES.size() - 1);
            embedder.register(text(SMALL_NAMES.get(i)), (float) Math.cos(theta), (float) Math.sin(theta));
        }
        for (int i = 0; i <= FILLERS; i++) {
            double theta = (Math.PI / 2.0) * i / FILLERS;
            embedder.register(text(i == FILLERS ? "RARE" : fill(i)), (float) Math.cos(theta), (float) Math.sin(theta));
        }
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, SMALL);
            PgContainerHelper.insertCollection(seed, TENANT, BIG);
            doc(seed, DOC_A, SMALL, "{\"tags\":[\"arch\",\"decision\"],\"category\":\"design\",\"ttl_days\":30,"
                + "\"source_agent\":\"alice\",\"extraction_method\":\"docling\",\"quality_gate_overridden\":true,"
                + "\"extraction_source\":\"pdf\",\"frecency_score\":0.9}");
            doc(seed, DOC_B, SMALL, "{\"tags\":\"arch,notes\",\"category\":\"\",\"ttl_days\":0,"
                + "\"quality_gate_overridden\":false}");
            doc(seed, DOC_C, SMALL, "{}");
            doc(seed, DOC_D, SMALL, "{\"tags\":[],\"category\":\"other\"}");
            doc(seed, DOC_F, BIG, "{\"category\":\"filler\"}");
            doc(seed, DOC_R, BIG, "{\"tags\":[\"rare-tag\",\"x\"],\"category\":\"rare-cat\",\"ttl_days\":9999,"
                + "\"source_agent\":\"rare-agent\",\"extraction_method\":\"rare-method\","
                + "\"quality_gate_overridden\":true,\"extraction_source\":\"rare-source\",\"frecency_score\":0.99}");
        }

        // C's values, the last writer of the shared S1 and the owner of PC.
        Map<String, Object> cRow = row("tags", "legacy-tag,x", "category", "legacy-cat", "ttl_days", "14",
            "source_agent", "bob", "extraction_method", "ocr", "quality_gate_overridden", true,
            "extraction_source", "scan", "frecency_score", 0.3);
        vec.upsertChunks(TENANT, SMALL, SMALL_NAMES.stream().map(PgVectorManifestMetaKeysTest::chash).toList(),
            SMALL_NAMES.stream().map(PgVectorManifestMetaKeysTest::text).toList(),
            List.of(cRow,
                row("tags", "stale-a", "category", "stale", "ttl_days", 1, "source_agent", "stale"),
                row("category", "stale-b", "ttl_days", 5, "quality_gate_overridden", true, "tags", "stale-b"),
                cRow,
                row("tags", "stale-d")));
        List<String> bigNames = new ArrayList<>();
        for (int i = 0; i < FILLERS; i++) bigNames.add(fill(i));
        bigNames.add("RARE");
        for (int from = 0; from < bigNames.size(); from += 150) {
            List<String> names = bigNames.subList(from, Math.min(bigNames.size(), from + 150));
            vec.upsertChunks(TENANT, BIG, names.stream().map(PgVectorManifestMetaKeysTest::chash).toList(),
                names.stream().map(PgVectorManifestMetaKeysTest::text).toList(),
                names.stream().map(n -> row()).toList());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            manifest(seed, DOC_A, SMALL, 0, "S1");
            manifest(seed, DOC_A, SMALL, 1, "PA");
            manifest(seed, DOC_B, SMALL, 0, "PB");
            manifest(seed, DOC_C, SMALL, 0, "S1");
            manifest(seed, DOC_C, SMALL, 1, "PC");
            manifest(seed, DOC_D, SMALL, 0, "PD");
            for (int i = 0; i < FILLERS; i++) manifest(seed, DOC_F, BIG, i, fill(i));
            manifest(seed, DOC_R, BIG, 0, "RARE");
            // The planner's plan for the selective statement turns on statistics; a bulk load is analysed when
            // autovacuum gets to it, so analyse here (PgVectorManifestRareLabelTest's lesson).
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void doc(DSLContext seed, String tumbler, String collection, String metaJson) {
        seed.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, tumbler)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + tumbler)
            .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(metaJson))
            .execute();
    }

    private static void manifest(DSLContext seed, String doc, String collection, int position, String name) {
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash(name)))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, false)
            .execute();
    }

    private static Map<String, Object> row(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static Map<String, Object> op(String operator, Object operand) {
        return row(operator, operand);
    }

    private static String label(Map<String, Object> r) {
        String chash = (String) r.get("id");
        String name = null;
        for (String n : List.of("S1", "PA", "PB", "PC", "PD", "RARE")) {
            if (chash.equals(chash(n))) name = n;
        }
        String doc = (String) r.get("doc_id");
        return (name == null ? "F" : name) + "@" + (doc == null ? "none" : doc.substring(doc.length() - 1).toUpperCase());
    }

    private static List<String> labels(List<Map<String, Object>> rows) {
        return rows.stream().map(PgVectorManifestMetaKeysTest::label).collect(Collectors.toList());
    }

    private void expect(String collection, Map<String, Object> where, String... expected) {
        assertThat(labels(vec.search(TENANT, QUERY, List.of(collection), 10, where)))
            .as("search where %s", where).containsExactly(expected);
        assertThat(labels(vec.hybridSearchWithTokens(TENANT, QUERY, List.of(collection), 10, where).value()))
            .as("hybrid where %s", where).containsExactlyInAnyOrder(expected);
        assertThat(labels(vec.searchPerCollection(TENANT, QUERY, List.of(collection), 10, 10, null, where, false).rows()))
            .as("per-collection where %s", where).containsExactly(expected);
    }

    private void expectSmall(Map<String, Object> where, String... expected) {
        expect(SMALL, where, expected);
    }

    // ── category, source_agent, extraction_*: exact strings ─────────────────

    @Test
    void category_isTheDocumentsOwn_aSharedRowsLastWriterDoesNotSpeakForTheOtherOwner() {
        // S1's row says legacy-cat (C wrote last), but A's meta says design: S1 is A's design chunk and C's legacy one.
        expectSmall(row("category", "design"), "S1@A", "PA@A");
        expectSmall(row("category", "legacy-cat"), "S1@C", "PC@C");
        // A's row says "stale"; the meta wins. D's meta says other.
        expectSmall(row("category", "stale"));
        expectSmall(row("category", "other"), "PD@D");
        expectSmall(row("category", op("$in", List.of("design", "other"))), "S1@A", "PA@A", "PD@D");
    }

    @Test
    void anEmptyMetaValue_isTheDocumentsAnswer_theRowIsNotConsulted() {
        // B's meta has category "" (the catalog cannot retract a key): PB's row says stale-b, which must not count.
        expectSmall(row("category", ""), "PB@B");
        expectSmall(row("category", "stale-b"));
    }

    @Test
    void sourceAgent_andTheExtractionKeys_areExactStrings_withTheRowAsTheFallback() {
        expectSmall(row("source_agent", "alice"), "S1@A", "PA@A");
        expectSmall(row("source_agent", "bob"), "S1@C", "PC@C");
        expectSmall(row("source_agent", "ALICE"));
        expectSmall(row("extraction_method", "docling"), "S1@A", "PA@A");
        expectSmall(row("extraction_method", "ocr"), "S1@C", "PC@C");
        expectSmall(row("extraction_source", "pdf"), "S1@A", "PA@A");
        expectSmall(row("extraction_source", "scan"), "S1@C", "PC@C");
    }

    // ── tags: a set ─────────────────────────────────────────────────────────

    @Test
    void tags_matchAnyMemberOfTheArray_andOfTheLegacyCommaString() {
        // A: array [arch, decision]; B: legacy comma string "arch,notes".
        expectSmall(row("tags", "arch"), "S1@A", "PA@A", "PB@B");
        expectSmall(row("tags", "decision"), "S1@A", "PA@A");
        expectSmall(row("tags", "notes"), "PB@B");
        expectSmall(row("tags", "arc"));
        expectSmall(row("tags", op("$in", List.of("decision", "notes"))), "S1@A", "PA@A", "PB@B");
    }

    @Test
    void tags_fallBackToTheRowsCommaString_whileTheMetaLacksTheKey() {
        // C's meta has no tags: its row's "legacy-tag,x" is split on commas.
        expectSmall(row("tags", "legacy-tag"), "S1@C", "PC@C");
        expectSmall(row("tags", "x"), "S1@C", "PC@C");
        // A's row says stale-a on PA, but A's meta has the key, so the row is not consulted.
        expectSmall(row("tags", "stale-a"));
    }

    @Test
    void anEmptyTagArray_isTheDocumentsAnswer_andAnUntaggedDocumentAnswersTheEmptyString() {
        // D's meta tags [] is authoritative over its row's stale-d.
        expectSmall(row("tags", "stale-d"));
        expectSmall(row("tags", ""), "PD@D");
    }

    @Test
    void tags_negativeOperators_arePostSearch_andMeanNoMember() {
        // S1@A holds arch, so $ne arch drops it; S1@C holds legacy-tag and x, so it stays.
        expectSmall(row("tags", op("$ne", "arch")), "S1@C", "PC@C", "PD@D");
        expectSmall(row("tags", op("$nin", List.of("arch", "x"))), "PD@D");
    }

    // ── ttl_days, frecency_score: numeric ───────────────────────────────────

    @Test
    void ttlDays_isNumeric_aNumberAndTheRowsTextCompareEqual_zeroIsAValue() {
        expectSmall(row("ttl_days", 30), "S1@A", "PA@A");
        expectSmall(row("ttl_days", "30"), "S1@A", "PA@A");
        // C's row holds the TEXT "14".
        expectSmall(row("ttl_days", 14), "S1@C", "PC@C");
        // B's meta says 0 (permanent): a value. D has no ttl_days anywhere, which is absent, not 0.
        expectSmall(row("ttl_days", 0), "PB@B");
        // A's row says 1 on PA; the meta's 30 wins.
        expectSmall(row("ttl_days", 1));
    }

    @Test
    void ttlDays_andFrecencyScore_rangesCompareAsNumbers_acrossTheMetaAndTheRowsText() {
        expectSmall(row("ttl_days", op("$gte", 20)), "S1@A", "PA@A");
        expectSmall(row("ttl_days", op("$lte", 14)), "S1@C", "PB@B", "PC@C");
        expectSmall(row("ttl_days", op("$gt", 14)), "S1@A", "PA@A");
        expectSmall(row("frecency_score", op("$gte", 0.5)), "S1@A", "PA@A");
        expectSmall(row("frecency_score", op("$lt", 0.5)), "S1@C", "PC@C");
    }

    // ── quality_gate_overridden: boolean ────────────────────────────────────

    @Test
    void qualityGateOverridden_theJsonBooleanAndItsTextFormMatch_theRowIsTheFallback() {
        // A's meta true; C's row true (S1 and PC); B's meta false over a row that says true.
        expectSmall(row("quality_gate_overridden", true), "S1@A", "S1@C", "PA@A", "PC@C");
        expectSmall(row("quality_gate_overridden", "true"), "S1@A", "S1@C", "PA@A", "PC@C");
        expectSmall(row("quality_gate_overridden", false), "PB@B");
        expectSmall(row("quality_gate_overridden", "True"));
    }

    // ── the owner of a shared chunk, and the row a filter names ─────────────

    @Test
    void aSharedChunk_isReportedUnderTheOwnerThatQualifies_andShowsThatOwnersValues() {
        var rows = vec.search(TENANT, QUERY, List.of(SMALL), 10, row("category", "design"));
        assertThat(rows).extracting(PgVectorManifestMetaKeysTest::label).containsExactly("S1@A", "PA@A");
        // The row speaks for A: its meta value, not the shared row's last writer's.
        assertThat(rows.get(0).get("category")).isEqualTo("design");

        var tags = vec.search(TENANT, QUERY, List.of(SMALL), 10, row("tags", "decision"));
        // tags show as the chunk rows' own comma-separated text.
        assertThat(tags.get(0).get("tags")).isEqualTo("arch,decision");

        var ttl = vec.search(TENANT, QUERY, List.of(SMALL), 10, row("ttl_days", 30));
        assertThat(((Number) ttl.get(0).get("ttl_days")).intValue()).isEqualTo(30);
    }

    // ── a rare value is found however far down it ranks ─────────────────────

    @Test
    void aRareMetaValue_heldByTheLastOfThreeHundredAndOneChunks_isFoundAtTenRows() {
        // The nearest 40 are filler; only the in-SQL EXISTS can reach chunk RARE (rank 301).
        expect(BIG, row("category", "rare-cat"), "RARE@R");
        expect(BIG, row("tags", "rare-tag"), "RARE@R");
        expect(BIG, row("tags", op("$in", List.of("nothing", "x"))), "RARE@R");
        expect(BIG, row("source_agent", "rare-agent"), "RARE@R");
        expect(BIG, row("extraction_method", "rare-method"), "RARE@R");
        expect(BIG, row("extraction_source", "rare-source"), "RARE@R");
        expect(BIG, row("ttl_days", 9999), "RARE@R");
        expect(BIG, row("ttl_days", op("$gte", 9000)), "RARE@R");
        expect(BIG, row("frecency_score", op("$gt", 0.95)), "RARE@R");
        expect(BIG, row("quality_gate_overridden", true), "RARE@R");
    }

    @Test
    void aRareMetaValue_isFoundOnTheHnswRouteToo() {
        PgSession.overrideSearchExactMaxRowsForTests(0);
        try {
            expect(BIG, row("category", "rare-cat"), "RARE@R");
            expect(BIG, row("tags", "rare-tag"), "RARE@R");
            expect(BIG, row("ttl_days", op("$gte", 9000)), "RARE@R");
        } finally {
            PgSession.resetSearchExactMaxRowsForTests();
        }
    }
}
