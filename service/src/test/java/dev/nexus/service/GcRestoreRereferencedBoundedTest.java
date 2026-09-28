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
 * so a real chunks row must already exist there for the FK to be satisfiable
 * (same shape as {@code CatalogGcAuditProducersTest.restoreRereferenced_...}'s
 * single-row fixture, extended to n chashes with one doc per chash to avoid a
 * {@code (doc_id, position)} collision).
 *
 * <p>nexus-brxnp / nexus-u6d93 (T2 {@code nexus/debug-u6d93-brxnp}): the
 * bounded function's copy-INSERT used to {@code ON CONFLICT DO UPDATE} that
 * pre-existing origin row with the quarantine row's content — correct only
 * when the origin row is a genuine placeholder, but WRONG whenever the
 * origin row is itself the product of a later, fresher re-index that landed
 * before the end-of-walk restore leg ran: the older quarantine copy then
 * clobbered the newer live metadata (and, from catalog-042, its
 * {@code created_at} too). The fix makes the ORIGIN row authoritative
 * whenever one already exists ({@code ON CONFLICT DO NOTHING}); the
 * quarantine copy is still deleted either way, so it never lingers. See
 * {@link #boundIsHonouredPerCall_andRemainingCountsDownToZero} (the
 * pre-existing stub survives untouched) and the dedicated
 * {@code liveOriginRow_*} test below for the load-bearing proof.
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
                .as("nexus-brxnp: the origin row already existed, so it is NEVER overwritten by "
                    + "the older quarantine copy (ON CONFLICT DO NOTHING) -- restore only removes "
                    + "the now-redundant quarantine copy")
                .isEqualTo(stubText(h));
        }

        var idle = vecRepo.restoreRereferencedBounded(TENANT, p.quarantine(), p.origin(), 2);
        assertThat(idle.restored()).as("the no-op path restores nothing").isZero();
        assertThat(idle.remaining()).as("an idempotent no-op once drained").isZero();
    }

    /**
     * nexus-brxnp regression guard: when the origin genuinely has NO row for
     * the re-referenced chash — the ordinary heal case, where D's manifest
     * write races AHEAD of the physical restore, exactly the production race
     * {@code fk_catalog_chunks_chunk} now structurally blocks unless bypassed
     * (mirrors {@code PgVectorRepositoryGcQuarantineTest
     * #restoreRereferenced_movesBackWhenManifestReReferencesIt}) — restore
     * must still INSERT the quarantine copy's own content and
     * {@code created_at}, and strip the quarantine stamps. This is the
     * insert-path property catalog-042 fixed and the new
     * {@code ON CONFLICT DO NOTHING} changeset must not regress: DO NOTHING
     * only changes behaviour when a conflicting row ALREADY exists.
     */
    @Test
    void createdAtIsCarriedThrough_onInsertWhenOriginIsAbsent_regressionGuard() throws Exception {
        String origin = "code__e8h5x-absent__minilm-l6-v2-384__v1";
        String quarantine = "quarantine-code__e8h5x-absent__minilm-l6-v2-384__v1";
        String chash = Chash.ofText("absent-restore").toHex();
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, origin);
            PgContainerHelper.insertCollection(dsl, TENANT, quarantine);
        }
        String docId = "e8h5x-absent-doc";
        vecRepo.upsertChunks(TENANT, origin, List.of(chash), List.of("orphan-text"), List.of(Map.of()));
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", docId, "title", "e8h5x-absent", "content_type", "code",
            "corpus", "code", "physical_collection", origin, "chunk_count", 1));
        repo.writeManifest(TENANT, docId, origin, List.of(
            Map.of("position", 0, "chash", chash, "chunk_index", 0)));

        // Orphan it, then quarantine it FOR REAL, so the quarantine copy
        // carries the quarantined_at/origin_collection stamps restore must strip.
        repo.writeManifest(TENANT, docId, origin, List.of());
        var quarantined = vecRepo.quarantineOrphansBounded(TENANT, origin, quarantine, "2026-08-01T00:00:00Z", 20, 10);
        assertThat(quarantined.moved()).as("guard: X actually left O for Q").isEqualTo(1L);
        backdate(quarantine, PAST);
        // NON-VACUITY: the backdate must have landed, or "equals PAST" below
        // could only pass by coincidence of the clock.
        assertThat(createdAts(quarantine)).as("guard: quarantine row is backdated").containsOnly(PAST);

        // D re-references X while X is STILL only in Q -- bypassing the FK
        // exactly like PgVectorRepositoryGcQuarantineTest's own
        // seedManifestBypassingFk, since a real write cannot do this without
        // the chunk already existing in O.
        insertManifestRowBypassingFk(docId, chash, origin);

        var outcome = vecRepo.restoreRereferencedBounded(TENANT, quarantine, origin, 10);
        assertThat(outcome.restored()).as("the restore inserts it").isEqualTo(1L);
        assertThat(chunkText(origin, chash))
            .as("origin gets the quarantine copy's content -- there was nothing to conflict with")
            .isEqualTo("orphan-text");
        assertThat(createdAts(origin))
            .as("created_at is carried through on a genuine INSERT, the absent-origin path")
            .containsOnly(PAST);
        assertThat(metadataField(origin, chash, "quarantined_at")).as("quarantine stamp stripped").isNull();
        assertThat(metadataField(origin, chash, "origin_collection")).as("quarantine stamp stripped").isNull();
        assertThat(chunkCount(quarantine)).as("Q no longer holds X").isZero();

        var rows = repo.listGcAudit(TENANT, quarantine, "gc_restore_rereferenced_bounded", 100, 0);
        assertThat(rows).hasSize(1);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) rows.get(0).get("details");
        assertThat(((Number) details.get("already_live")).longValue())
            .as("nexus-brxnp: a genuine INSERT (origin absent) hits no conflict at all")
            .isZero();
    }

    /**
     * nexus-brxnp / nexus-u6d93 load-bearing proof: a chash re-referenced by
     * a FRESH combined-write (new metadata, e.g. {@code indexed_at}/
     * {@code content_hash}) lands in the ORIGIN collection BEFORE the
     * end-of-walk restore leg runs. The quarantine collection still holds an
     * OLDER copy of that same chash from an earlier orphan sweep. Before the
     * fix, {@code ON CONFLICT DO UPDATE} let the stale quarantine copy
     * clobber the fresh origin row's metadata and {@code created_at}. The
     * fix ({@code ON CONFLICT DO NOTHING}) makes the live origin row win; the
     * quarantine copy is still deleted so it does not linger.
     */
    @Test
    void liveOriginRow_metadataAndCreatedAt_areNeverOverwrittenByAnOlderQuarantineCopy() throws Exception {
        String origin = "code__e8h5x-live-win__minilm-l6-v2-384__v1";
        String quarantine = "quarantine-code__e8h5x-live-win__minilm-l6-v2-384__v1";
        String docId = "e8h5x-live-win-doc";
        String chash = Chash.ofText("live-win-chash").toHex();
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, origin);
            PgContainerHelper.insertCollection(dsl, TENANT, quarantine);
        }

        // 1. X lands in O with T1/H1 metadata, manifest-referenced by D.
        vecRepo.upsertChunks(TENANT, origin, List.of(chash), List.of("text-t1"),
            List.of(Map.of("indexed_at", "T1", "content_hash", "H1")));
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", docId, "title", "e8h5x-live-win", "content_type", "code",
            "corpus", "code", "physical_collection", origin, "chunk_count", 1));
        repo.writeManifest(TENANT, docId, origin, List.of(
            Map.of("position", 0, "chash", chash, "chunk_index", 0)));

        // 2. D's reference to X is dropped -- X becomes an orphan -- quarantined.
        repo.writeManifest(TENANT, docId, origin, List.of());
        var quarantined = vecRepo.quarantineOrphansBounded(TENANT, origin, quarantine, "2026-08-01T00:00:00Z", 20, 10);
        assertThat(quarantined.moved()).as("guard: X actually left O for Q").isEqualTo(1L);
        backdate(quarantine, PAST);
        assertThat(createdAts(quarantine)).as("guard: quarantine copy is backdated").containsOnly(PAST);

        // 3. A later re-index re-inserts X live into O with fresh T2/H2
        // metadata, and re-references it from D -- exactly what a combined
        // write does before the walk's end-of-walk restore leg runs.
        vecRepo.upsertChunks(TENANT, origin, List.of(chash), List.of("text-t2"),
            List.of(Map.of("indexed_at", "T2", "content_hash", "H2")));
        OffsetDateTime freshCreatedAt = createdAts(origin).get(0);
        assertThat(freshCreatedAt).as("guard: the fresh row is NOT the backdated quarantine copy")
            .isNotEqualTo(PAST);
        repo.writeManifest(TENANT, docId, origin, List.of(
            Map.of("position", 0, "chash", chash, "chunk_index", 0)));

        // 4. The end-of-walk restore leg runs and finds X re-referenced.
        var outcome = vecRepo.restoreRereferencedBounded(TENANT, quarantine, origin, 10);
        assertThat(outcome.restored()).as("the restore still processes and reports X").isEqualTo(1L);
        assertThat(outcome.remaining()).isZero();

        assertThat(chunkCount(quarantine)).as("Q no longer holds X").isZero();
        assertThat(chunkText(origin, chash)).as("O keeps its OWN, live content").isEqualTo("text-t2");
        assertThat(metadataField(origin, chash, "indexed_at")).isEqualTo("T2");
        assertThat(metadataField(origin, chash, "content_hash")).isEqualTo("H2");
        assertThat(createdAts(origin))
            .as("O's created_at is untouched by the restore")
            .containsExactly(freshCreatedAt);

        var rows = repo.listGcAudit(TENANT, quarantine, "gc_restore_rereferenced_bounded", 100, 0);
        assertThat(rows).hasSize(1);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) rows.get(0).get("details");
        assertThat(((Number) details.get("already_live")).longValue())
            .as("nexus-brxnp: the one candidate hit the DO NOTHING conflict -- the origin was already live")
            .isEqualTo(1L);
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

    /**
     * nexus-brxnp: inserts a manifest row naming {@code collection} for a
     * chash whose physical chunk currently sits ONLY in the quarantine
     * collection — the real production race {@code fk_catalog_chunks_chunk}
     * now structurally blocks (RDR-191 Phase 5, nexus-o8dil.29). Bypasses the
     * FK locally, the same drop/insert/re-add-NOT-VALID idiom
     * {@code PgVectorRepositoryGcQuarantineTest#seedManifestBypassingFk} uses,
     * so the SQL restore function's own insert-path handling of an
     * already-referenced-but-not-yet-physically-restored row stays covered.
     */
    private void insertManifestRowBypassingFk(String docId, String chashHex, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
            insertManifestRow(su, TENANT, docId, chashHex, collection);
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }
    }

    private String metadataField(String collection, String chashHex, String key) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT metadata->>'" + key + "' FROM nexus.chunks WHERE collection = '" + collection
                + "' AND chash = decode('" + chashHex + "', 'hex')");
            return rs.next() ? rs.getString(1) : null;
        }
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
