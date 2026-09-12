// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-169 Gap 2 remainder (bead nexus-4k1vz): live-schema proof that the four
 * catalog-scoped combined-query tools ({@link PgVectorRepository#searchMetadataScoped},
 * {@link PgVectorRepository#searchGraphHop}, {@link PgVectorRepository#searchTopicScoped},
 * {@link PgVectorRepository#searchAspectScoped}) surface {@code retention} on every row,
 * including a reference-only one — the gap {@code vectors-015-retention-search-return.xml}
 * (RDR-169 Phase B fix round 1, Gap 2) explicitly deferred as "a materially larger,
 * independently reviewable change than this fix round's other six items combined".
 *
 * <p>Real PG via {@link PgContainerHelper#applyProductSchema} (a full Liquibase walk
 * through {@code vectors-016-combined-query-retention.xml}), style of {@link
 * ReferenceOnlyChunkUpsertTest}. One FULL-content chunk and one reference-only chunk are
 * each registered in the catalog manifest, reached by a catalog link from a common seed
 * (graph-hop), assigned to a shared topic (topic-scoped), and carry a document_aspects row
 * (aspect-scoped) — so all four combined-query paths return BOTH rows in one call, and each
 * assertion is genuinely non-vacuous: a function that dropped the retention column, or
 * returned the wrong value for either row, would fail here, not just "return nothing".
 *
 * <p>No vector-similarity threshold gates any of the four functions (ranking + LIMIT only),
 * so the fixture does not need carefully engineered embedding geometry — {@code n_results}
 * merely needs to be at least 2 for both rows to survive the LIMIT.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CombinedQueryRetentionTest {

    private static final int DIM = 1024;
    private static final String TENANT = "t-cq-retention";
    private static final String COL    = "knowledge__cqretention-owner__voyage-context-3__v1";

    private static final String SEED_TUMBLER    = "cq-ret-seed";
    private static final String FULL_TUMBLER    = "cq-ret-full";
    private static final String REFONLY_TUMBLER = "cq-ret-refonly";
    private static final String LINK_TYPE       = "cites";
    private static final String TOPIC_LABEL     = "cq-retention-topic";

    private static final String FULL_TEXT  = "full content chunk for combined query retention probe";
    private static final String QUERY_TEXT = "combined query retention probe";
    private static final float[] REFONLY_VEC = FakeEmbedder.unitVector(DIM, 0.6f, 0.8f);

    private static final String FULL_CHASH    = Chash.ofText("cq-ret-full-chash").toHex();
    private static final String REFONLY_CHASH = Chash.ofText("cq-ret-refonly-chash").toHex();

    private static final int N_RESULTS = 10;

    PostgreSQLContainer<?> pg;
    HikariDataSource       svcDs;
    TenantScope            tenantScope;
    PgVectorRepository     repo;
    AspectRepository       aspectRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        FakeEmbedder embedder = new FakeEmbedder(DIM);
        embedder.register(FULL_TEXT, 1.0f, 0.0f);
        embedder.register(QUERY_TEXT, 1.0f, 0.0f);
        repo = new PgVectorRepository(tenantScope, embedder, embedder);
        aspectRepo = new AspectRepository(tenantScope);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * One full-content chunk and one reference-only chunk, both registered in the catalog
     * manifest under distinct tumblers, both reachable via a single catalog link from a
     * shared seed, both assigned to the same topic, and both carrying a document_aspects
     * row — so every one of the four combined-query paths sees both rows.
     */
    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COL);
        }

        // Full-content chunk via the ordinary write path (retention='full' implicitly).
        repo.upsertChunks(TENANT, COL, List.of(FULL_CHASH), List.of(FULL_TEXT), List.of(Map.of()));
        // Reference-only chunk: no content, a caller-supplied embedding (RDR-169 G4).
        repo.upsertReferenceOnlyChunk(TENANT, COL, REFONLY_CHASH, REFONLY_VEC, Map.of());

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);

            // Catalog documents: the seed (no chunks of its own) plus the two content docs.
            for (String tumbler : List.of(SEED_TUMBLER, FULL_TUMBLER, REFONLY_TUMBLER)) {
                ctx.insertInto(CATALOG_DOCUMENTS)
                   .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                   .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
                   .set(CATALOG_DOCUMENTS.TITLE, "Doc " + tumbler)
                   .set(CATALOG_DOCUMENTS.AUTHOR, "ada")
                   .set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                   .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COL)
                   .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
                   .doNothing()
                   .execute();
            }

            // Manifest: doc -> chash (metadata-scoped, graph-hop, aspect-scoped all join
            // through this).
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                    CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(TENANT, FULL_TUMBLER, 0, HexFormat.of().parseHex(FULL_CHASH), COL)
               .onConflict(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION)
               .doNothing()
               .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                    CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(TENANT, REFONLY_TUMBLER, 0, HexFormat.of().parseHex(REFONLY_CHASH), COL)
               .onConflict(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION)
               .doNothing()
               .execute();

            // One catalog link each, from the shared seed (graph-hop's reachable set).
            ctx.insertInto(CATALOG_LINKS,
                    CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER,
                    CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
               .values(TENANT, SEED_TUMBLER, FULL_TUMBLER, LINK_TYPE, "test")
               .onConflictDoNothing()
               .execute();
            ctx.insertInto(CATALOG_LINKS,
                    CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER,
                    CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
               .values(TENANT, SEED_TUMBLER, REFONLY_TUMBLER, LINK_TYPE, "test")
               .onConflictDoNothing()
               .execute();

            // One shared topic, both chashes assigned to it (topic-scoped).
            long topicId = ctx.insertInto(TOPICS,
                    TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.CREATED_AT)
               .values(TENANT, TOPIC_LABEL, COL, OffsetDateTime.parse("2026-01-01T00:00:00+00:00"))
               .returning(TOPICS.ID)
               .fetchOne()
               .getId();
            for (String chash : List.of(FULL_CHASH, REFONLY_CHASH)) {
                ctx.insertInto(TOPIC_ASSIGNMENTS,
                        TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID,
                        TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                   .values(TENANT, HexFormat.of().parseHex(chash), topicId, COL,
                        OffsetDateTime.parse("2026-01-01T00:00:00+00:00"))
                   .onConflict(TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID)
                   .doNothing()
                   .execute();
            }
        }

        // One document_aspects row per tumbler (aspect-scoped's inner join requires one to
        // exist at all; field/pattern/confidence are left unfiltered in the calls below).
        for (String tumbler : List.of(FULL_TUMBLER, REFONLY_TUMBLER)) {
            long aspectId = aspectRepo.upsertAspect(TENANT, Map.of(
                "collection", COL,
                "source_path", tumbler + ".md",
                "extracted_at", "2026-01-01T00:00:00+00:00",
                "model_version", "v1",
                "extractor_name", "test",
                "source_uri", "file:///" + tumbler + ".md",
                "confidence", 0.9,
                "doc_id", tumbler,
                "problem_formulation", "retention probe " + tumbler));
            assertThat(aspectId).as("aspect seed for %s must be accepted (confidence above floor)", tumbler)
                .isPositive();
        }
    }

    // -------------------------------------------------------------------------
    // search_metadata_scoped_<dim> (document-level, id = tumbler)
    // -------------------------------------------------------------------------

    @Test
    void metadataScoped_returnsBothRows_withCorrectRetentionAndContent() {
        List<Map<String, Object>> rows = repo.searchMetadataScoped(
            TENANT, QUERY_TEXT, List.of(COL), null, null, null, null, N_RESULTS);

        Map<String, Object> full = byId(rows, FULL_TUMBLER);
        assertThat(full.get("retention")).isEqualTo("full");
        assertThat(full.get("content")).isEqualTo(FULL_TEXT);

        Map<String, Object> refOnly = byId(rows, REFONLY_TUMBLER);
        assertThat(refOnly.get("retention")).isEqualTo("reference-only");
        assertThat(refOnly.get("content")).isNull();
    }

    // -------------------------------------------------------------------------
    // search_graph_hop_<dim> (document-level, id = tumbler, seeded from SEED_TUMBLER)
    // -------------------------------------------------------------------------

    @Test
    void graphHop_returnsBothRows_withCorrectRetentionAndContent() {
        List<Map<String, Object>> rows = repo.searchGraphHop(
            TENANT, QUERY_TEXT, List.of(SEED_TUMBLER), List.of(COL), LINK_TYPE, 1, "out", N_RESULTS);

        Map<String, Object> full = byId(rows, FULL_TUMBLER);
        assertThat(full.get("retention")).isEqualTo("full");
        assertThat(full.get("content")).isEqualTo(FULL_TEXT);

        Map<String, Object> refOnly = byId(rows, REFONLY_TUMBLER);
        assertThat(refOnly.get("retention")).isEqualTo("reference-only");
        assertThat(refOnly.get("content")).isNull();
    }

    // -------------------------------------------------------------------------
    // search_topic_scoped_<dim> (chunk-level, id = chash)
    // -------------------------------------------------------------------------

    @Test
    void topicScoped_returnsBothRows_withCorrectRetentionAndContent() {
        List<Map<String, Object>> rows =
            repo.searchTopicScoped(TENANT, QUERY_TEXT, TOPIC_LABEL, COL, N_RESULTS);

        Map<String, Object> full = byId(rows, FULL_CHASH);
        assertThat(full.get("retention")).isEqualTo("full");
        assertThat(full.get("content")).isEqualTo(FULL_TEXT);

        Map<String, Object> refOnly = byId(rows, REFONLY_CHASH);
        assertThat(refOnly.get("retention")).isEqualTo("reference-only");
        assertThat(refOnly.get("content")).isNull();
    }

    // -------------------------------------------------------------------------
    // search_aspect_scoped_<dim> (document-level, id = tumbler)
    // -------------------------------------------------------------------------

    @Test
    void aspectScoped_returnsBothRows_withCorrectRetentionAndContent() {
        List<Map<String, Object>> rows = repo.searchAspectScopedWithTokens(
            TENANT, QUERY_TEXT, List.of(COL), null, null, null, null, N_RESULTS).value();

        Map<String, Object> full = byId(rows, FULL_TUMBLER);
        assertThat(full.get("retention")).isEqualTo("full");
        assertThat(full.get("content")).isEqualTo(FULL_TEXT);

        Map<String, Object> refOnly = byId(rows, REFONLY_TUMBLER);
        assertThat(refOnly.get("retention")).isEqualTo("reference-only");
        assertThat(refOnly.get("content")).isNull();
    }

    private static Map<String, Object> byId(List<Map<String, Object>> rows, String id) {
        return rows.stream()
            .filter(r -> id.equals(r.get("id")))
            .findFirst()
            .orElseThrow(() -> new AssertionError(
                "expected a row with id='" + id + "' among " + rows));
    }

    // -------------------------------------------------------------------------
    // Schema-level: every one of the 12 functions' RETURNS TABLE carries retention
    // -------------------------------------------------------------------------

    /**
     * jOOQ-typed catalog probe (no raw SQL, {@link PgCatalogProbes#routineSignature}) that
     * the RETURNS TABLE of all 12 functions vectors-016-combined-query-retention.xml
     * touches names a {@code retention} column — a schema-shape assertion independent of
     * the row-level tests above, so a function whose DDL regressed but whose Java caller
     * happened not to exercise the field would still be caught here.
     */
    @Test
    void allTwelveFunctions_returnsTableIncludesRetention() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int dim : new int[]{384, 768, 1024}) {
                for (String prefix : new String[]{
                        "search_metadata_scoped_", "search_graph_hop_",
                        "search_topic_scoped_", "search_aspect_scoped_"}) {
                    String name = prefix + dim;
                    PgCatalogProbes.RoutineSignature sig =
                        PgCatalogProbes.routineSignature(ctx, "nexus", name);
                    assertThat(sig).as("nexus.%s must exist", name).isNotNull();
                    assertThat(sig.result())
                        .as("nexus.%s's RETURNS TABLE must carry a retention column "
                            + "(RDR-169 Gap 2 remainder, bead nexus-4k1vz)", name)
                        .contains("retention");
                }
            }
        }
    }
}
