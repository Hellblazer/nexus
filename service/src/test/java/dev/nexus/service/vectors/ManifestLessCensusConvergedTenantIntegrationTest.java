// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-236 Phase 3 (bead nexus-6pfgb.39): once a tenant has its rdr192 completion record
 * ({@code nexus.ladder_completions}, rung {@link Rdr192BackfillGate#RUNG_NAME}) the manifest-less census
 * stops reading {@code catalog_doc_id}/{@code doc_id} off the chunk row, because the Phase 4 sweep removes
 * those keys. A manifest-less chunk is then attributed by the REVERSE path alone; a chunk with no reverse
 * match is {@code no-owner} whatever stale keys its row still carries.
 *
 * <p>Three tenants carry the same fixture: PRE (no completion record, keys present: the forward join still
 * runs, the control), KEYS (completion record, keys present) and STRIPPED (completion record, keys removed).
 * The record, not the bucket, is the condition: a dropped join would read {@code legacy-unmanifested} as zero
 * vacuously, so the PRE tenant pins that the join is still on without the record.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestLessCensusConvergedTenantIntegrationTest {

    private static final String SVC_ROLE = "svc_6pfgb39_census";
    private static final String SVC_PASS = "svc_6pfgb39_census_pass";
    private static final String TENANT_PRE = "6pfgb39-pre";
    private static final String TENANT_KEYS = "6pfgb39-keys";
    private static final String TENANT_STRIPPED = "6pfgb39-stripped";
    private static final String COLLECTION = "knowledge__6pfgb39-fixture__minilm-l6-v2-384__v1";

    private static final List<String> BUCKETS =
        List.of("superseded", "legacy-unmanifested", "dead-owner", "no-owner", "unclassified");

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CatalogRepository catalogRepo;
    private LadderRepository ladder;
    private PgVectorRepository vecRepo;

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private static final String CURRENT = ch("6pfgb39-current");
    private static final String SUPERSEDED = ch("6pfgb39-superseded");
    private static final String LEGACY_FWD = ch("6pfgb39-legacy-forward");
    private static final String DEAD = ch("6pfgb39-dead-owner");
    private static final String GHOST = ch("6pfgb39-ghost");
    private static final String REV_ONLY = ch("6pfgb39-reverse-only");

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
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(tenantScope);
        ladder = new LadderRepository(tenantScope);
        var embedder = new ConstantEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        for (String tenant : List.of(TENANT_PRE, TENANT_KEYS, TENANT_STRIPPED)) {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, COLLECTION);
            }
        }
        seed(TENANT_PRE, true);
        seed(TENANT_KEYS, true);
        ladder.record(TENANT_KEYS, Rdr192BackfillGate.RUNG_NAME, "test", "");
        seed(TENANT_STRIPPED, false);
        ladder.record(TENANT_STRIPPED, Rdr192BackfillGate.RUNG_NAME, "test", "");
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void doc(String tenant, String tumbler, Map<String, Object> metadata) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("tumbler", tumbler);
        d.put("title", "6pfgb39 fixture " + tumbler);
        d.put("content_type", "prose");
        d.put("corpus", "knowledge");
        d.put("physical_collection", COLLECTION);
        if (metadata != null) d.put("metadata", metadata);
        catalogRepo.upsertDocument(tenant, d);
    }

    private Map<String, Object> keys(boolean withKeys, String owner) {
        return withKeys ? Map.of("catalog_doc_id", owner) : Map.of();
    }

    /**
     * The same shapes for every tenant. With the row keys present and the forward join on they classify as
     * superseded, legacy-unmanifested, dead-owner, no-owner (the ghost) and (reverse) legacy-unmanifested.
     */
    private void seed(String tenant, boolean withKeys) {
        // superseded: live owner with a manifest row in the collection for ANOTHER chash.
        doc(tenant, "d-sup", null);
        vecRepo.upsertChunks(tenant, COLLECTION, List.of(CURRENT), List.of("current text"), List.of(Map.of()));
        catalogRepo.writeManifest(tenant, "d-sup", COLLECTION,
            List.of(Map.<String, Object>of("position", 0, "chash", CURRENT, "chunk_index", 0)));
        vecRepo.upsertChunks(tenant, COLLECTION, List.of(SUPERSEDED), List.of("superseded text"),
            List.of(keys(withKeys, "d-sup")));

        // legacy-unmanifested through the forward key: live owner, no manifest row anywhere.
        doc(tenant, "d-leg", null);
        vecRepo.upsertChunks(tenant, COLLECTION, List.of(LEGACY_FWD), List.of("legacy forward text"),
            List.of(keys(withKeys, "d-leg")));

        // dead-owner: the owner is tombstoned.
        doc(tenant, "d-dead", null);
        catalogRepo.deleteDocument(tenant, "d-dead");
        vecRepo.upsertChunks(tenant, COLLECTION, List.of(DEAD), List.of("dead owner text"),
            List.of(keys(withKeys, "d-dead")));

        // no-owner: the key names a tumbler never registered.
        vecRepo.upsertChunks(tenant, COLLECTION, List.of(GHOST), List.of("ghost text"),
            List.of(keys(withKeys, "never-registered")));

        // reverse: a live note whose own metadata.doc_id names the chash; the row carries no key at all.
        doc(tenant, "d-rev", Map.of("doc_id", REV_ONLY));
        vecRepo.upsertChunks(tenant, COLLECTION, List.of(REV_ONLY), List.of("reverse only text"),
            List.of(Map.of()));
    }

    private Map<String, List<String>> buckets(String tenant) {
        var result = vecRepo.manifestLessCensus(tenant, COLLECTION, 300, 0);
        Map<String, List<String>> out = new LinkedHashMap<>();
        for (String b : BUCKETS) out.put(b, result.chashes().get(b).stream().sorted().toList());
        return out;
    }

    @Test
    void aTenantWithoutTheCompletionRecordStillAttributesByTheRowKeys() {
        Map<String, List<String>> b = buckets(TENANT_PRE);

        assertThat(b.get("superseded")).containsExactly(SUPERSEDED);
        assertThat(b.get("legacy-unmanifested")).containsExactlyInAnyOrder(LEGACY_FWD, REV_ONLY);
        assertThat(b.get("dead-owner")).containsExactly(DEAD);
        assertThat(b.get("no-owner")).containsExactly(GHOST);
        assertThat(b.get("unclassified")).isEmpty();
    }

    @Test
    void aConvergedTenantAttributesByTheReversePathOnly_staleRowKeysAreNotRead() {
        Map<String, List<String>> b = buckets(TENANT_KEYS);

        assertThat(b.get("legacy-unmanifested")).as("only the reverse match survives").containsExactly(REV_ONLY);
        assertThat(b.get("no-owner"))
            .as("a keyed row with no reverse match is no-owner: the stale key names a live, a dead and a ghost owner")
            .containsExactlyInAnyOrder(SUPERSEDED, LEGACY_FWD, DEAD, GHOST);
        assertThat(b.get("superseded")).isEmpty();
        assertThat(b.get("dead-owner")).isEmpty();
        assertThat(b.get("unclassified")).isEmpty();
    }

    @Test
    void theBucketsAreIdenticalWithTheKeysPresentAndStrippedAfterConvergence() {
        assertThat(buckets(TENANT_STRIPPED)).isEqualTo(buckets(TENANT_KEYS));
        var keys = vecRepo.manifestLessCensus(TENANT_KEYS, COLLECTION, 300, 0);
        var stripped = vecRepo.manifestLessCensus(TENANT_STRIPPED, COLLECTION, 300, 0);
        assertThat(stripped.totals()).isEqualTo(keys.totals());
        assertThat(stripped.scopeChunkTotal()).isEqualTo(keys.scopeChunkTotal());
        assertThat(stripped.owners().get(REV_ONLY)).containsEntry("owner_path", "reverse");
        assertThat(keys.owners().get(LEGACY_FWD)).as("no owner is reported for a chunk the join no longer finds")
            .containsEntry("owner_tumbler", null).containsEntry("owner_path", null);
    }

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
