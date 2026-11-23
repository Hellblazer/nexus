// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P2.9 (nexus-p2m1x.14): the stale-note repair proves a note collection by the collection's registry row, never
 * by its name. Split from {@link NoteUriRepairTest}, which keeps the census, the rewrite, the ledger and the HTTP route;
 * this class holds the content-type half: file-routed labels, unproven types, a missing registry row, the stale registry
 * cache, a retype between census and lock, and a retype that must wait on the repair's share lock.
 *
 * <p>Each test plants its own tenant, because the census counts the whole tenant.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NoteUriRepairContentTypeTest {

    private static final String SVC_ROLE = "svc_note_uri_repair_ct_test";
    private static final String SVC_PASS = "svc_note_uri_repair_ct_test_pass";
    private static final AtomicInteger SEQ = new AtomicInteger();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository repo;

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
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @AfterEach
    void clearHook() {
        CatalogRepository.setNoteUriRepairHookForTests(null);
    }

    /** One tenant with Y (where notes live, knowledge) and X1 (a collection notes used to live in). */
    final class Fx {
        final String tenant;
        final String y;
        final String x1;

        Fx(String tag) throws Exception {
            int n = SEQ.incrementAndGet();
            tenant = "repair-ct-" + tag + "-" + n;
            y = coll("y" + n);
            x1 = coll("x1-" + n);
            try (Connection su = pg.createConnection("")) {
                var dsl = DSL.using(su, SQLDialect.POSTGRES);
                PgContainerHelper.seedServiceToken(dsl, "note-uri-repair-ct-token-" + tag + "-" + n, tenant, "repair-test");
                for (String c : List.of(y, x1)) PgContainerHelper.insertCollection(dsl, tenant, c);
            }
        }

        String nx(String coll, String title) {
            return "nx-note://" + coll + "/" + title;
        }
    }

    // ── RDR-229 P2.9: a live collection's routing reads its row, never its name ──

    private static final List<String> REFUSED_KEYS = List.of("refused_file_routed_collection", "refused_malformed",
        "refused_unregistered_collection", "refused_unproven_content_type");

    /** Registers {@code name} (any shape) under {@code tenant} with an explicit content type. */
    private void register(String tenant, String name, String contentType) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, tenant, name);
            dsl.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, contentType)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(name)))
                .execute();
        }
    }

    private void setContentType(String tenant, String name, String contentType) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, contentType)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(name)))
                .execute();
        }
    }

    /** A collection with a prefix-free, minted-style label ({@code c<digits>}) and an explicit content type. */
    private String registerLabel(String tenant, String contentType) throws Exception {
        String label = "c" + (700000 + SEQ.incrementAndGet());
        register(tenant, label, contentType);
        return label;
    }

    private void assertOnlyRefusedKey(Map<String, Object> r, String key, int n) {
        for (String k : REFUSED_KEYS) assertThat(r.get(k)).as(k).isEqualTo(k.equals(key) ? n : 0);
        assertThat(r.get("refused")).isEqualTo(n);
    }

    @Test
    void census_aPrefixFreeLabelWithAFileRoutedContentTypeIsRefused_asFileRoutedCollection() throws Exception {
        for (String contentType : List.of("docs", "rdr", "code")) {
            Fx f = new Fx("label-" + contentType);
            String label = registerLabel(f.tenant, contentType);
            seedNote(f.tenant, "n1", label, f.nx(f.x1, "odd"));

            Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, false, 1000, 5);

            assertOnlyRefusedKey(r, "refused_file_routed_collection", 1);
            assertThat(r.get("rewrite")).as(contentType).isEqualTo(0);
            assertThat(byTumbler(sampleOf(r).get("refused"), "n1").get("reason"))
                .as(contentType).isEqualTo("file_routed_collection");
        }
    }

    @Test
    void apply_aPrefixFreeFileRoutedLabelLeavesTheUriUntouched() throws Exception {
        for (String contentType : List.of("docs", "rdr", "code")) {
            Fx f = new Fx("apply-" + contentType);
            String label = registerLabel(f.tenant, contentType);
            seedNote(f.tenant, "n1", label, f.nx(f.x1, "odd"));
            seedNote(f.tenant, "n2", label, f.nx(f.x1, "odd2"));

            Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 5);

            assertOnlyRefusedKey(r, "refused_file_routed_collection", 2);
            assertThat(r.get("rewritten")).as(contentType).isEqualTo(0);
            assertThat(docUri(f.tenant, "n1")).as(contentType).isEqualTo(f.nx(f.x1, "odd"));
            assertThat(docUri(f.tenant, "n2")).as(contentType).isEqualTo(f.nx(f.x1, "odd2"));
        }
    }

    @Test
    void census_aPrefixFreeLabelWithKnowledgeContentTypeIsARewriteCandidate() throws Exception {
        Fx f = new Fx("label-knowledge");
        String label = registerLabel(f.tenant, "knowledge");
        seedNote(f.tenant, "n1", label, f.nx(f.x1, "alpha"));
        seedNote(f.tenant, "n2", label, f.nx(f.x1, "beta"));

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, false, 1000, 5);

        assertThat(r.get("rewrite")).isEqualTo(2);
        assertThat(r.get("refused")).isEqualTo(0);
        assertThat(byTumbler(sampleOf(r).get("rewrite"), "n1").get("new_uri")).isEqualTo(f.nx(label, "alpha"));
    }

    @Test
    void census_aPrefixedNameWithAKnowledgeRowIsACandidate_theRowDecidesNotTheName() throws Exception {
        Fx f = new Fx("name-lies");
        String name = "docs__lies-" + SEQ.incrementAndGet() + "__minilm-l6-v2-384__v1";
        register(f.tenant, name, "knowledge");
        seedNote(f.tenant, "n1", name, f.nx(f.x1, "alpha"));

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, false, 1000, 5);

        assertThat(r.get("rewrite")).isEqualTo(1);
        assertThat(r.get("refused")).isEqualTo(0);
    }

    @Test
    void census_aRowTypedAnythingButKnowledgeOrFileRoutedIsRefused_asUnprovenContentType() throws Exception {
        // 'unknown' is what the hygiene walks leave on a row they could not type; a quarantine type is the other shape.
        for (String contentType : List.of("unknown", "quarantine-docs")) {
            Fx f = new Fx("unproven-" + contentType);
            String label = registerLabel(f.tenant, contentType);
            String prefixed = "docs__a-" + SEQ.incrementAndGet() + "__minilm-l6-v2-384__v1";
            register(f.tenant, prefixed, contentType);
            seedNote(f.tenant, "n1", label, f.nx(f.x1, "alpha"));
            seedNote(f.tenant, "n2", prefixed, f.nx(f.x1, "beta"));

            Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 5);

            assertOnlyRefusedKey(r, "refused_unproven_content_type", 2);
            assertThat(r.get("rewrite")).as(contentType).isEqualTo(0);
            assertThat(r.get("rewritten")).as(contentType).isEqualTo(0);
            assertThat(byTumbler(sampleOf(r).get("refused"), "n1").get("reason")).isEqualTo("unproven_content_type");
            assertThat(byTumbler(sampleOf(r).get("refused"), "n2").get("reason")).isEqualTo("unproven_content_type");
            assertThat(docUri(f.tenant, "n1")).isEqualTo(f.nx(f.x1, "alpha"));
            assertThat(docUri(f.tenant, "n2")).isEqualTo(f.nx(f.x1, "beta"));
        }
    }

    @Test
    void census_aRowTheCacheStillCallsKnowledgeButTheTableCallsDocsIsRefused() throws Exception {
        Fx f = new Fx("stale-cache");
        String label = registerLabel(f.tenant, "knowledge");
        // The registry caches the row it reads, and the direct UPDATE below (not upsertCollection, which evicts) leaves it.
        assertThat(tenantScope.withTenant(f.tenant, ctx -> CollectionRegistry.require(ctx, f.tenant, label)).contentType())
            .isEqualTo("knowledge");
        setContentType(f.tenant, label, "docs");
        assertThat(CollectionRegistry.cached(f.tenant, label)).as("the cache is still warm").isPresent();
        seedNote(f.tenant, "n1", label, f.nx(f.x1, "alpha"));

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 5);

        assertOnlyRefusedKey(r, "refused_file_routed_collection", 1);
        assertThat(r.get("rewrite")).isEqualTo(0);
        assertThat(docUri(f.tenant, "n1")).isEqualTo(f.nx(f.x1, "alpha"));
    }

    /**
     * The repair takes the registry row FOR SHARE before its "locked" stage (RDR-229 P3.5a, registry first), so a flip to docs between the census and the
     * lock can no longer land: it WAITS behind the repair. The hook's flip runs on a second connection with a lock_timeout, so a regression (the
     * flip going through, or the repair not holding the row) fails the assertions instead of hanging the repair's own thread on its own lock.
     */
    @Test
    void apply_aFlipToDocsAtTheLockedStageWaitsBehindTheRepair_soTheRowIsRewrittenAsKnowledge() throws Exception {
        Fx f = new Fx("flip");
        String label = registerLabel(f.tenant, "knowledge");
        seedNote(f.tenant, "n1", label, f.nx(f.x1, "alpha"));
        java.util.concurrent.atomic.AtomicReference<Throwable> flip = new java.util.concurrent.atomic.AtomicReference<>();
        java.util.concurrent.atomic.AtomicBoolean ran = new java.util.concurrent.atomic.AtomicBoolean();
        CatalogRepository.setNoteUriRepairHookForTests(stage -> {
            if (!stage.equals("locked")) return;
            ran.set(true);
            try (Connection other = pg.createConnection("")) {
                other.setAutoCommit(true);
                var dsl = DSL.using(other, SQLDialect.POSTGRES);
                dsl.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val("400ms"), DSL.val(false))).fetch();
                try {
                    dsl.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, "docs")
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(f.tenant).and(CATALOG_COLLECTIONS.NAME.eq(label))).execute();
                } catch (Throwable t) {
                    flip.set(t);
                }
            } catch (java.sql.SQLException e) {
                throw new IllegalStateException(e);
            }
        });

        Map<String, Object> r;
        try {
            r = repo.repairStaleNoteUris(f.tenant, true, 1000, 5);
        } finally {
            CatalogRepository.setNoteUriRepairHookForTests(null);
        }

        assertThat(ran.get()).as("the locked stage ran").isTrue();
        assertThat(flip.get()).as("the flip waited on the repair's share lock and timed out").isNotNull().hasStackTraceContaining("lock timeout");
        assertThat(r.get("rewritten")).as("the row was still a knowledge collection when the repair wrote it").isEqualTo(1);
        assertThat(docUri(f.tenant, "n1")).isEqualTo(f.nx(label, "alpha"));
    }

    @Test
    void apply_aRetypeOfTheCollectionBlocksUntilTheRepairCommits_andTheRowIsRewrittenOnceAsKnowledge() throws Exception {
        Fx f = new Fx("share-lock");
        String label = registerLabel(f.tenant, "knowledge");
        seedNote(f.tenant, "n1", label, f.nx(f.x1, "alpha"));
        java.util.concurrent.atomic.AtomicReference<Throwable> retypeInsideTheRepair = new java.util.concurrent.atomic.AtomicReference<>();
        CatalogRepository.setNoteUriRepairHookForTests(stage -> {
            if (!stage.equals("checked")) return;      // past the lock, the re-read and the holder check: only the UPDATE is left
            try (Connection other = pg.createConnection("")) {
                other.setAutoCommit(true);
                var dsl = DSL.using(other, SQLDialect.POSTGRES);
                dsl.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val("400ms"), DSL.val(false)))
                    .fetch();
                try {
                    dsl.update(CATALOG_COLLECTIONS)
                        .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "docs")
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(f.tenant).and(CATALOG_COLLECTIONS.NAME.eq(label)))
                        .execute();
                } catch (Throwable t) {
                    retypeInsideTheRepair.set(t);
                }
            } catch (java.sql.SQLException e) {
                throw new IllegalStateException(e);
            }
        });

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 5);

        assertThat(retypeInsideTheRepair.get()).as("the retype waited on the repair's share lock and timed out")
            .isNotNull().hasStackTraceContaining("lock timeout");
        assertThat(r.get("rewritten")).isEqualTo(1);
        assertThat(docUri(f.tenant, "n1")).isEqualTo(f.nx(label, "alpha"));
        CatalogRepository.setNoteUriRepairHookForTests(null);
        setContentType(f.tenant, label, "docs");      // the retype lands once the repair has committed
        Map<String, Object> again = repo.repairStaleNoteUris(f.tenant, true, 1000, 5);
        assertThat(again.get("rewritten")).as("rewritten once, never again").isEqualTo(0);
        assertThat(docUri(f.tenant, "n1")).isEqualTo(f.nx(label, "alpha"));
    }

    @Test
    void census_moreCollectionsThanOneContentTypeQueryHolds_allBecomeCandidates() throws Exception {
        Fx f = new Fx("many");
        int n = 501;
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (int i = 0; i < n; i++) PgContainerHelper.insertCollection(dsl, f.tenant, coll("many" + i + "-" + f.tenant));
        }
        for (int i = 0; i < n; i++) seedNote(f.tenant, "m" + i, coll("many" + i + "-" + f.tenant), f.nx(f.x1, "t" + i));

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, false, 1000, 5);

        assertThat(r.get("rewrite")).isEqualTo(n);
        assertThat(r.get("refused")).isEqualTo(0);
    }

    @Test
    void census_aLiveRowWhoseCollectionHasNoRegistryRow_isRefusedNotRewritten_andDoesNotFailTheCensus() throws Exception {
        Fx f = new Fx("unregistered");
        String gone = "c999" + SEQ.incrementAndGet();
        seedNote(f.tenant, "ok", f.y, f.nx(f.x1, "fine"));
        registerCollection(f.tenant, gone);
        seedNote(f.tenant, "un", gone, f.nx(f.x1, "orphan"));
        seedNote(f.tenant, "un2", gone, f.nx(f.x1, "orphan2"));
        dropRegistryRow(f.tenant, gone);

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, true, 1000, 5);

        assertThat(r.get("rewrite")).isEqualTo(1);
        assertOnlyRefusedKey(r, "refused_unregistered_collection", 2);
        assertThat(byTumbler(sampleOf(r).get("refused"), "un").get("reason")).isEqualTo("unregistered_collection");
        assertThat(docUri(f.tenant, "un")).as("never rewritten").isEqualTo(f.nx(f.x1, "orphan"));
        assertThat(docUri(f.tenant, "un2")).isEqualTo(f.nx(f.x1, "orphan2"));
        assertThat(docUri(f.tenant, "ok")).isEqualTo(f.nx(f.y, "fine"));
    }

    @Test
    void census_aDocsNamedCollectionWithNoRegistryRowIsRefused_asUnregistered_notByItsName() throws Exception {
        Fx f = new Fx("docs-unregistered");
        String ghost = "docs__ghost-" + SEQ.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(f.tenant, ghost);
        seedNote(f.tenant, "g1", ghost, f.nx(f.x1, "alpha"));
        dropRegistryRow(f.tenant, ghost);

        Map<String, Object> r = repo.repairStaleNoteUris(f.tenant, false, 1000, 5);

        assertOnlyRefusedKey(r, "refused_unregistered_collection", 1);
        assertThat(byTumbler(sampleOf(r).get("refused"), "g1").get("reason")).isEqualTo("unregistered_collection");
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private static Map<String, List<Map<String, Object>>> sampleOf(Map<String, Object> r) {
        return (Map<String, List<Map<String, Object>>>) r.get("sample");
    }

    private static Map<String, Object> byTumbler(List<Map<String, Object>> rows, String tumbler) {
        return rows.stream().filter(m -> tumbler.equals(m.get("tumbler"))).findFirst().orElseThrow();
    }

    private static String coll(String slug) {
        return "knowledge__" + slug + "__minilm-l6-v2-384__v1";
    }

    /**
     * Registers {@code name}: since P3.1 a document under an unregistered collection is refused (typed 422), so a
     * row whose collection has no registry row is seeded by registering first and then {@link #dropRegistryRow}.
     */
    private void registerCollection(String tenant, String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, name);
        }
    }

    /** The legacy shape the census must refuse: a live row naming a collection whose registry row is gone. */
    private void dropRegistryRow(String tenant, String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
        }
    }

    private void seedNote(String tenant, String tumbler, String collection, String uri) {
        Map<String, Object> d = new HashMap<>(Map.of(
            "tumbler", tumbler, "title", tumbler, "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", collection));
        d.put("source_uri", uri);
        repo.upsertDocument(tenant, d);
    }

    private String docUri(String tenant, String tumbler) {
        return tenantScope.withTenant(tenant, ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(0, String.class));
    }
}
