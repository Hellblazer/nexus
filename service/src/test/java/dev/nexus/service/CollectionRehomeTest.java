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
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-wsx4l — the collection re-home, and the shared-chash hazard that ruled out
 * the batched design it started as.
 *
 * <p>GROUP 1 is a HAZARD PIN, not a test of our own code. It proves against a real
 * Postgres that batching a re-home by DOCUMENT is necessary but NOT sufficient, which
 * is why no batched version survives here. {@code nexus.catalog_document_chunks} has
 * PRIMARY KEY (tenant_id, doc_id, position) and nothing constrains {@code chash}, so
 * ONE {@code nexus.chunks} row may be referenced by manifest rows of SEVERAL documents
 * — the documented dedup behaviour ("identical chunk text in the same collection
 * collapses to one T3 row by design", AGENTS.md § Catalog/T3 split). {@code
 * fk_catalog_chunks_chunk} is ON UPDATE CASCADE on (tenant_id, collection, chash), so
 * moving document A's chunks rewrites the manifest rows of any document B sharing one
 * of A's chashes while B's own {@code physical_collection} stays behind: a torn
 * document.
 *
 * <p>WHY THE BATCHED DESIGN DIED ANYWAY, recorded so nobody rebuilds it from this
 * file: shared-chash components on the real source turned out to be 13, the largest
 * 919 of 2,142 documents (73.2% of its manifest rows), because the components are
 * welded by DEGENERATE CHUNKS — a bare docstring delimiter shared by 463 documents, a
 * single close brace by 192 (see nexus-x50jb). A bound would have been exceeded on
 * essentially every call. Then a PITR fork measured the whole move at 169 s against a
 * ~30 s edge deadline, which killed bounding and atomicity as the axis entirely: the
 * caller never sees the response either way, so the op is ONE transaction plus a
 * pollable status read. GROUP 1 survives because it is why a future batched refinement
 * would need closure; it is not dead weight.
 *
 * <p>GROUP 2 tests the op as built: whole collection, one transaction, merge-and-report
 * on collisions.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CollectionRehomeTest {

    private static final String SVC_ROLE = "svc_rehome_batch_test";
    private static final String SVC_PASS = "svc_rehome_batch_test_pass";
    private static final String TENANT = "rehome-batch-tenant";

    private static final String SRC = "knowledge__rehome-src__minilm-l6-v2-384__v1";
    private static final String DST = "knowledge__rehome-dst__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
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
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(tenantScope);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, SRC);
            PgContainerHelper.insertCollection(dsl, TENANT, DST);
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 1 — the shared-chash hazard (a pin on Postgres behaviour, not on ours)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Two documents share one chash. Move only document A's chunks and only A's
     * {@code physical_collection}, exactly as a document-batch that ignores chash
     * sharing would, and document B tears: its manifest follows the cascade to the
     * target while its own row still names the source.
     */
    @Test
    @Order(10)
    void naiveDocumentBatch_tearsAPeerSharingAChash() throws Exception {
        String docA = "rehome-hazard-a";
        String docB = "rehome-hazard-b";
        String shared = Chash.ofText("rehome-hazard-shared-text").toHex();
        String onlyA = Chash.ofText("rehome-hazard-only-a").toHex();
        String onlyB = Chash.ofText("rehome-hazard-only-b").toHex();

        seedDocument(docA, SRC);
        seedDocument(docB, SRC);
        vecRepo.upsertChunks(TENANT, SRC,
            List.of(shared, onlyA, onlyB),
            List.of("shared text", "only a text", "only b text"),
            List.of(Map.of(), Map.of(), Map.of()));
        // The shared chash sits at position 0 of BOTH documents.
        catalogRepo.writeManifest(TENANT, docA, SRC, List.of(
            Map.of("position", 0, "chash", shared),
            Map.of("position", 1, "chash", onlyA)));
        catalogRepo.writeManifest(TENANT, docB, SRC, List.of(
            Map.of("position", 0, "chash", shared),
            Map.of("position", 1, "chash", onlyB)));

        assertThat(manifestCollections(docB))
            .as("precondition: document B's manifest starts wholly in the source")
            .containsOnly(SRC);

        // THE NAIVE MOVE: document A only. Its two chashes, and its own
        // catalog_documents row. This is the implementation we are refusing to
        // write, stated plainly so the hazard is attributable to the database's
        // behaviour and not to a bug in our op. Superuser connection so it bypasses
        // the repository entirely -- nothing here should read as the op's own code.
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.update(CHUNKS)
               .set(CHUNKS.COLLECTION, DST)
               .where(CHUNKS.TENANT_ID.eq(TENANT)
                   .and(CHUNKS.COLLECTION.eq(SRC))
                   .and(CHUNKS.CHASH.in(
                       Chash.fromHex(shared).toBytes(), Chash.fromHex(onlyA).toBytes())))
               .execute();
            dsl.update(CATALOG_DOCUMENTS)
               .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, DST)
               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)
                   .and(CATALOG_DOCUMENTS.TUMBLER.eq(docA)))
               .execute();
        }

        assertThat(physicalCollection(docA))
            .as("document A moved, as asked")
            .isEqualTo(DST);
        assertThat(physicalCollection(docB))
            .as("document B was NOT in the batch, so its own row stayed behind")
            .isEqualTo(SRC);
        // THE TEAR. B's shared chash cascaded to the target; B's other chash did not.
        assertThat(manifestCollections(docB))
            .as("THE HAZARD: document B's manifest is now split across both "
                + "collections because the chash it shares with A cascaded away from "
                + "under it. This is why the op must close each batch over shared "
                + "chashes -- batching by document alone does not prevent a tear.")
            .containsExactlyInAnyOrder(DST, SRC);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — the op: one transaction, whole collection, merge-and-report
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * The whole collection moves in ONE transaction, and nothing tears — including the
     * shared-chash peers GROUP 1 showed a batched design would split. No closure is
     * needed to achieve this; there is simply no partial state to be in.
     */
    @Test
    @Order(20)
    void wholeCollectionMoves_inOneTransaction_includingSharedChashPeers() throws Exception {
        var p = freshPair("whole");
        String docA = "whole-a";
        String docB = "whole-b";
        String shared = Chash.ofText("whole-shared").toHex();
        String onlyA = Chash.ofText("whole-only-a").toHex();
        String onlyB = Chash.ofText("whole-only-b").toHex();
        seedDocument(docA, p.src());
        seedDocument(docB, p.src());
        vecRepo.upsertChunks(TENANT, p.src(), List.of(shared, onlyA, onlyB),
            List.of("s", "a", "b"), List.of(Map.of(), Map.of(), Map.of()));
        catalogRepo.writeManifest(TENANT, docA, p.src(), List.of(
            Map.of("position", 0, "chash", shared), Map.of("position", 1, "chash", onlyA)));
        catalogRepo.writeManifest(TENANT, docB, p.src(), List.of(
            Map.of("position", 0, "chash", shared), Map.of("position", 1, "chash", onlyB)));

        var r = catalogRepo.rehomeCollection(TENANT, p.src(), p.dst());

        assertThat(r.movedChunks()).isEqualTo(3);
        assertThat(r.movedDocuments()).isEqualTo(2);
        assertThat(manifestCollections(docA))
            .as("the cascade carried the manifest; the op never writes it directly")
            .containsOnly(p.dst());
        assertThat(manifestCollections(docB))
            .as("the peer sharing a chash is whole in the target, not split as GROUP 1 "
                + "showed a batched move would leave it")
            .containsOnly(p.dst());
        assertThat(physicalCollection(docA)).isEqualTo(p.dst());
        assertThat(physicalCollection(docB)).isEqualTo(p.dst());
        assertThat(r.status().done()).isTrue();
        assertThat(r.leftBehindByTable()).as("nothing collided here").isEmpty();
        assertThat(danglingManifests()).isZero();
    }

    /**
     * Chunk rows no manifest references still move. They are invisible to any
     * document-driven view of the collection, so an op that walked documents would
     * report success and leave them behind.
     */
    @Test
    @Order(21)
    void manifestLessChunks_moveToo() throws Exception {
        var p = freshPair("orph");
        String doc = "orph-doc";
        String docChash = Chash.ofText("orph-doc-text").toHex();
        String orphan = Chash.ofText("orph-orphan-text").toHex();
        seedDocument(doc, p.src());
        vecRepo.upsertChunks(TENANT, p.src(), List.of(docChash, orphan),
            List.of("doc text", "orphan text"), List.of(Map.of(), Map.of()));
        catalogRepo.writeManifest(TENANT, doc, p.src(),
            List.of(Map.of("position", 0, "chash", docChash)));

        var r = catalogRepo.rehomeCollection(TENANT, p.src(), p.dst());

        assertThat(r.movedChunks()).isEqualTo(2);
        assertThat(r.status().remainingChunks())
            .as("the chunk nothing references left with the rest")
            .isZero();
        assertThat(r.status().done()).isTrue();
    }

    /**
     * THE MERGE CASE, and the one the PITR fork found: a row whose move would collide
     * with a row already at the target is LEFT WHERE IT IS and COUNTED, never dropped
     * and never allowed to abort the transaction (Sam's ruling 2026-09-16, "move and
     * report").
     *
     * <p>Built on {@code taxonomy_meta}, whose primary key is {@code (tenant_id,
     * collection)} — the same shape as {@code search_telemetry_pk}, which is what
     * actually collided on production data, and reproducible here without standing up
     * a telemetry fixture. The collision predicate is derived from the table's own
     * unique keys, so this exercises the general mechanism rather than a special case.
     */
    @Test
    @Order(22)
    void collidingRow_isLeftBehindAndCounted_ratherThanDroppedOrFatal() throws Exception {
        var p = freshPair("merge");
        // Both sides hold a taxonomy_meta row. Moving the source's would violate
        // (tenant_id, collection) on the target's.
        seedTaxonomyMeta(p.src());
        seedTaxonomyMeta(p.dst());
        String doc = "merge-doc";
        String chash = Chash.ofText("merge-text").toHex();
        seedDocument(doc, p.src());
        vecRepo.upsertChunks(TENANT, p.src(), List.of(chash), List.of("t"), List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, doc, p.src(), List.of(Map.of("position", 0, "chash", chash)));

        var r = catalogRepo.rehomeCollection(TENANT, p.src(), p.dst());

        assertThat(r.movedChunks())
            .as("the collision must not abort the transaction -- the naive whole-set "
                + "UPDATE died on search_telemetry_pk and rolled back everything")
            .isEqualTo(1);
        assertThat(r.movedDocuments()).isEqualTo(1);
        assertThat(r.leftBehindByTable())
            .as("REPORTED, not swallowed: an operator reading this has to see that a row "
                + "stayed behind, or move-and-report is just move")
            .containsEntry("taxonomy_meta", 1);
        assertThat(taxonomyMetaCount(p.src()))
            .as("and the row is still there -- ON CONFLICT DO NOTHING would have "
                + "destroyed it silently")
            .isEqualTo(1);
        assertThat(taxonomyMetaCount(p.dst())).isEqualTo(1);
        assertThat(r.status().done())
            .as("done() is false while a left-behind row still names the source, so a "
                + "caller polling for completion is told the truth rather than a "
                + "convenient one")
            .isFalse();
        assertThat(r.status().remainingByTable()).containsEntry("taxonomy_meta", 1);
    }

    /** The status read is the same answer as the submit's, from a separate call. */
    @Test
    @Order(23)
    void status_readsTheSameRemainderAsTheMoveReported() throws Exception {
        var p = freshPair("status");
        String doc = "status-doc";
        String chash = Chash.ofText("status-text").toHex();
        seedDocument(doc, p.src());
        vecRepo.upsertChunks(TENANT, p.src(), List.of(chash), List.of("t"), List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, doc, p.src(), List.of(Map.of("position", 0, "chash", chash)));

        var before = catalogRepo.rehomeStatus(TENANT, p.src());
        assertThat(before.done()).isFalse();
        assertThat(before.remainingChunks()).isEqualTo(1);

        var r = catalogRepo.rehomeCollection(TENANT, p.src(), p.dst());
        var after = catalogRepo.rehomeStatus(TENANT, p.src());

        assertThat(after.remainingRows())
            .as("a caller cut mid-request polls this and must get exactly what the "
                + "response would have told them")
            .isEqualTo(r.status().remainingRows());
        assertThat(after.done()).isTrue();
    }

    /** Both registry rows survive; a re-home never creates, revives or retires one. */
    @Test
    @Order(24)
    void bothRegistryRowsSurvive_andStayLive() throws Exception {
        var p = freshPair("reg");
        String doc = "reg-doc";
        String chash = Chash.ofText("reg-text").toHex();
        seedDocument(doc, p.src());
        vecRepo.upsertChunks(TENANT, p.src(), List.of(chash), List.of("t"), List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, doc, p.src(), List.of(Map.of("position", 0, "chash", chash)));

        catalogRepo.rehomeCollection(TENANT, p.src(), p.dst());

        for (String name : List.of(p.src(), p.dst())) {
            Map<String, Object> row = catalogRepo.getCollection(TENANT, name);
            assertThat(row).as(name + " registry row must survive").isNotNull();
            assertThat((String) row.get("superseded_by"))
                .as(name + " must remain LIVE -- chunks -> catalog_collections is ON DELETE "
                    + "RESTRICT and nothing here retires a collection. The source is NOT "
                    + "reclaimed afterwards either: the ghost sweep is one-shot per tenant "
                    + "and already spent on the real estate (nexus-29drn)")
                .isEmpty();
        }
    }

    /** The refusals, each naming its own reason. */
    @Test
    @Order(25)
    void refusals_sameCollection_andUnregisteredEndpoints() throws Exception {
        var p = freshPair("ref");
        assertThatThrownBy(() -> catalogRepo.rehomeCollection(TENANT, p.src(), p.src()))
            .isInstanceOf(CatalogRepository.RehomeRefused.class)
            .hasMessageContaining("same collection");
        assertThatThrownBy(() -> catalogRepo.rehomeCollection(
                TENANT, "knowledge__rehome-absent__minilm-l6-v2-384__v1", p.dst()))
            .as("an unregistered source must fail loud, not return an all-zero no-op")
            .isInstanceOf(CatalogRepository.RehomeRefused.class)
            .hasMessageContaining("source collection");
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    /** A taxonomy_meta row, whose PK (tenant_id, collection) is the collision shape. */
    private void seedTaxonomyMeta(String collection) {
        tenantScope.withTenant(TENANT, ctx -> ctx
            .insertInto(DSL.table(DSL.name("nexus", "taxonomy_meta")),
                DSL.field(DSL.name("tenant_id"), String.class),
                DSL.field(DSL.name("collection"), String.class),
                DSL.field(DSL.name("last_discover_doc_count"), Integer.class))
            .values(TENANT, collection, 1)
            .execute());
    }

    private int taxonomyMetaCount(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(
            DSL.table(DSL.name("nexus", "taxonomy_meta")),
            DSL.field("collection", String.class).eq(collection)));
    }

    private record Pair(String src, String dst) {}

    /** A fresh live source/target pair, so GROUP 2's cases do not interfere. */
    private Pair freshPair(String slug) throws Exception {
        String src = "knowledge__rehome-" + slug + "-src__minilm-l6-v2-384__v1";
        String dst = "knowledge__rehome-" + slug + "-dst__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, src);
            PgContainerHelper.insertCollection(dsl, TENANT, dst);
        }
        return new Pair(src, dst);
    }

    /**
     * Manifest rows whose {@code (collection, chash)} has no {@code nexus.chunks}
     * row — the invariant the op must never break. Joined on the PAIR, never on
     * chash alone: chashes recur across collections, so a chash-only join
     * undercounts the danglers it is meant to find.
     */
    private int danglingManifests() {
        return tenantScope.withTenant(TENANT, ctx -> ctx
            .fetchCount(
                DSL.table(DSL.name("nexus", "catalog_document_chunks")).as("m"),
                DSL.notExists(ctx.selectOne()
                    .from(DSL.table(DSL.name("nexus", "chunks")).as("c"))
                    .where(DSL.field(DSL.name("c", "collection"), String.class)
                            .eq(DSL.field(DSL.name("m", "collection"), String.class))
                        .and(DSL.field(DSL.name("c", "chash"), byte[].class)
                            .eq(DSL.field(DSL.name("m", "chash"), byte[].class)))))));
    }

    private void seedDocument(String tumbler, String collection) {
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", tumbler, "content_type", "paper",
            "corpus", "knowledge", "physical_collection", collection));
    }

    private String physicalCollection(String tumbler) {
        return tenantScope.withTenant(TENANT, ctx -> ctx
            .select(DSL.field("physical_collection", String.class))
            .from(DSL.table(DSL.name("nexus", "catalog_documents")))
            .where(DSL.field("tumbler", String.class).eq(tumbler))
            .fetchOne(0, String.class));
    }

    private List<String> manifestCollections(String tumbler) {
        return tenantScope.withTenant(TENANT, ctx -> ctx
            .select(DSL.field("collection", String.class))
            .from(DSL.table(DSL.name("nexus", "catalog_document_chunks")))
            .where(DSL.field("doc_id", String.class).eq(tumbler))
            .fetch(0, String.class));
    }
}
