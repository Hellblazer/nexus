// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 2 (bead nexus-wbfpw.4): {@link PgVectorRepository#manifestLessCensus}
 * classifies every chunk in a collection carrying no OWN-COLLECTION manifest row
 * into exactly one of five buckets, following the MVV (a) producer-to-bucket mapping
 * (docs/rdr/rdr-192-superseded-note-chunks-outlive-manifest-less-is-live.md).
 *
 * <p>One seeded row per bucket, plus a "current" chunk that DOES carry an
 * own-collection manifest row (must be excluded from the census entirely) and a
 * second fallback-lookup row proving {@code catalog_doc_id} absent falls back to
 * {@code doc_id}. Fixture shape:
 * <ul>
 *   <li><strong>superseded</strong> — doc D1 live, has a manifest row in collection
 *       A for chash CURRENT; test chash OLD carries {@code catalog_doc_id=D1} and
 *       no manifest row of its own (a lost reap).</li>
 *   <li><strong>legacy-unmanifested</strong> — doc D2 live, zero manifest rows
 *       anywhere; test chash LEGACY carries {@code catalog_doc_id=D2}. A second
 *       chash, LEGACY_FALLBACK, carries only {@code doc_id=D2B} (no
 *       {@code catalog_doc_id}) against an equally manifest-less live doc D2B,
 *       proving the fallback lookup.</li>
 *   <li><strong>dead-owner</strong> — two distinct producers: doc D3 is tombstoned
 *       (chash TOMBSTONED); doc D4 is live but its only manifest row is in
 *       collection B, not A (chash RENAME_COPY) — the rename-COPY leftover,
 *       {@code CatalogRepository.java} ~8101-8125.</li>
 *   <li><strong>no-owner</strong> — chash NO_OWNER_EMPTY carries no
 *       {@code catalog_doc_id}/{@code doc_id} at all (a {@code .nxexp} import);
 *       chash NO_OWNER_GHOST names a tumbler that was never registered.</li>
 *   <li><strong>unclassified</strong> — structurally unreachable given today's
 *       schema (the CASE in {@code manifest_less_census.sql} is exhaustive over
 *       owner-found/tombstoned/live x total-manifest-count/own-manifest-count);
 *       this suite instead pins the NEGATIVE property the bucket exists to
 *       guarantee — every one of the 7 manifest-less rows gets a non-null bucket
 *       and the returned count equals the seeded population, so nothing is ever
 *       silently dropped.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestLessCensusIntegrationTest {

    private static final String TENANT = "wbfpw4-census";
    private static final String SVC_ROLE = "svc_wbfpw4_census";
    private static final String SVC_PASS = "svc_wbfpw4_census_pass";
    private static final String COLLECTION_A = "knowledge__wbfpw4-fixture-a__minilm-l6-v2-384__v1";
    private static final String COLLECTION_B = "knowledge__wbfpw4-fixture-b__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    PgVectorRepository vecRepo;
    HikariDataSource svcDs;

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

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
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(tenantScope);
        var embedder = new ConstantEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION_A);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION_B);
        }

        seedFixture();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture chashes ──────────────────────────────────────────────────────

    private static final String CURRENT          = ch("wbfpw4-current");
    private static final String OLD              = ch("wbfpw4-old-superseded");
    private static final String LEGACY           = ch("wbfpw4-legacy");
    private static final String LEGACY_FALLBACK  = ch("wbfpw4-legacy-fallback");
    private static final String TOMBSTONED       = ch("wbfpw4-tombstoned");
    private static final String RENAME_COPY      = ch("wbfpw4-rename-copy");
    private static final String MANIFEST_B_ONLY  = ch("wbfpw4-manifest-b-only");
    private static final String NO_OWNER_EMPTY   = ch("wbfpw4-no-owner-empty");
    private static final String NO_OWNER_GHOST   = ch("wbfpw4-no-owner-ghost");

    private static final String D1  = "wbfpw4-doc-superseded";
    private static final String D2  = "wbfpw4-doc-legacy";
    private static final String D2B = "wbfpw4-doc-legacy-fallback";
    private static final String D3  = "wbfpw4-doc-tombstoned";
    private static final String D4  = "wbfpw4-doc-rename-copy";

    private void registerDoc(String tumbler, String physicalCollection) {
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler,
            "title", "RDR-192 census fixture " + tumbler,
            "content_type", "prose",
            "corpus", "knowledge",
            "physical_collection", physicalCollection
        ));
    }

    private void seedFixture() {
        // D1: live, manifest row in A for CURRENT; OLD is the superseded leftover.
        registerDoc(D1, COLLECTION_A);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(CURRENT), List.of("current text"),
            List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, D1, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", CURRENT, "chunk_index", 0)));
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(OLD), List.of("old superseded text"),
            List.of(Map.of("catalog_doc_id", D1)));

        // D2: live, zero manifest rows anywhere -> legacy-unmanifested via catalog_doc_id.
        registerDoc(D2, COLLECTION_A);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(LEGACY), List.of("legacy note text"),
            List.of(Map.of("catalog_doc_id", D2)));

        // D2B: live, zero manifest rows anywhere -> legacy-unmanifested via the doc_id FALLBACK
        // (no catalog_doc_id present at all).
        registerDoc(D2B, COLLECTION_A);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(LEGACY_FALLBACK), List.of("legacy fallback text"),
            List.of(Map.of("doc_id", D2B)));

        // D3: tombstoned -> dead-owner regardless of manifest state.
        registerDoc(D3, COLLECTION_A);
        catalogRepo.deleteDocument(TENANT, D3);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(TOMBSTONED), List.of("tombstoned owner text"),
            List.of(Map.of("catalog_doc_id", D3)));

        // D4: live, but its ONLY manifest row is in collection B (rename-COPY leftover) ->
        // dead-owner in A even though the doc itself is live.
        registerDoc(D4, COLLECTION_B);
        vecRepo.upsertChunks(TENANT, COLLECTION_B, List.of(MANIFEST_B_ONLY), List.of("manifest b only text"),
            List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, D4, COLLECTION_B,
            List.of(Map.<String, Object>of("position", 0, "chash", MANIFEST_B_ONLY, "chunk_index", 0)));
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(RENAME_COPY), List.of("rename copy leftover text"),
            List.of(Map.of("catalog_doc_id", D4)));

        // no-owner: no catalog_doc_id/doc_id at all, and a catalog_doc_id naming a tumbler
        // that was never registered.
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(NO_OWNER_EMPTY), List.of("no owner empty text"),
            List.of(Map.of()));
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(NO_OWNER_GHOST), List.of("no owner ghost text"),
            List.of(Map.of("catalog_doc_id", "wbfpw4-doc-never-registered")));
    }

    // ── assertions ───────────────────────────────────────────────────────────

    @Test
    void classifiesEveryManifestLessChunkIntoExactlyOneBucket() {
        var result = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 300, 0);

        assertThat(result.returned()).isEqualTo(7);
        assertThat(result.chashes().get("superseded")).containsExactlyInAnyOrder(OLD);
        assertThat(result.chashes().get("legacy-unmanifested"))
            .containsExactlyInAnyOrder(LEGACY, LEGACY_FALLBACK);
        assertThat(result.chashes().get("dead-owner"))
            .containsExactlyInAnyOrder(TOMBSTONED, RENAME_COPY);
        assertThat(result.chashes().get("no-owner"))
            .containsExactlyInAnyOrder(NO_OWNER_EMPTY, NO_OWNER_GHOST);
        assertThat(result.chashes().get("unclassified")).isEmpty();

        assertThat(result.counts().get("superseded")).isEqualTo(1L);
        assertThat(result.counts().get("legacy-unmanifested")).isEqualTo(2L);
        assertThat(result.counts().get("dead-owner")).isEqualTo(2L);
        assertThat(result.counts().get("no-owner")).isEqualTo(2L);
        assertThat(result.counts().get("unclassified")).isEqualTo(0L);

        // CURRENT carries its own-collection manifest row -- excluded from the
        // census population entirely, never labeled into any bucket.
        Set<String> allReturned = new HashSet<>();
        result.chashes().values().forEach(allReturned::addAll);
        assertThat(allReturned).doesNotContain(CURRENT);
        assertThat(allReturned).hasSize(7);
    }

    @Test
    void pagesWithoutLosingOrDuplicatingRows() {
        Set<String> paged = new HashSet<>();
        int offset = 0;
        int limit = 3;
        int pages = 0;
        while (true) {
            var page = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, limit, offset);
            page.chashes().values().forEach(paged::addAll);
            pages++;
            if (page.returned() < limit) break;
            offset += limit;
            assertThat(pages).isLessThan(10); // non-vacuity: this loop must terminate
        }

        var full = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 300, 0);
        Set<String> fullSet = new HashSet<>();
        full.chashes().values().forEach(fullSet::addAll);

        assertThat(paged).isEqualTo(fullSet);
        assertThat(paged).hasSize(7);
        assertThat(pages).isGreaterThan(1); // proves pagination actually exercised >1 page
    }

    /** Minimal {@link Embedder}: content of the vector is irrelevant — only chash/collection/metadata movement is under test. */
    private static final class ConstantEmbedder implements Embedder {
        private final int dim;
        ConstantEmbedder(int dim) { this.dim = dim; }

        @Override
        public List<float[]> embed(List<String> texts) {
            float[] v = new float[dim];
            v[0] = 1.0f;
            return texts.stream().map(t -> v.clone()).toList();
        }

        @Override
        public void close() { }
    }
}
