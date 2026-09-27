// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
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
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-e8h5x — {@code nexus.gc_restore_rereferenced_bounded}, the bounded
 * restore sweep, through {@link PgVectorRepository#restoreRereferencedBounded}.
 * Mirrors {@link GcQuarantineOrphansBoundedTest} for the opposite direction
 * (catalog-037/nexus-a6mon's bounded quarantine sweep).
 *
 * <p>The unbounded {@code gc_restore_rereferenced} restores every re-referenced
 * row in one transaction; about 36,000 code__1-1 rows left quarantine that way
 * on 2026-09-16, the same night catalog-037's 41,032-row quarantine call ran
 * 58 s past the ~30 s edge deadline. This pins the same four properties
 * catalog-037 established for quarantine: the bound is honoured per call, the
 * caller can loop on {@code remaining} to zero, {@code created_at} survives
 * the round trip, one {@code gc_audit} row lands per batch, and a no-op call
 * writes none.
 *
 * <p>Unlike the quarantine fixture (whose victims have no destination row at
 * all before the move), a restore candidate's ORIGIN collection already
 * carries a STUB chunk row for every seeded chash — the manifest FK that
 * makes it "re-referenced" is scoped to (tenant, origin collection, chash),
 * so a real chunks row must already exist there for the FK to be satisfiable.
 * The bounded function's copy-INSERT then {@code ON CONFLICT DO UPDATE}s that
 * stub with the quarantine row's real content, exactly like a heal
 * rehydrating a reference before the vector itself came back (same shape as
 * {@code CatalogGcAuditProducersTest.restoreRereferenced_...}'s single-row
 * fixture, extended to n chashes with one doc per chash to avoid a
 * {@code (doc_id, position)} collision).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GcRestoreRereferencedBoundedTest {

    private static final String SVC_ROLE = "svc_e8h5x_test";
    private static final String SVC_PASS = "svc_e8h5x_test_pass";
    private static final String TENANT = "e8h5x-tenant";
    private static final OffsetDateTime PAST =
        OffsetDateTime.of(2026, 7, 16, 0, 8, 44, 0, ZoneOffset.UTC);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository repo;
    PgVectorRepository vecRepo;

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
        repo = new CatalogRepository(tenantScope);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void boundIsHonouredPerCall_andRemainingCountsDownToZero() throws Exception {
        var p = seed("bound", 5);

        var first = vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 2);
        assertThat(first.restored()).as("one call restores at most row_limit").isEqualTo(2);
        assertThat(first.remaining()).as("and says what is left, so the caller need not guess").isEqualTo(3);
        assertThat(chunkCount(p.quarantine())).as("committed: the quarantine really lost 2").isEqualTo(3);

        int calls = 1;
        var last = first;
        while (last.remaining() > 0) {
            last = vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 2);
            calls++;
            assertThat(calls).as("loop must terminate").isLessThan(10);
        }
        assertThat(calls).as("5 rows at a bound of 2 is exactly three batches").isEqualTo(3);
        assertThat(chunkCount(p.quarantine())).as("quarantine fully drained").isZero();
        for (String h : p.hashes()) {
            assertThat(chunkText(p.origin(), h))
                .as("every chash's origin row now carries the REAL content, not the stub")
                .isEqualTo(realText(h));
        }

        var idle = vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 2);
        assertThat(idle.restored()).as("the no-op path restores nothing").isZero();
        assertThat(idle.remaining()).as("an idempotent no-op once drained").isZero();
    }

    @Test
    void createdAtIsCarriedThrough_notRestampedToRestoreTime() throws Exception {
        var p = seed("ts", 3);
        backdate(p.quarantine(), PAST);
        // NON-VACUITY: the backdate must have landed, or "equals PAST" below
        // could only pass by coincidence of the clock.
        assertThat(createdAts(p.quarantine())).as("guard: quarantine rows are backdated").containsOnly(PAST);

        vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 10);

        assertThat(createdAts(p.origin()))
            .as("restored rows keep their ORIGINAL (quarantine) created_at. The unbounded "
                + "gc_restore_rereferenced omits created_at from its INSERT just like "
                + "gc_quarantine_orphans did before a6mon's fix, so the column default would "
                + "restamp every restored row to restore time and erase the generation "
                + "history a round trip through quarantine should preserve")
            .containsOnly(PAST);
    }

    @Test
    void nonPositiveBound_isRefused_notTreatedAsUnbounded() throws Exception {
        var p = seed("refuse", 2);
        assertThatThrownBy(() ->
                vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 0))
            .as("a zero bound must be an error, never a silent fallback to the unbounded transaction")
            .hasMessageContaining("p_row_limit must be positive");
        assertThat(chunkCount(p.quarantine())).as("and nothing restored").isEqualTo(2);
    }

    @Test
    void oneGcAuditRowPerBatch_withTheRightShape() throws Exception {
        var p = seed("audit", 5);
        assertThat(repo.listGcAudit(TENANT, p.quarantine(), "gc_restore_rereferenced_bounded", 100, 0))
            .as("guard: no prior audit noise for this quarantine collection").isEmpty();

        var first = vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 2);
        var afterFirst = repo.listGcAudit(TENANT, p.quarantine(), "gc_restore_rereferenced_bounded", 100, 0);
        assertThat(afterFirst).as("exactly one audit row for this one batch").hasSize(1);
        Map<String, Object> row = afterFirst.get(0);
        assertThat(row.get("actor")).as("server-driven producer, not a client-attributed one").isEqualTo("engine");
        assertThat(row.get("dry_run")).isEqualTo(false);
        assertThat(((Number) row.get("chash_count")).longValue())
            .as("exactly what this batch restored").isEqualTo(first.restored());
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) row.get("details");
        assertThat(details.get("origin_collection")).isEqualTo(p.origin());
        assertThat(((Number) details.get("row_limit")).intValue()).isEqualTo(2);
        assertThat(((Number) details.get("remaining_after")).longValue()).isEqualTo(first.remaining());

        int calls = 1;
        var last = first;
        while (last.remaining() > 0) {
            last = vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 2);
            calls++;
            assertThat(calls).isLessThan(10);
        }
        assertThat(repo.listGcAudit(TENANT, p.quarantine(), "gc_restore_rereferenced_bounded", 100, 0))
            .as("one gc_audit row per COMMITTED batch, none merged or dropped")
            .hasSize(calls);
    }

    @Test
    void noOpCall_writesNoAuditRow() throws Exception {
        // A quarantine collection with nothing eligible to restore -- the
        // IF v_chashes IS NULL THEN RETURN branch fires before ever reaching
        // the audit INSERT, exactly like the unbounded twin's own no-op path.
        var p = seed("noop", 0);
        var outcome = vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 10);
        assertThat(outcome.restored()).isZero();
        assertThat(outcome.remaining()).isZero();
        assertThat(repo.listGcAudit(TENANT, p.quarantine(), "gc_restore_rereferenced_bounded", 100, 0))
            .as("a genuinely no-op call leaves no forensic trail, same as gc_quarantine_orphans_bounded's")
            .isEmpty();
    }

    // ── fixtures ──────────────────────────────────────────────────────────────

    private record Pair(String origin, String quarantine, List<String> hashes) {}

    private static String realText(String chashHex) {
        return "real-" + chashHex;
    }

    private static String stubText(String chashHex) {
        return "stub-" + chashHex;
    }

    /**
     * {@code n} re-referenced chashes: real content in the QUARANTINE
     * collection, a stub row already in the ORIGIN collection (the manifest
     * FK's target), and one document + one manifest row per chash pointing
     * at the origin collection (one doc per chash avoids a
     * {@code (doc_id, position)} collision from reusing position 0).
     */
    private Pair seed(String slug, int n) throws Exception {
        String origin = "code__e8h5x-" + slug + "__minilm-l6-v2-384__v1";
        String quarantine = "quarantine-code__e8h5x-" + slug + "__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, origin);
            PgContainerHelper.insertCollection(dsl, TENANT, quarantine);
        }
        List<String> hashes = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            String chash = Chash.ofText(slug + "-restore-" + i).toHex();
            hashes.add(chash);
            vecRepo.upsertChunks(TENANT, quarantine, List.of(chash),
                List.of(realText(chash)), List.of(Map.of()));
            vecRepo.upsertChunks(TENANT, origin, List.of(chash),
                List.of(stubText(chash)), List.of(Map.of()));
            String docId = "e8h5x-" + slug + "-doc-" + i;
            repo.upsertDocument(TENANT, Map.of(
                "tumbler", docId, "title", "e8h5x-" + slug + "-" + i,
                "content_type", "code", "corpus", "code",
                "physical_collection", origin, "chunk_count", 0));
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                insertManifestRow(su, TENANT, docId, chash, origin);
            }
        }
        return new Pair(origin, quarantine, hashes);
    }

    private void insertManifestRow(Connection su, String tenant, String docId, String chashHex, String collection) {
        DSL.using(su, SQLDialect.POSTGRES)
           .insertInto(CATALOG_DOCUMENT_CHUNKS,
                CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION)
           .values(tenant, docId, 0, Chash.fromHex(chashHex).toBytes(), collection)
           .execute();
    }

    private int chunkCount(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(
            DSL.table(DSL.name("nexus", "chunks")),
            DSL.field("collection", String.class).eq(collection)));
    }

    private String chunkText(String collection, String chashHex) {
        return tenantScope.withTenant(TENANT, ctx -> ctx
            .select(DSL.field("chunk_text", String.class))
            .from(DSL.table(DSL.name("nexus", "chunks")))
            .where(DSL.field("collection", String.class).eq(collection))
            .and(DSL.field("chash", byte[].class).eq(Chash.fromHex(chashHex).toBytes()))
            .fetchOne(0, String.class));
    }

    private List<OffsetDateTime> createdAts(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx
            .select(DSL.field("created_at", OffsetDateTime.class))
            .from(DSL.table(DSL.name("nexus", "chunks")))
            .where(DSL.field("collection", String.class).eq(collection))
            .fetch(0, OffsetDateTime.class))
            .stream().map(t -> t.withOffsetSameInstant(ZoneOffset.UTC)).toList();
    }

    private void backdate(String collection, OffsetDateTime to) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES)
               .update(DSL.table(DSL.name("nexus", "chunks")))
               .set(DSL.field("created_at", OffsetDateTime.class), to)
               .where(DSL.field("collection", String.class).eq(collection))
               .execute();
        }
    }
}
