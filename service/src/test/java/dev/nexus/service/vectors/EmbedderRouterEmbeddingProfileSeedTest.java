// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
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

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 1 (bead nexus-ft04v.6) — {@link EmbedderRouter}'s
 * {@code nexus.embedding_profile} seed methods against a real PG substrate:
 * ONNX-mode and Voyage-mode boot seeding, idempotent re-seed, a mode-switch
 * reboot overwriting stale rows, {@link CollectionRegistry} eviction, and the
 * lazy per-content-type cloud-tenant seam.
 *
 * <p>Both modes are exercised in every content-type assertion below — a run
 * that covered only one mode would be a vacuous pass per the bead's own
 * acceptance criteria.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, {@code nexus_svc} role
 * (full DML via {@code grants-nexus-svc.xml}), PER_CLASS — the same idiom as
 * {@code ChashRepositoryTest} / {@code CollectionRegistryTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class EmbedderRouterEmbeddingProfileSeedTest {

    private static final String LOCAL_TENANT = "profile-seed-onnx-tenant";
    private static final String VOYAGE_TENANT = "profile-seed-voyage-tenant";
    private static final String CLOUD_TENANT = "profile-seed-cloud-tenant";

    /** A stand-in for {@link Bge768Embedder} that needs no model file (mirrors EmbedderRouterBge768Test). */
    private static final class FakeBge implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> new float[768]).toList();
        }
        @Override public String modelToken() {
            return "bge-base-en-v15-768";
        }
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // NOTE: no CollectionRegistry.clearForTests() between tests here —
    // clearForTests() is package-private to dev.nexus.service.db (this test
    // lives in dev.nexus.service.vectors, EmbedderRouter's own package).
    // Not needed anyway: every test below uses a tenant/collection name
    // unique to itself, so cross-test CollectionRegistry state cannot
    // collide (CollectionRegistryTest, in the db package, already owns the
    // clearForTests()-guarded pure-cache contract tests for evict/evictTenant).

    /** Reads every embedding_profile row for {@code tenant} via a superuser probe connection (bypasses RLS by design, matching Catalog036EmbeddingProfileSchemaLiquibaseTest's own read idiom). */
    private Map<String, Map<String, Object>> profileRows(String tenant) {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var rows = ctx.select(EMBEDDING_PROFILE.CONTENT_TYPE, EMBEDDING_PROFILE.EMBEDDING_MODEL,
                                   EMBEDDING_PROFILE.DIMENSION)
                    .from(EMBEDDING_PROFILE)
                    .where(EMBEDDING_PROFILE.TENANT_ID.eq(tenant))
                    .fetch();
            Map<String, Map<String, Object>> out = new LinkedHashMap<>();
            for (var r : rows) {
                out.put(r.get(EMBEDDING_PROFILE.CONTENT_TYPE), Map.of(
                        "model", r.get(EMBEDDING_PROFILE.EMBEDDING_MODEL),
                        "dimension", r.get(EMBEDDING_PROFILE.DIMENSION)));
            }
            return out;
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    // ── boot seeding: both modes, all content types ─────────────────────────

    @Test
    void onnxMode_seedsBgeRow_perContentType() {
        EmbedderRouter router = new EmbedderRouter(new FakeBge(), "document");
        router.seedEmbeddingProfile(tenantScope, LOCAL_TENANT);

        var rows = profileRows(LOCAL_TENANT);
        assertThat(rows.keySet())
                .as("one row per content type: code, docs, rdr, knowledge, unknown")
                .containsExactlyInAnyOrder("code", "docs", "rdr", "knowledge", "unknown");
        for (var e : rows.entrySet()) {
            assertThat(e.getValue().get("model"))
                    .as("ONNX mode: every content type gets bge-768 (content type %s)", e.getKey())
                    .isEqualTo("bge-base-en-v15-768");
            assertThat(e.getValue().get("dimension")).isEqualTo(768);
        }
    }

    @Test
    void voyageMode_seedsVoyageCodeForCode_voyageContextForEverythingElse() {
        EmbedderRouter router = new EmbedderRouter("dummy-key", "document");
        router.seedEmbeddingProfile(tenantScope, VOYAGE_TENANT);

        var rows = profileRows(VOYAGE_TENANT);
        assertThat(rows.keySet())
                .containsExactlyInAnyOrder("code", "docs", "rdr", "knowledge", "unknown");
        assertThat(rows.get("code").get("model")).isEqualTo("voyage-code-3");
        assertThat(rows.get("code").get("dimension")).isEqualTo(1024);
        for (String contentType : List.of("docs", "rdr", "knowledge", "unknown")) {
            assertThat(rows.get(contentType).get("model"))
                    .as("Voyage mode: content type %s gets voyage-context-3", contentType)
                    .isEqualTo("voyage-context-3");
            assertThat(rows.get(contentType).get("dimension")).isEqualTo(1024);
        }
    }

    // ── idempotence + mode-switch reboot ────────────────────────────────────

    @Test
    void secondBoot_sameMode_changesNothing() {
        EmbedderRouter router = new EmbedderRouter(new FakeBge(), "document");
        String tenant = LOCAL_TENANT + "-repeat";
        router.seedEmbeddingProfile(tenantScope, tenant);
        var first = profileRows(tenant);

        router.seedEmbeddingProfile(tenantScope, tenant);
        var second = profileRows(tenant);

        assertThat(second).as("a second boot in the same mode must change no row's content")
                .isEqualTo(first);
    }

    @Test
    void modeSwitch_reboot_overwritesExistingRows() {
        String tenant = LOCAL_TENANT + "-switch";
        EmbedderRouter onnx = new EmbedderRouter(new FakeBge(), "document");
        onnx.seedEmbeddingProfile(tenantScope, tenant);
        assertThat(profileRows(tenant).get("code").get("model")).isEqualTo("bge-base-en-v15-768");

        EmbedderRouter voyage = new EmbedderRouter("dummy-key", "document");
        voyage.seedEmbeddingProfile(tenantScope, tenant);
        var rows = profileRows(tenant);
        assertThat(rows.get("code").get("model"))
                .as("a reboot in a NEW mode must overwrite the stale profile row, not leave it pinned")
                .isEqualTo("voyage-code-3");
        assertThat(rows.get("docs").get("model")).isEqualTo("voyage-context-3");
    }

    // ── CollectionRegistry eviction (non-vacuous: pre-populate, then observe the flip) ──

    @Test
    void profileWrite_evictsCollectionRegistry_forcingSubsequentReadToReFetch() {
        String tenant = LOCAL_TENANT + "-evict";
        String collection = "code__" + tenant + "__bge-base-en-v15-768__v1";
        // Pre-populate a KNOWN entry, simulating realistic prior-registration
        // state. Asserting the whole cache is empty afterward would be
        // vacuous if it started empty (Sam's decision, T2 204-research-17) —
        // this instead observes a cached TRUE flip to FALSE, i.e. the next
        // registration attempt for this exact collection must re-fetch/
        // re-verify against the database rather than trusting the stale fact.
        CollectionRegistry.markKnown(tenant, collection,
            new dev.nexus.service.db.CollectionRow("code", tenant, "bge-base-en-v15-768", 768, "live"));
        assertThat(CollectionRegistry.isKnown(tenant, collection)).isTrue();

        EmbedderRouter router = new EmbedderRouter(new FakeBge(), "document");
        router.seedEmbeddingProfile(tenantScope, tenant);

        assertThat(CollectionRegistry.isKnown(tenant, collection))
                .as("a profile write must evict every CollectionRegistry entry for the "
                    + "tenant, forcing the next registration attempt to re-verify against "
                    + "the database instead of trusting stale in-process state")
                .isFalse();
    }

    // ── lazy per-content-type cloud seam ────────────────────────────────────

    @Test
    void lazySeed_cloudTenantWithNoProfile_getsExactlyOneRow_onlyForThatContentType() {
        EmbedderRouter router = new EmbedderRouter("dummy-key", "document");
        assertThat(profileRows(CLOUD_TENANT)).as("no profile yet").isEmpty();

        router.seedEmbeddingProfileForContentType(tenantScope, CLOUD_TENANT, "code");

        var rows = profileRows(CLOUD_TENANT);
        assertThat(rows.keySet())
                .as("only the ONE content type registered — no other rows appear")
                .containsExactly("code");
        assertThat(rows.get("code").get("model")).isEqualTo("voyage-code-3");
        assertThat(rows.get("code").get("dimension")).isEqualTo(1024);
    }

    @Test
    void lazySeed_secondContentType_addsOnlyThatRow_leavesFirstUntouched() {
        String tenant = CLOUD_TENANT + "-second";
        EmbedderRouter router = new EmbedderRouter("dummy-key", "document");
        router.seedEmbeddingProfileForContentType(tenantScope, tenant, "code");
        router.seedEmbeddingProfileForContentType(tenantScope, tenant, "knowledge");

        var rows = profileRows(tenant);
        assertThat(rows.keySet()).containsExactlyInAnyOrder("code", "knowledge");
        assertThat(rows.get("code").get("model")).isEqualTo("voyage-code-3");
        assertThat(rows.get("knowledge").get("model")).isEqualTo("voyage-context-3");
    }

    @Test
    void unknownContentType_hasAProfileRow_inBothModes() {
        EmbedderRouter onnx = new EmbedderRouter(new FakeBge(), "document");
        String onnxTenant = LOCAL_TENANT + "-unknown";
        onnx.seedEmbeddingProfileForContentType(tenantScope, onnxTenant, "unknown");
        assertThat(profileRows(onnxTenant).get("unknown").get("model"))
                .isEqualTo("bge-base-en-v15-768");

        EmbedderRouter voyage = new EmbedderRouter("dummy-key", "document");
        String voyageTenant = VOYAGE_TENANT + "-unknown";
        voyage.seedEmbeddingProfileForContentType(tenantScope, voyageTenant, "unknown");
        assertThat(profileRows(voyageTenant).get("unknown").get("model"))
                .isEqualTo("voyage-context-3");
    }

    /**
     * bead nexus-ft04v.8 fix (coordinator-reported regression, 2026-09-07):
     * content types are free-form on the client — an unmapped one must fall
     * back to the SAME CCE bucket token {@code "unknown"} gets, in BOTH
     * modes, never throw. This test used to assert the opposite
     * ({@code IllegalArgumentException}); that behaviour, once the caller
     * (bead .8's {@code CatalogHandler}) moved the seed ahead of the
     * registration decision, turned a registration that used to succeed
     * into an HTTP 400 for any content type outside the fixed set (a test
     * fixture's {@code "prose"}, a {@code quarantine-<ct>} content type,
     * etc.) — never a hard refusal.
     */
    @Test
    void unmappedContentType_fallsBackToTheCCEBucketToken_inBothModes() {
        EmbedderRouter onnx = new EmbedderRouter(new FakeBge(), "document");
        String onnxTenant = LOCAL_TENANT + "-unmapped";
        onnx.seedEmbeddingProfileForContentType(tenantScope, onnxTenant, "not-a-real-content-type");
        assertThat(profileRows(onnxTenant).get("not-a-real-content-type").get("model"))
                .as("ONNX mode: unmapped content type falls back to bge-768, same as \"unknown\"")
                .isEqualTo("bge-base-en-v15-768");

        EmbedderRouter voyage = new EmbedderRouter("dummy-key", "document");
        String voyageTenant = VOYAGE_TENANT + "-unmapped";
        voyage.seedEmbeddingProfileForContentType(tenantScope, voyageTenant, "not-a-real-content-type");
        assertThat(profileRows(voyageTenant).get("not-a-real-content-type").get("model"))
                .as("Voyage mode: unmapped content type falls back to voyage-context-3, same as \"unknown\"")
                .isEqualTo("voyage-context-3");
    }
}
