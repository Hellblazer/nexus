// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 1 (bead nexus-ft04v.9) — the GH #1461 profile-change journey,
 * end to end on the engine substrate, as ONE test that crosses a real
 * service restart: boot in ONNX mode, register + write real chunks to a
 * {@code code} (bge-768) collection, then simulate the restart (RDR-204
 * Technical Design 1a: {@code nx config set} writes nothing — the restart
 * IS the profile write, modelled here exactly as {@code Main.java} performs
 * it, by constructing a NEW voyage-mode {@link EmbedderRouter} and calling
 * {@link EmbedderRouter#seedEmbeddingProfile}) with a Voyage key, and proves:
 *
 * <ol>
 *   <li>the tenant's {@code nexus.embedding_profile} rows now name the
 *       Voyage models for every content type;</li>
 *   <li>the EXISTING bge collection's {@code catalog_collections} row is
 *       byte-for-byte unchanged (model, dimension, lifecycle_state) —
 *       Technical Design 1a: "a profile change never touches an existing
 *       collection's row";</li>
 *   <li>the bge collection is STILL SEARCHABLE — a real vector search
 *       against it, using an embedder compatible with the row's OWN model
 *       (bge-768), returns the chunk written in step 1. {@code
 *       resolveEmbedderStrict} is not yet repointed at the catalog registry
 *       (that is Phase 2 item 2 — see the Phase 1 critique, T2
 *       nexus/critique-rdr-204-phase1-2b18f802f..be19f58b7), so this proves
 *       the DATA and the "route by the row's own model" CONTRACT (Technical
 *       Design 1a: "reads... continue to route by the row's own model and
 *       are never refused") rather than an automatic dispatch this phase
 *       has not built yet;</li>
 *   <li>a new write for the SAME content type and owner mints a SIBLING
 *       collection carrying the Voyage model — a real chunk lands there too
 *       (a hermetic stand-in embedder self-keyed to the {@code
 *       voyage-code-3} token, so no live Voyage network call is made) — and
 *       the bge row/collection remain untouched afterward.</li>
 * </ol>
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17. The "Voyage mode"
 * router used to drive the boot-time profile seed ({@code
 * EmbedderRouter(String voyageApiKey, String inputType)}, the exact
 * constructor {@code Main.java} uses when {@code NX_VOYAGE_API_KEY} is
 * present) never has {@code embed()} called on it — only {@code
 * modelToken()} reads (same idiom as {@code
 * CatalogHandlerCollectionUpsertProfileModelTest}, "dummy key: this path
 * never calls embed(), only modelToken() reads"). The sibling collection's
 * actual chunk write instead goes through a separate, deterministic
 * fake embedder self-keyed to {@code voyage-code-3} (the same technique
 * {@code EmbedderRouterEmbeddingProfileSeedTest}'s {@code FakeBge} and
 * {@code CombinedWriteRepositoryTest}'s {@code CountingFakeEmbedder} use),
 * so the write path is exercised for real without a live network call.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr204Gh1461ProfileRestartJourneyTest {

    private static final String TENANT = "gh1461-journey-tenant";

    private static final String COL_BGE    = "code__" + TENANT + "__bge-base-en-v15-768__v1";
    private static final String COL_VOYAGE = "code__" + TENANT + "__voyage-code-3__v1";

    private static final String CHUNK_TEXT_BGE    = "gh1461 journey bge chunk text";
    private static final String CHUNK_TEXT_VOYAGE = "gh1461 journey voyage sibling chunk text";

    /** Deterministic bge-768 stand-in: one-hot at index {@code text.hashCode() % 768}. */
    private static final class FakeBge768 implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                float[] v = new float[768];
                v[Math.floorMod(t.hashCode(), 768)] = 1.0f;
                out.add(v);
            }
            return out;
        }
        @Override public String modelToken() { return "bge-base-en-v15-768"; }
    }

    /**
     * Deterministic voyage-code-3 stand-in — same one-hot technique, 1024-dim,
     * self-keyed to the real Voyage code token. Used ONLY for the sibling's
     * chunk write, never for the profile-seed router (which must be the REAL
     * production Voyage constructor so the per-content-type mapping — code
     * vs everything-else — is the genuine one, not a single-model local-mode
     * mapping).
     */
    private static final class FakeVoyageCode1024 implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                float[] v = new float[1024];
                v[Math.floorMod(t.hashCode(), 1024)] = 1.0f;
                out.add(v);
            }
            return out;
        }
        @Override public String modelToken() { return "voyage-code-3"; }
    }

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── read helpers (superuser probe connection, bypasses RLS by design —
    //    same idiom as EmbedderRouterEmbeddingProfileSeedTest/
    //    CatalogHandlerCollectionUpsertProfileModelTest) ──────────────────

    private Map<String, String> profileModelsByContentType(String tenant) {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var rows = ctx.select(EMBEDDING_PROFILE.CONTENT_TYPE, EMBEDDING_PROFILE.EMBEDDING_MODEL)
                    .from(EMBEDDING_PROFILE)
                    .where(EMBEDDING_PROFILE.TENANT_ID.eq(tenant))
                    .fetch();
            Map<String, String> out = new LinkedHashMap<>();
            for (var r : rows) {
                out.put(r.get(EMBEDDING_PROFILE.CONTENT_TYPE), r.get(EMBEDDING_PROFILE.EMBEDDING_MODEL));
            }
            return out;
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private record CollectionRow(String embeddingModel, Integer dimension, String lifecycleState) {}

    private CollectionRow collectionRow(String tenant, String name) {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var r = ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                    .from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                    .and(CATALOG_COLLECTIONS.NAME.eq(name))
                    .fetchOne();
            if (r == null) return null;
            return new CollectionRow(r.value1(), r.value2(), r.value3());
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private boolean chunkExists(String tenant, String collection, String hexChash, int dim) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // The dim-specific embedding column has no fixed generated field (one per width),
            // so it is resolved by name against the generated CHUNKS table.
            var embedding = DSL.field(DSL.name(DimTables.embeddingColumn(dim)), Object.class);
            return ctx.fetchExists(
                ctx.selectOne()
                    .from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(tenant))
                    .and(CHUNKS.COLLECTION.eq(collection))
                    .and(CHUNKS.CHASH.eq(java.util.HexFormat.of().parseHex(hexChash)))
                    .and(embedding.isNotNull()));
        }
    }

    private static Map<String, Object> chunk(String chash, String text) {
        return Map.of("chash", chash, "text", text, "metadata", Map.of());
    }

    private static Map<String, Object> row(int position, String chash) {
        return Map.of("position", position, "chash", chash, "chunk_index", position);
    }

    private static Map<String, Object> doc(String docId, List<Map<String, Object>> rows) {
        return Map.of("doc_id", docId, "rows", rows);
    }

    // ── the journey ──────────────────────────────────────────────────────

    @Test
    void gh1461_bootOnnx_setVoyageKey_restart_bgeStillReadable_newWriteMintsVoyageSibling() throws Exception {
        String chashBge = Chash.ofText("gh1461-bge-chunk").toHex();

        // ── Step 1: boot in ONNX mode. Profile is bge-768 for every content
        //    type. Register + write real chunks so a bge collection exists
        //    with vectors. ───────────────────────────────────────────────
        FakeBge768 bge = new FakeBge768();
        EmbedderRouter routerOnnxDoc   = new EmbedderRouter(bge, "document");
        EmbedderRouter routerOnnxQuery = new EmbedderRouter(bge, "query");

        routerOnnxDoc.seedEmbeddingProfile(tenantScope, TENANT);
        var bootProfile = profileModelsByContentType(TENANT);
        assertThat(bootProfile.keySet())
            .as("boot seeds one profile row per content type")
            .containsExactlyInAnyOrder("code", "docs", "rdr", "knowledge", "unknown");
        for (var e : bootProfile.entrySet()) {
            assertThat(e.getValue())
                .as("ONNX-mode boot: content type %s gets bge-768", e.getKey())
                .isEqualTo("bge-base-en-v15-768");
        }

        catalogRepo.upsertCollection(TENANT, Map.of(
            "name", COL_BGE, "content_type", "code", "owner_id", TENANT, "model_version", "v1"));
        var bgeRowAtBoot = collectionRow(TENANT, COL_BGE);
        assertThat(bgeRowAtBoot).as("bge collection registered at boot").isNotNull();
        assertThat(bgeRowAtBoot.embeddingModel()).isEqualTo("bge-base-en-v15-768");
        assertThat(bgeRowAtBoot.dimension()).isEqualTo(768);
        assertThat(bgeRowAtBoot.lifecycleState()).isEqualTo("live");

        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gh1461.1", "title", "gh1461-journey-doc",
            "content_type", "code", "corpus", "code",
            "physical_collection", COL_BGE, "chunk_count", 0));

        CombinedWriteService writeOnnx = new CombinedWriteService(tenantScope, catalogRepo, routerOnnxDoc);
        writeOnnx.writeManyCombined(TENANT, COL_BGE,
            List.of(chunk(chashBge, CHUNK_TEXT_BGE)),
            List.of(doc("gh1461.1", List.of(row(0, chashBge)))),
            null, false, false);

        assertThat(chunkExists(TENANT, COL_BGE, chashBge, 768))
            .as("step 1: a real bge-768 chunk landed in nexus.chunks")
            .isTrue();

        // ── Step 2/3: set a Voyage key and RESTART the service against the
        //    SAME database — the restart IS the profile write (Technical
        //    Design 1a: `nx config set` writes nothing). Modelled exactly
        //    as Main.java's Voyage branch constructs its router. ─────────
        EmbedderRouter routerVoyageProfile = new EmbedderRouter("dummy-voyage-key", "document");
        routerVoyageProfile.seedEmbeddingProfile(tenantScope, TENANT);

        // ── Step 4: the profile now names the Voyage models for EVERY
        //    content type. ─────────────────────────────────────────────
        var restartedProfile = profileModelsByContentType(TENANT);
        assertThat(restartedProfile.get("code"))
            .as("step 4: code content type now profiled to voyage-code-3")
            .isEqualTo("voyage-code-3");
        for (String contentType : List.of("docs", "rdr", "knowledge", "unknown")) {
            assertThat(restartedProfile.get(contentType))
                .as("step 4: content type %s now profiled to voyage-context-3", contentType)
                .isEqualTo("voyage-context-3");
        }

        // ── Step 5: the EXISTING bge collection's row is UNCHANGED, and a
        //    vector search against it still returns results — reads route
        //    by the row's own model and are never refused (Technical
        //    Design 1a). resolveEmbedderStrict is not yet repointed at the
        //    registry (Phase 2), so the search below uses an embedder
        //    compatible with the row's OWN recorded model (bge-768) —
        //    proving the contract this phase owns (the data survives the
        //    restart, untouched and queryable), not the not-yet-built
        //    automatic per-row dispatch. ─────────────────────────────────
        var bgeRowAfterRestart = collectionRow(TENANT, COL_BGE);
        assertThat(bgeRowAfterRestart)
            .as("step 5: the bge row must be UNCHANGED after the restart")
            .isEqualTo(bgeRowAtBoot);

        PgVectorRepository searchOnnx = new PgVectorRepository(tenantScope, routerOnnxDoc, routerOnnxQuery);
        var searchResults = searchOnnx.search(TENANT, CHUNK_TEXT_BGE, List.of(COL_BGE), 5, null);
        assertThat(searchResults)
            .as("step 5: a vector search against the bge collection returns results after the switch")
            .isNotEmpty();
        assertThat(searchResults.get(0).get("id"))
            .as("step 5: the top result is the chunk written before the restart")
            .isEqualTo(chashBge);

        // ── Step 6: a new write for the SAME content type and owner mints
        //    a SIBLING collection carrying the Voyage model — a real chunk
        //    lands there (hermetic stand-in, no live Voyage call) — and the
        //    bge row is STILL unchanged afterward. ──────────────────────
        catalogRepo.upsertCollection(TENANT, Map.of(
            "name", COL_VOYAGE, "content_type", "code", "owner_id", TENANT, "model_version", "v1"));
        var voyageRow = collectionRow(TENANT, COL_VOYAGE);
        assertThat(voyageRow).as("step 6: the Voyage sibling is registered").isNotNull();
        assertThat(voyageRow.embeddingModel())
            .as("step 6: the sibling carries the (now-Voyage) profile's model")
            .isEqualTo("voyage-code-3");
        assertThat(voyageRow.dimension()).isEqualTo(1024);
        assertThat(voyageRow.lifecycleState()).isEqualTo("live");
        assertThat(voyageRow)
            .as("step 6: the sibling is a genuinely NEW row, not the bge one re-pointed")
            .isNotEqualTo(bgeRowAtBoot);

        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gh1461.2", "title", "gh1461-journey-voyage-sibling-doc",
            "content_type", "code", "corpus", "code",
            "physical_collection", COL_VOYAGE, "chunk_count", 0));

        EmbedderRouter routerVoyageWrite = new EmbedderRouter(new FakeVoyageCode1024(), "document");
        CombinedWriteService writeVoyage = new CombinedWriteService(tenantScope, catalogRepo, routerVoyageWrite);
        String chashVoyage = Chash.ofText("gh1461-voyage-sibling-chunk").toHex();
        writeVoyage.writeManyCombined(TENANT, COL_VOYAGE,
            List.of(chunk(chashVoyage, CHUNK_TEXT_VOYAGE)),
            List.of(doc("gh1461.2", List.of(row(0, chashVoyage)))),
            null, false, false);

        assertThat(chunkExists(TENANT, COL_VOYAGE, chashVoyage, 1024))
            .as("step 6: a real voyage-1024 chunk landed in the sibling collection")
            .isTrue();

        var bgeRowAfterSiblingWrite = collectionRow(TENANT, COL_BGE);
        assertThat(bgeRowAfterSiblingWrite)
            .as("step 6: the bge row is STILL unchanged after the sibling write")
            .isEqualTo(bgeRowAtBoot);
        assertThat(chunkExists(TENANT, COL_BGE, chashBge, 768))
            .as("step 6: the original bge chunk is still there, untouched by the sibling write")
            .isTrue();
    }
}
