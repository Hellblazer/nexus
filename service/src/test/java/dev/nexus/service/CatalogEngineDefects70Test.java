// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * The 7.0.0 engine-defect arc — repository-level contract tests for the fix set
 * carried by beads nexus-mqd6t, nexus-e4gel, nexus-s4e1n, nexus-9ssih,
 * nexus-tz1cx, nexus-ekaxn, nexus-jqvzk and nexus-a3kbf.
 *
 * <p>Every test here is the JAVA half of a Python contract that is currently
 * pinned by a {@code xfail(strict=True)} marker naming the same bead
 * (tests/db/test_i711w_gap_xfails.py, tests/db/test_i711w_gap_contracts.py,
 * tests/db/test_i711w_gap_contracts_fresh.py, tests/test_auto_linker.py,
 * tests/test_catalog_manifest_read_api.py, tests/test_indexer.py). The Python
 * markers are flipped by the ORCHESTRATOR once this engine ships; the pins
 * below are the engine's own, and they must stay green independently.
 *
 * <p>One container for the whole set (each service test class starts its own
 * Testcontainers PG — see {@link PgContainerHelper}); grouping keeps the
 * suite's container count flat while the arc lands eight beads.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogEngineDefects70Test {

    private static final String TENANT = "defects70-tenant";
    private static final String SVC_ROLE = "svc_defects70";
    private static final String SVC_PASS = "svc_defects70_pass";

    /** Canonical 64-hex chash from a seed (RDR-180: the full sha256 IS the chash). */
    private static String ch(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    private static Map<String, Object> row(int position, String chash) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("position", position);
        m.put("chash", chash);
        m.put("chunk_index", position);
        return m;
    }

    /**
     * RDR-191 Phase 5 (nexus-o8dil.29): {@code fk_catalog_chunks_chunk} now requires
     * every {@code catalog_document_chunks} row's {@code (tenant_id, collection,
     * chash)} to have a matching {@code nexus.chunks} row. Every manifest-write call
     * site below is routed through one of the {@code *Seeded} wrappers, which stub a
     * minimal {@code nexus.chunks} row (single {@code embedding_384} vector,
     * arbitrary text) for each row's chash first. A chash that is null, not valid
     * hex, or not EXACTLY 64 hex chars (32 bytes) is left unstubbed on purpose:
     * nexus.chunks carries the SAME {@code chunks_chash_octet_check} (octet_length=32)
     * as catalog_document_chunks, so a wrong-length stub would itself violate that
     * check rather than reaching whatever violation a given test is actually after.
     */
    private static final String STUB_VECTOR_384 =
        "[" + "0.1,".repeat(383) + "0.1]";

    private void stubChunk(String tenant, String collection, Object chashObj) {
        if (!(chashObj instanceof String chashHex) || chashHex.length() != 64) {
            return;
        }
        if (!chashHex.matches("(?i)^[0-9a-f]+$")) {
            return;
        }
        tenantScope.withTenant(tenant, ctx -> {
            // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
            // chunks_collection_fk (tenant_id, collection) -> catalog_collections
            // (tenant_id, name) — stub-register the collection first, mirroring
            // PgVectorRepository#upsertChunks' own ensure-registered step.
            ctx.execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                + "ON CONFLICT (tenant_id, name) DO NOTHING",
                tenant, collection);
            return ctx.execute(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
                + "VALUES (?, ?, decode(?, 'hex'), 'stub', ?::vector) "
                + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING",
                tenant, collection, chashHex, STUB_VECTOR_384);
        });
    }

    private void writeManifestSeeded(String tenant, String docId, String collection,
                                      List<Map<String, Object>> rows) {
        for (var row : rows) {
            stubChunk(tenant, collection, row.get("chash"));
        }
        repo.writeManifest(tenant, docId, collection, rows);
    }

    private void appendManifestChunksSeeded(String tenant, String docId, String collection,
                                             List<Map<String, Object>> rows) {
        for (var row : rows) {
            stubChunk(tenant, collection, row.get("chash"));
        }
        repo.appendManifestChunks(tenant, docId, collection, rows);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> writeManifestManySeeded(String tenant, List<Map<String, Object>> docs,
                                                          String collection) {
        for (var doc : docs) {
            var rows = (List<Map<String, Object>>) doc.get("rows");
            if (rows != null) {
                for (var row : rows) {
                    stubChunk(tenant, collection, row.get("chash"));
                }
            }
        }
        return repo.writeManifestMany(tenant, docs, collection);
    }

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    /** Distinct owner prefix per test so no two tests share a tumbler namespace. */
    private int ownerSeq = 100;

    private String freshOwner() {
        String prefix = String.valueOf(++ownerSeq);
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", prefix,
            "name", "defects70-" + prefix,
            "owner_type", "repo"));
        return prefix;
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; "
                + "  END IF; "
                + "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; "
                + "  END IF; "
                + "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);

        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-mqd6t — tombstone filters on the manifest-side reads
    // ══════════════════════════════════════════════════════════════════════════

    /** BUG 1: chashesForCollection is the T3 GC alive-set; a tombstoned doc's
     *  chunks must leave it or `nx t3 gc` never collects those vectors. */
    @Test
    void mqd6t_chashesForCollection_excludesTombstonedDoc() {
        String owner = freshOwner();
        String coll = "code__defects70-gc1__voyage-code-3__v1";
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "gc1", "content_type", "code",
            "physical_collection", coll,
            "source_uri", "file:///defects70/gc1/doc.md"));
        String h = ch("defects70-gc1-chunk");
        writeManifestSeeded(TENANT, t, coll, List.of(row(0, h)));

        // NON-VACUITY: visible while live.
        assertThat(repo.chashesForCollection(TENANT, coll)).contains(h);

        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        assertThat(repo.chashesForCollection(TENANT, coll))
            .as("tombstoned doc's chunks must leave the T3 GC alive-set")
            .doesNotContain(h);
    }

    /** BUG 2: docsForChashes is search-hit attribution; it must never name a
     *  document the user deleted. */
    @Test
    void mqd6t_docsForChashes_excludesTombstonedDoc() {
        String owner = freshOwner();
        String coll = "code__defects70-gc2__voyage-code-3__v1";
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "gc2", "content_type", "code",
            "physical_collection", coll,
            "source_uri", "file:///defects70/gc2/doc.md"));
        String h = ch("defects70-gc2-chunk");
        writeManifestSeeded(TENANT, t, coll, List.of(row(0, h)));

        assertThat(repo.docsForChashes(TENANT, List.of(h))).containsExactly(t);

        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        assertThat(repo.docsForChashes(TENANT, List.of(h)))
            .as("a deleted document must never be attributed a search hit")
            .doesNotContain(t);
    }

    /**
     * The FIFTH sibling (review H1 / critique C3): resolveChash's doc_id
     * attribution. Same user-visible shape as BUG 2 — content served over
     * /resolve_chash must not be attributed to a document the user deleted.
     *
     * <p>Two live documents are NOT required to make this fail: with the filter
     * absent, the tombstoned doc's manifest row is still the only one matching,
     * so the endpoint hands back a dead doc_id.
     */
    @Test
    void mqd6t_resolveChash_doesNotAttributeToTombstonedDoc() throws Exception {
        String owner = freshOwner();
        String coll = "code__defects70-rchash__voyage-code-3__v1";
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "rchash", "content_type", "code",
            "physical_collection", coll,
            "source_uri", "file:///defects70/rchash/doc.md"));
        String h = ch("defects70-rchash-chunk");
        writeManifestSeeded(TENANT, t, coll, List.of(row(0, h)));
        seedChunkRow(coll, h, "resolve-chash body");

        var before = repo.resolveChash(TENANT, h, coll);
        assertThat(before).as("precondition: the live doc is attributed").isNotNull();
        assertThat(before.get("doc_id")).isEqualTo(t);

        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        var after = repo.resolveChash(TENANT, h, coll);
        if (after != null) {
            assertThat(after.get("doc_id"))
                .as("resolve_chash must not attribute chunk content to a DELETED document")
                .isNotEqualTo(t);
        }
    }

    /**
     * resyncChunkCount must not write through to a tombstoned row — the same
     * non-resurrection rule updateDocument enforces (review M1 / critique C3).
     */
    @Test
    void mqd6t_resyncChunkCount_refusesTombstonedTarget() {
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "resync-dead", "content_type", "prose", "chunk_count", 0,
            "source_uri", "file:///defects70/resyncdead/doc.md"));
        appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-resyncdead__v1",
            List.of(row(0, ch("defects70-rs-0"))));
        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        assertThat(repo.resyncChunkCount(TENANT, t))
            .as("a resync must not mutate a soft-deleted document")
            .isEqualTo(0);
    }

    /** Item-8 sub-finding: a soft-deleted doc's manifest must not stay publicly
     *  readable via /manifest/get while resolve() returns null. */
    @Test
    void mqd6t_getManifest_excludesTombstonedDoc() {
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "man1", "content_type", "code",
            "physical_collection", "code__defects70-man1__voyage-code-3__v1",
            "source_uri", "file:///defects70/man1/doc.md"));
        appendManifestChunksSeeded(TENANT, t, "code__defects70-man1__voyage-code-3__v1", List.of(
            row(0, ch("defects70-man1-0")),
            row(1, ch("defects70-man1-1")),
            row(2, ch("defects70-man1-2"))));

        assertThat(repo.getManifest(TENANT, t)).hasSize(3);

        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        assertThat(repo.getDocument(TENANT, t)).isNull();
        assertThat(repo.getManifest(TENANT, t))
            .as("manifest of a tombstoned document must read empty")
            .isEmpty();
    }

    /** getManifestMany is the batch sibling — the same read, and the one the
     *  client's docs_for_chashes reconstruction actually consumes. */
    @Test
    void mqd6t_getManifestMany_excludesTombstonedDoc() {
        // RDR-191: writeManifest requires the caller-supplied collection
        // (Hal ruling 2026-08-12) — no physical_collection registration
        // needed, the document's own registered collection is irrelevant to
        // the manifest stamp now.
        String coll = "code__defects70-mqd6t__voyage-code-3__v1";
        String owner = freshOwner();
        String live = repo.registerDocument(TENANT, owner, Map.of(
            "title", "many-live", "content_type", "code",
            "source_uri", "file:///defects70/many/live.md"));
        String dead = repo.registerDocument(TENANT, owner, Map.of(
            "title", "many-dead", "content_type", "code",
            "source_uri", "file:///defects70/many/dead.md"));
        writeManifestSeeded(TENANT, live, coll, List.of(row(0, ch("defects70-many-live"))));
        writeManifestSeeded(TENANT, dead, coll, List.of(row(0, ch("defects70-many-dead"))));

        assertThat(repo.getManifestMany(TENANT, List.of(live, dead)))
            .containsKeys(live, dead);

        assertThat(repo.deleteDocument(TENANT, dead)).isEqualTo(1);

        var after = repo.getManifestMany(TENANT, List.of(live, dead));
        assertThat(after).containsKey(live);
        assertThat(after)
            .as("batch manifest read must drop tombstoned docs, like its single-doc twin")
            .doesNotContainKey(dead);
    }

    /** Non-resurrection, half 1: an UPDATE must never bring a tombstone back. */
    @Test
    void mqd6t_updateDocument_refusesTombstonedTarget() {
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "nonres", "content_type", "code",
            "source_uri", "file:///defects70/nonres/doc.md"));
        assertThat(repo.updateDocument(TENANT, t, Map.of("title", "live-edit"))).isEqualTo(1);

        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        assertThat(repo.updateDocument(TENANT, t, Map.of("title", "zombie")))
            .as("update must refuse a tombstoned target (0 rows), never resurrect it")
            .isZero();
        assertThat(repo.getDocument(TENANT, t)).isNull();
    }

    /** Non-resurrection, half 2: an explicit REGISTER of a tombstoned tumbler
     *  un-tombstones it — the one sanctioned way back. */
    @Test
    void mqd6t_registerUnTombstonesExplicitly() {
        String owner = freshOwner();
        String tumbler = owner + ".9001";
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "resurrect-me", "content_type", "code",
            "source_uri", "file:///defects70/resurrect/doc.md"));
        assertThat(repo.getDocument(TENANT, tumbler)).isNotNull();

        assertThat(repo.deleteDocument(TENANT, tumbler)).isEqualTo(1);
        assertThat(repo.getDocument(TENANT, tumbler)).isNull();

        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "resurrected", "content_type", "code",
            "source_uri", "file:///defects70/resurrect/doc.md"));

        var doc = repo.getDocument(TENANT, tumbler);
        assertThat(doc)
            .as("an explicit register must un-tombstone the row it addresses")
            .isNotNull();
        assertThat(doc.get("title")).isEqualTo("resurrected");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-e4gel — chunk_count re-derivation
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * nexus-zq79 F4: update() OMITTING chunk_count re-derives it from the manifest.
     *
     * <p>The stale seed is load-bearing (review L1). appendManifestChunks folds
     * the count itself, so asserting 5 straight after the append would pass with
     * the update-path re-derivation reverted — proving nothing. Forcing a
     * disagreeing count first means only the update path can restore it.
     */
    @Test
    void e4gel_updateReDerivesChunkCountWhenOmitted() throws Exception {
        // RDR-191 (Hal ruling 2026-08-12): appendManifestChunks requires an
        // explicit caller-supplied collection — the document's own
        // physical_collection is irrelevant to the manifest stamp now.
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "rederive", "content_type", "prose", "chunk_count", 0,
            "source_uri", "file:///defects70/rederive/doc.md"));
        appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-rederive__voyage-context-3__v1", List.of(
            row(0, ch("defects70-rd-0")), row(1, ch("defects70-rd-1")),
            row(2, ch("defects70-rd-2")), row(3, ch("defects70-rd-3")),
            row(4, ch("defects70-rd-4"))));
        forceStaleChunkCount(t, 99);
        assertThat(repo.getDocument(TENANT, t).get("chunk_count"))
            .as("precondition: the count DISAGREES with the 5-row manifest")
            .isEqualTo(99);

        repo.updateDocument(TENANT, t, Map.of("head_hash", "updated"));

        assertThat(repo.getDocument(TENANT, t).get("chunk_count"))
            .as("update() omitting chunk_count must re-derive it from the manifest")
            .isEqualTo(5);
    }

    /** …and an EXPLICIT chunk_count still wins (orphan-backfill paths). */
    @Test
    void e4gel_updateRespectsCallerSuppliedChunkCount() {
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "caller-count", "content_type", "prose", "chunk_count", 0,
            "source_uri", "file:///defects70/callercount/doc.md"));
        appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-callercount__v1", List.of(
            row(0, ch("defects70-cc-0")), row(1, ch("defects70-cc-1")),
            row(2, ch("defects70-cc-2"))));

        repo.updateDocument(TENANT, t, Map.of("chunk_count", 99));

        assertThat(repo.getDocument(TENANT, t).get("chunk_count")).isEqualTo(99);
    }

    /**
     * THE H2 GUARD (Hal decision 2026-07-30): an incidental update must NOT
     * zero a positive count against an EMPTY manifest.
     *
     * <p>That disagreement is the GH #1371 damage signature and the only
     * discriminator `nx catalog reconcile` classifies on (manifest_heal.py
     * sorts unrebuildable docs into "chunks LOST" vs "never-chunked" on exactly
     * this field). Letting a routine head_hash bump heal the number would
     * rewrite a real data-loss event as expected noise.
     */
    @Test
    void e4gel_updateDoesNotZeroPositiveCountAgainstEmptyManifest() throws Exception {
        String tumbler = "9101.1";
        String coll = "code__defects70-h2guard__voyage-code-3__v1";
        injectDamagedDoc(tumbler, coll, 7, "{\"content_hash\":\"deadbeef\"}");
        assertThat(repo.getManifest(TENANT, tumbler))
            .as("precondition: the damage shape is count>0 with an EMPTY manifest")
            .isEmpty();

        // The incidental update: a head_hash bump that never mentions chunk_count.
        repo.updateDocument(TENANT, tumbler, Map.of("head_hash", "routine-bump"));

        assertThat(repo.getDocument(TENANT, tumbler).get("chunk_count"))
            .as("the damage signal must survive an update that did not ask to change it")
            .isEqualTo(7);
    }

    /** ...but an EXPLICIT zero still wins: the guard refuses only the unasked-for one. */
    @Test
    void e4gel_explicitZeroStillClearsTheCount() throws Exception {
        String tumbler = "9102.1";
        String coll = "code__defects70-h2explicit__voyage-code-3__v1";
        injectDamagedDoc(tumbler, coll, 7, "{\"content_hash\":\"deadbeef\"}");

        repo.updateDocument(TENANT, tumbler, Map.of("chunk_count", 0));

        assertThat(repo.getDocument(TENANT, tumbler).get("chunk_count"))
            .as("caller intent wins — the guard is only about the incidental path")
            .isEqualTo(0);
    }

    /** appendManifestChunks must fold chunk_count like writeManifestRows does. */
    @Test
    void e4gel_appendManifestFoldsChunkCount() {
        // RDR-191 (Hal ruling 2026-08-12): appendManifestChunks requires an
        // explicit caller-supplied collection now.
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "append-fold", "content_type", "prose", "chunk_count", 0,
            "source_uri", "file:///defects70/appendfold/doc.md"));

        appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-appendfold__voyage-context-3__v1", List.of(
            row(0, ch("defects70-af-0")), row(1, ch("defects70-af-1"))));
        assertThat(repo.getDocument(TENANT, t).get("chunk_count"))
            .as("append must fold the count in the same transaction")
            .isEqualTo(2);

        appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-appendfold__voyage-context-3__v1",
            List.of(row(2, ch("defects70-af-2"))));
        assertThat(repo.getDocument(TENANT, t).get("chunk_count")).isEqualTo(3);
    }

    /**
     * Batch update shares the builder, so it shares the re-derivation.
     * Stale-seeded for the same reason as the single-doc case (review L1).
     */
    @Test
    void e4gel_updateManyReDerivesChunkCount() throws Exception {
        // RDR-191 (Hal ruling 2026-08-12): appendManifestChunks requires an
        // explicit caller-supplied collection now.
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "batch-rederive", "content_type", "prose", "chunk_count", 0,
            "source_uri", "file:///defects70/batchrederive/doc.md"));
        appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-batchrederive__voyage-context-3__v1", List.of(
            row(0, ch("defects70-br-0")), row(1, ch("defects70-br-1"))));
        forceStaleChunkCount(t, 77);
        assertThat(repo.getDocument(TENANT, t).get("chunk_count"))
            .as("precondition: the count DISAGREES with the 2-row manifest")
            .isEqualTo(77);

        var results = repo.updateDocumentsMany(TENANT, List.of(
            Map.of("tumbler", t, "head_hash", "batched")));
        assertThat(results).containsExactly(1);

        assertThat(repo.getDocument(TENANT, t).get("chunk_count")).isEqualTo(2);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-927mo — /update must stamp indexed_at when head_hash CHANGES
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void mo927_updateStampsIndexedAtWhenHeadHashChanges() throws Exception {
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "idxat-changed", "content_type", "prose",
            "source_uri", "file:///defects70/idxatchanged/doc.md"));
        repo.updateDocument(TENANT, t, Map.of("indexed_at", "2020-01-01T00:00:00.000000+00:00"));
        String original = (String) repo.getDocument(TENANT, t).get("indexed_at");
        assertThat(original)
            .as("precondition: a known, controlled original value")
            .isEqualTo("2020-01-01T00:00:00.000000+00:00");

        Thread.sleep(10);
        repo.updateDocument(TENANT, t, Map.of("head_hash", "rev2"));

        String refreshed = (String) repo.getDocument(TENANT, t).get("indexed_at");
        assertThat(refreshed)
            .as("indexed_at must advance when update() changes head_hash")
            .isNotEqualTo(original);
    }

    @Test
    void mo927_updateDoesNotStampIndexedAtWhenHeadHashUnchanged() {
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "idxat-same", "content_type", "prose", "head_hash", "same-hash",
            "source_uri", "file:///defects70/idxatsame/doc.md"));
        repo.updateDocument(TENANT, t, Map.of("indexed_at", "2020-01-01T00:00:00.000000+00:00"));
        String original = (String) repo.getDocument(TENANT, t).get("indexed_at");

        // Resubmitting the SAME head_hash (plus an unrelated field) must NOT stamp.
        repo.updateDocument(TENANT, t, Map.of("head_hash", "same-hash", "title", "idxat-same-2"));

        assertThat(repo.getDocument(TENANT, t).get("indexed_at"))
            .as("an update that does not actually CHANGE head_hash must not stamp indexed_at")
            .isEqualTo(original);
        assertThat(repo.getDocument(TENANT, t).get("title")).isEqualTo("idxat-same-2");
    }

    @Test
    void mo927_explicitIndexedAtStillWinsOverHeadHashChange() {
        // Mirrors e4gel_updateRespectsCallerSuppliedChunkCount's "caller intent
        // wins" shape: an explicit indexed_at in the SAME update as a head_hash
        // change must not be clobbered by the auto-stamp.
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "idxat-explicit", "content_type", "prose",
            "source_uri", "file:///defects70/idxatexplicit/doc.md"));

        repo.updateDocument(TENANT, t, Map.of(
            "head_hash", "rev-explicit", "indexed_at", "2021-06-01T00:00:00.000000+00:00"));

        assertThat(repo.getDocument(TENANT, t).get("indexed_at"))
            .isEqualTo("2021-06-01T00:00:00.000000+00:00");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-eldyi — the seven write paths bypassing the non-resurrection rule
    // ══════════════════════════════════════════════════════════════════════════

    /** Register, tombstone, and return the tumbler — shared fixture for the eldyi block. */
    private String tombstonedDoc(String tag) {
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", tag, "content_type", "prose",
            "source_uri", "file:///defects70/eldyi/" + tag + "/doc.md"));
        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);
        return t;
    }

    @Test
    void eldyi_stampIndexedAt_rollsBackCleanlyOnTombstonedDoc() throws Exception {
        // stampIndexedAt itself is advisory (guarded, silent no-op — no
        // exception of its own), called from appendManifestChunks BEFORE that
        // method's own chunk_count guard fires and throws. This proves the
        // combination is correct end-to-end: nothing from the refused attempt
        // survives, including a stray indexed_at stamp, because the whole
        // transaction rolls back on the throw.
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "stampidx", "content_type", "prose",
            "source_uri", "file:///defects70/eldyi/stampidx/doc.md"));
        repo.updateDocument(TENANT, t, Map.of("indexed_at", "2020-01-01T00:00:00.000000+00:00"));
        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        assertThatThrownBy(() -> appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-stampidx__v1",
                List.of(row(0, ch("defects70-eldyi-stampidx-0")))))
            .isInstanceOf(CatalogRepository.TombstonedDocumentException.class);

        // Read indexed_at directly (raw SQL, bypassing the deleted_at read
        // filter — getDocument would just return null for a tombstoned row)
        // and confirm it is exactly the pre-tombstone value: untouched by the
        // refused attempt above, proving the whole transaction rolled back
        // cleanly rather than leaving a stray indexed_at stamp behind.
        assertThat(rawIndexedAt(t))
            .as("a refused appendManifestChunks attempt while tombstoned must leave indexed_at untouched")
            .isEqualTo("2020-01-01T00:00:00.000000+00:00");
    }

    /** Raw read of indexed_at bypassing RLS/tombstone filtering (nexus-eldyi). */
    private String rawIndexedAt(String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "SELECT indexed_at FROM nexus.catalog_documents WHERE tenant_id = ? AND tumbler = ?")) {
                ps.setString(1, TENANT);
                ps.setString(2, tumbler);
                try (var rs = ps.executeQuery()) {
                    if (!rs.next()) {
                        throw new IllegalStateException("rawIndexedAt found no row for " + tumbler);
                    }
                    return rs.getString(1);
                }
            }
        }
    }

    @Test
    void eldyi_writeManifest_refusesTombstonedDoc() {
        String t = tombstonedDoc("writemanifest");
        assertThatThrownBy(() -> writeManifestSeeded(TENANT, t, "knowledge__defects70-writemanifest__v1",
                List.of(row(0, ch("defects70-eldyi-wm-0")))))
            .as("writeManifest must refuse a tombstoned target, not silently write orphan chunks")
            .isInstanceOf(CatalogRepository.TombstonedDocumentException.class);
        assertThat(repo.getManifest(TENANT, t))
            .as("the refused write's manifest rows must not have landed (rolled back)")
            .isEmpty();
    }

    @Test
    void eldyi_appendManifestChunks_refusesTombstonedDoc() {
        String t = tombstonedDoc("appendmanifest");
        assertThatThrownBy(() -> appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-appendmanifest__v1",
                List.of(row(0, ch("defects70-eldyi-am-0")))))
            .as("appendManifestChunks must refuse a tombstoned target")
            .isInstanceOf(CatalogRepository.TombstonedDocumentException.class);
        assertThat(repo.getManifest(TENANT, t)).isEmpty();
    }

    @Test
    void eldyi_purgeManifest_refusesTombstonedDocWithManifestRows() {
        // Manifest rows must exist BEFORE the tombstone (soft delete does not
        // cascade to the manifest, RDR-156 P1.2) so the purge has real work to
        // refuse, not an incidental empty-manifest 0.
        String owner = freshOwner();
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", "purgemanifest", "content_type", "prose",
            "source_uri", "file:///defects70/eldyi/purgemanifest/doc.md"));
        appendManifestChunksSeeded(TENANT, t, "knowledge__defects70-purgemanifest__v1",
            List.of(row(0, ch("defects70-eldyi-pm-0"))));
        assertThat(repo.deleteDocument(TENANT, t)).isEqualTo(1);

        assertThatThrownBy(() -> repo.purgeManifest(TENANT, t))
            .as("purgeManifest must refuse a tombstoned target")
            .isInstanceOf(CatalogRepository.TombstonedDocumentException.class);
    }

    @Test
    void eldyi_purgeManifest_missingDocStaysSilentZero() {
        // The OTHER 0-rows case: a tumbler that was never registered at all is
        // NOT a tombstone refusal — same long-standing silent-0 contract.
        String owner = freshOwner();
        String neverRegistered = owner + ".999999";
        assertThat(repo.purgeManifest(TENANT, neverRegistered)).isZero();
    }

    @Test
    void eldyi_updateDocumentCollection_silentZeroOnTombstonedDoc() {
        String t = tombstonedDoc("updatecoll");
        assertThat(repo.updateDocumentCollection(TENANT, t, "code__eldyi-newcoll__voyage-code-3__v1"))
            .as("updateDocumentCollection returns the honest 0 rows-affected — no exception needed")
            .isZero();
    }

    @Test
    void eldyi_updateDocumentsCollectionBatch_silentZeroOnTombstonedDoc() {
        String t = tombstonedDoc("updatecollbatch");
        assertThat(repo.updateDocumentsCollectionBatch(TENANT, List.of(t), "code__eldyi-newcoll2__voyage-code-3__v1"))
            .isZero();
    }

    @Test
    void eldyi_setAlias_silentZeroOnTombstonedDoc() {
        String owner = freshOwner();
        String canonical = repo.registerDocument(TENANT, owner, Map.of(
            "title", "eldyi-alias-canonical", "content_type", "prose",
            "source_uri", "file:///defects70/eldyi/aliascanon/doc.md"));
        String t = tombstonedDoc("setalias");
        assertThat(repo.setAlias(TENANT, t, canonical)).isZero();
    }

    @Test
    void eldyi_upsertDocument_unTombstoneStillWorks() {
        // The pinned exemption: upsertDocument is the ONE sanctioned
        // un-tombstone (CatalogEngineDefects70Test mqd6t_registerUnTombstonesExplicitly
        // above already pins this; this is the eldyi-block twin verifying the
        // NEW guards on the other seven paths did not regress it).
        String owner = freshOwner();
        String tumbler = owner + ".8001";
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "eldyi-resurrect", "content_type", "code",
            "source_uri", "file:///defects70/eldyi/resurrect/doc.md"));
        assertThat(repo.deleteDocument(TENANT, tumbler)).isEqualTo(1);
        assertThat(repo.getDocument(TENANT, tumbler)).isNull();

        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "eldyi-resurrected", "content_type", "code",
            "source_uri", "file:///defects70/eldyi/resurrect/doc.md"));

        var doc = repo.getDocument(TENANT, tumbler);
        assertThat(doc).as("upsertDocument must still un-tombstone after the eldyi guards").isNotNull();
        assertThat(doc.get("title")).isEqualTo("eldyi-resurrected");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-s4e1n — link upsert must MERGE, not overwrite, attribution + metadata
    // ══════════════════════════════════════════════════════════════════════════

    private String[] linkPair(String tag) {
        String owner = freshOwner();
        String a = repo.registerDocument(TENANT, owner, Map.of(
            "title", tag + "-from", "content_type", "paper",
            "source_uri", "file:///defects70/" + tag + "/from.md"));
        String b = repo.registerDocument(TENANT, owner, Map.of(
            "title", tag + "-to", "content_type", "paper",
            "source_uri", "file:///defects70/" + tag + "/to.md"));
        return new String[]{a, b};
    }

    /** Re-type a wildcard JSON list as List&lt;String&gt; so AssertJ's varargs bind. */
    private static List<String> strings(Object raw) {
        return ((List<?>) raw).stream().map(String::valueOf).toList();
    }

    @SuppressWarnings("unchecked")
    private static List<String> coDiscovered(Map<String, Object> meta) {
        return (List<String>) meta.get("co_discovered_by");
    }

    private Map<String, Object> onlyEdge(String from, String to, String type) {
        var edges = repo.linksFrom(TENANT, from, List.of(type)).stream()
            .filter(e -> to.equals(e.get("to_tumbler")))
            .toList();
        assertThat(edges).hasSize(1);
        return edges.get(0);
    }

    @Test
    void s4e1n_secondCreatorPreservedAsCoDiscoveredBy() {
        var p = linkPair("codisc");
        assertThat(repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-x"))).isTrue();
        assertThat(repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-y"))).isFalse();

        var edge = onlyEdge(p[0], p[1], "cites");
        assertThat(edge.get("created_by"))
            .as("merged link must keep the ORIGINAL creator")
            .isEqualTo("agent-x");
        @SuppressWarnings("unchecked")
        var meta = (Map<String, Object>) edge.get("metadata");
        assertThat(meta).isNotNull();
        assertThat(coDiscovered(meta)).contains("agent-y");
    }

    @Test
    void s4e1n_mergePreservesExistingMetadataKeys() {
        var p = linkPair("metamerge");
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-x", "metadata", Map.of("confidence", 0.9, "keep", "me")));
        // Second call carries NO metadata at all — the pre-fix bug wrote NULL.
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-y"));

        @SuppressWarnings("unchecked")
        var meta = (Map<String, Object>) onlyEdge(p[0], p[1], "cites").get("metadata");
        assertThat(meta).containsEntry("keep", "me");
        assertThat(meta).containsKey("confidence");
        assertThat(coDiscovered(meta)).contains("agent-y");
    }

    @Test
    void s4e1n_sameCreatorTwiceDoesNotSelfCoDiscover() {
        var p = linkPair("samecreator");
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-x"));
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-x"));

        var edge = onlyEdge(p[0], p[1], "cites");
        assertThat(edge.get("created_by")).isEqualTo("agent-x");
        @SuppressWarnings("unchecked")
        var meta = (Map<String, Object>) edge.get("metadata");
        assertThat(coDiscovered(meta)).doesNotContain("agent-x");
    }

    @Test
    void s4e1n_thirdCreatorAppendsWithoutDuplicating() {
        var p = linkPair("thirdcreator");
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-x"));
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-y"));
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-y"));
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "agent-z"));

        @SuppressWarnings("unchecked")
        var meta = (Map<String, Object>) onlyEdge(p[0], p[1], "cites").get("metadata");
        var co = coDiscovered(meta);
        assertThat(co).containsExactlyInAnyOrder("agent-y", "agent-z");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-9ssih — dangling-endpoint rejection with allow_dangling opt-in
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void ssih_rejectsMissingToEndpoint() {
        var p = linkPair("dangle-to");
        var ex = assertThrows(CatalogRepository.DanglingEndpointException.class, () ->
            repo.upsertLink(TENANT, Map.of(
                "from_tumbler", p[0], "to_tumbler", p[1] + "0999",
                "link_type", "cites", "created_by", "auto-linker")));
        assertThat(ex.missing()).containsExactly("to_tumbler");
        assertThat(repo.linksFrom(TENANT, p[0], List.of("cites"))).isEmpty();
    }

    @Test
    void ssih_rejectsTombstonedEndpoint() {
        var p = linkPair("dangle-tombstone");
        assertThat(repo.deleteDocument(TENANT, p[1])).isEqualTo(1);
        var ex = assertThrows(CatalogRepository.DanglingEndpointException.class, () ->
            repo.upsertLink(TENANT, Map.of(
                "from_tumbler", p[0], "to_tumbler", p[1],
                "link_type", "cites", "created_by", "auto-linker")));
        assertThat(ex.missing()).containsExactly("to_tumbler");
    }

    @Test
    void ssih_reportsBothSidesWhenBothDangle() {
        var p = linkPair("dangle-both");
        var ex = assertThrows(CatalogRepository.DanglingEndpointException.class, () ->
            repo.upsertLink(TENANT, Map.of(
                "from_tumbler", p[0] + "0999", "to_tumbler", p[1] + "0999",
                "link_type", "cites", "created_by", "auto-linker")));
        assertThat(ex.missing()).containsExactlyInAnyOrder("from_tumbler", "to_tumbler");
    }

    @Test
    void ssih_allowDanglingOptInStillWrites() {
        var p = linkPair("dangle-allow");
        String ghost = p[1] + "0999";
        assertThat(repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", ghost,
            "link_type", "cites", "created_by", "importer",
            "allow_dangling", true))).isTrue();
        assertThat(repo.linksFrom(TENANT, p[0], List.of("cites"))).hasSize(1);
    }

    @Test
    void ssih_liveEndpointsStillLink() {
        var p = linkPair("dangle-ok");
        assertThat(repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1],
            "link_type", "cites", "created_by", "auto-linker"))).isTrue();
    }

    /** The ETL/import leg must stay unguarded: it legitimately writes edges for
     *  documents whose live state it does not yet control. */
    @Test
    void ssih_importLinkPathIsExempt() {
        var p = linkPair("dangle-import");
        repo.importLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1] + "0999",
            "link_type", "cites", "created_by", "etl"));
        assertThat(repo.linksFrom(TENANT, p[0], List.of("cites"))).hasSize(1);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-ekaxn — alias following
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void ekaxn_getDocumentFollowsAliasWhenAsked() {
        String owner = freshOwner();
        String canonical = repo.registerDocument(TENANT, owner, Map.of(
            "title", "canonical", "content_type", "code",
            "source_uri", "file:///defects70/alias/canonical.md"));
        String alias = repo.registerDocument(TENANT, owner, Map.of(
            "title", "alias", "content_type", "code",
            "source_uri", "file:///defects70/alias/alias.md"));
        repo.setAlias(TENANT, alias, canonical);

        // Backward compatible: no follow => today's behaviour, the alias row.
        assertThat(repo.getDocument(TENANT, alias).get("tumbler")).isEqualTo(alias);
        assertThat(repo.getDocument(TENANT, alias, true).get("tumbler"))
            .as("follow_alias must hop to the canonical target")
            .isEqualTo(canonical);
    }

    @Test
    void ekaxn_followAliasWalksAChain() {
        String owner = freshOwner();
        String c = repo.registerDocument(TENANT, owner, Map.of(
            "title", "chain-c", "content_type", "code",
            "source_uri", "file:///defects70/aliaschain/c.md"));
        String b = repo.registerDocument(TENANT, owner, Map.of(
            "title", "chain-b", "content_type", "code",
            "source_uri", "file:///defects70/aliaschain/b.md"));
        String a = repo.registerDocument(TENANT, owner, Map.of(
            "title", "chain-a", "content_type", "code",
            "source_uri", "file:///defects70/aliaschain/a.md"));
        repo.setAlias(TENANT, b, c);
        repo.setAlias(TENANT, a, b);

        assertThat(repo.getDocument(TENANT, a, true).get("tumbler")).isEqualTo(c);
    }

    @Test
    void ekaxn_followAliasIsCycleSafe() {
        String owner = freshOwner();
        String a = repo.registerDocument(TENANT, owner, Map.of(
            "title", "cycle-a", "content_type", "code",
            "source_uri", "file:///defects70/aliascycle/a.md"));
        String b = repo.registerDocument(TENANT, owner, Map.of(
            "title", "cycle-b", "content_type", "code",
            "source_uri", "file:///defects70/aliascycle/b.md"));
        repo.setAlias(TENANT, a, b);
        repo.setAlias(TENANT, b, a);

        // Terminates, returns a real row rather than spinning or throwing.
        var seen = repo.getDocument(TENANT, a, true);
        assertThat(seen).isNotNull();
        assertThat(seen.get("tumbler")).isIn(a, b);
    }

    @Test
    void ekaxn_danglingAliasFallsBackToLastLiveRow() {
        String owner = freshOwner();
        String a = repo.registerDocument(TENANT, owner, Map.of(
            "title", "dangle-alias", "content_type", "code",
            "source_uri", "file:///defects70/aliasdangle/a.md"));
        repo.setAlias(TENANT, a, owner + ".424242");

        var seen = repo.getDocument(TENANT, a, true);
        assertThat(seen)
            .as("an alias pointing at nothing must resolve to the alias row, not null")
            .isNotNull();
        assertThat(seen.get("tumbler")).isEqualTo(a);
    }

    @Test
    void ekaxn_updateThroughAliasLandsOnCanonical() {
        String owner = freshOwner();
        String canonical = repo.registerDocument(TENANT, owner, Map.of(
            "title", "upd-canonical", "content_type", "code", "chunk_count", 0,
            "source_uri", "file:///defects70/aliasupd/canonical.md"));
        String alias = repo.registerDocument(TENANT, owner, Map.of(
            "title", "upd-alias", "content_type", "code", "chunk_count", 0,
            "source_uri", "file:///defects70/aliasupd/alias.md"));
        repo.setAlias(TENANT, alias, canonical);

        assertThat(repo.updateDocument(TENANT, alias, Map.of("head_hash", "through-alias")))
            .isEqualTo(1);

        assertThat(repo.getDocument(TENANT, canonical).get("head_hash"))
            .as("an update addressed to an alias must land on the CANONICAL row")
            .isEqualTo("through-alias");
        assertThat(repo.getDocument(TENANT, alias).get("head_hash")).isEqualTo("");
    }

    /** The alias-pointer write itself must NOT hop, or re-pointing an alias
     *  would corrupt its canonical target. */
    @Test
    void ekaxn_aliasOfWriteDoesNotHop() {
        String owner = freshOwner();
        String first = repo.registerDocument(TENANT, owner, Map.of(
            "title", "repoint-first", "content_type", "code",
            "source_uri", "file:///defects70/aliasrepoint/first.md"));
        String second = repo.registerDocument(TENANT, owner, Map.of(
            "title", "repoint-second", "content_type", "code",
            "source_uri", "file:///defects70/aliasrepoint/second.md"));
        String alias = repo.registerDocument(TENANT, owner, Map.of(
            "title", "repoint-alias", "content_type", "code",
            "source_uri", "file:///defects70/aliasrepoint/alias.md"));
        repo.setAlias(TENANT, alias, first);

        repo.updateDocument(TENANT, alias, Map.of("alias_of", second));

        assertThat(repo.getDocument(TENANT, alias).get("alias_of")).isEqualTo(second);
        assertThat(repo.getDocument(TENANT, first).get("alias_of")).isEqualTo("");
    }

    /** The batch path resolves aliases too — and does it with ONE query for the
     *  whole batch (batchAliasTargets), not one per document. */
    @Test
    void ekaxn_updateManyThroughAliasLandsOnCanonical() {
        String owner = freshOwner();
        String canonical = repo.registerDocument(TENANT, owner, Map.of(
            "title", "batch-canonical", "content_type", "code",
            "source_uri", "file:///defects70/aliasbatch/canonical.md"));
        String alias = repo.registerDocument(TENANT, owner, Map.of(
            "title", "batch-alias", "content_type", "code",
            "source_uri", "file:///defects70/aliasbatch/alias.md"));
        String plain = repo.registerDocument(TENANT, owner, Map.of(
            "title", "batch-plain", "content_type", "code",
            "source_uri", "file:///defects70/aliasbatch/plain.md"));
        repo.setAlias(TENANT, alias, canonical);

        var results = repo.updateDocumentsMany(TENANT, List.of(
            Map.of("tumbler", alias, "head_hash", "via-alias"),
            Map.of("tumbler", plain, "head_hash", "direct")));
        assertThat(results).containsExactly(1, 1);

        assertThat(repo.getDocument(TENANT, canonical).get("head_hash")).isEqualTo("via-alias");
        assertThat(repo.getDocument(TENANT, alias).get("head_hash")).isEqualTo("");
        assertThat(repo.getDocument(TENANT, plain).get("head_hash"))
            .as("a non-aliased entry in the same batch is unaffected")
            .isEqualTo("direct");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-jqvzk — gc-audit surface for destructive T3 operations
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void jqvzk_recordsAndReadsBackADestructiveOp() {
        String coll = "code__defects70-gcaudit__voyage-code-3__v1";
        long id = repo.recordGcAudit(TENANT, Map.of(
            "operation", "t3_gc",
            "collection", coll,
            "actor", "nx t3 gc",
            "dry_run", false,
            "chashes", List.of(ch("gcaudit-a"), ch("gcaudit-b")),
            "details", Map.of("orphan_window_days", 30)));
        assertThat(id).isPositive();

        var entries = repo.listGcAudit(TENANT, coll, null, 100, 0);
        assertThat(entries).hasSize(1);
        var e = entries.get(0);
        assertThat(e.get("operation")).isEqualTo("t3_gc");
        assertThat(e.get("collection")).isEqualTo(coll);
        assertThat(e.get("actor")).isEqualTo("nx t3 gc");
        assertThat(e.get("dry_run")).isEqualTo(false);
        assertThat(e.get("chash_count")).isEqualTo(2);
        assertThat(strings(e.get("chashes")))
            .containsExactly(ch("gcaudit-a"), ch("gcaudit-b"));
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) e.get("details");
        assertThat(details).containsEntry("orphan_window_days", 30);
        assertThat(e.get("created_at")).isNotNull();
    }

    @Test
    void jqvzk_dryRunIsRecordedSeparately() {
        String coll = "code__defects70-gcaudit-dry__voyage-code-3__v1";
        repo.recordGcAudit(TENANT, Map.of(
            "operation", "t3_gc", "collection", coll, "dry_run", true,
            "chashes", List.of(ch("gcaudit-dry"))));
        repo.recordGcAudit(TENANT, Map.of(
            "operation", "t3_gc", "collection", coll, "dry_run", false,
            "chashes", List.of(ch("gcaudit-dry"))));

        var entries = repo.listGcAudit(TENANT, coll, null, 100, 0);
        assertThat(entries).hasSize(2);
        // Newest first — the destructive run follows its own preview.
        assertThat(entries.get(0).get("dry_run")).isEqualTo(false);
        assertThat(entries.get(1).get("dry_run")).isEqualTo(true);
    }

    @Test
    void jqvzk_operationIsRequired() {
        assertThrows(IllegalArgumentException.class, () ->
            repo.recordGcAudit(TENANT, Map.of("collection", "x")));
    }

    @Test
    void jqvzk_hugeChashListIsTruncatedButCountIsExact() {
        String coll = "code__defects70-gcaudit-big__voyage-code-3__v1";
        int n = CatalogRepositoryGcAuditProbe.MAX_CHASHES + 25;
        List<String> many = new java.util.ArrayList<>(n);
        for (int i = 0; i < n; i++) many.add(ch("gcaudit-big-" + i));

        repo.recordGcAudit(TENANT, Map.of(
            "operation", "t3_gc", "collection", coll, "chashes", many));

        var e = repo.listGcAudit(TENANT, coll, null, 10, 0).get(0);
        assertThat(e.get("chash_count"))
            .as("count must stay EXACT even when the stored list is capped")
            .isEqualTo(n);
        assertThat((List<?>) e.get("chashes")).hasSize(CatalogRepositoryGcAuditProbe.MAX_CHASHES);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) e.get("details");
        assertThat(details)
            .as("truncation must be REPORTED, never a silently short list")
            .containsEntry("chashes_truncated", true);
    }

    /** Package-visible mirror of the repository's cap so the test asserts the
     *  real constant rather than a re-typed literal that can drift. */
    private static final class CatalogRepositoryGcAuditProbe {
        static final int MAX_CHASHES = dev.nexus.service.db.CatalogRepository.GC_AUDIT_MAX_CHASHES;
        private CatalogRepositoryGcAuditProbe() {}
    }

    @Test
    void jqvzk_operationFilterNarrows() {
        String coll = "code__defects70-gcaudit-filter__voyage-code-3__v1";
        repo.recordGcAudit(TENANT, Map.of("operation", "t3_gc", "collection", coll));
        repo.recordGcAudit(TENANT, Map.of("operation", "collection_delete", "collection", coll));

        assertThat(repo.listGcAudit(TENANT, coll, "t3_gc", 100, 0)).hasSize(1);
        assertThat(repo.listGcAudit(TENANT, coll, "collection_delete", 100, 0)).hasSize(1);
        assertThat(repo.listGcAudit(TENANT, coll, null, 100, 0)).hasSize(2);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-a3kbf — `nx catalog reconcile` damage classes, fault-injected
    //
    // The Python coverage (tests/test_catalog_reconcile.py, 15 tests) died with
    // the local catalog because every one of them seeded DAMAGED catalog state
    // via raw SQL with caller-pinned tumblers, and the service substrate exposes
    // no public API that can create that damage — by design: writeManifestRows,
    // appendManifestChunks and purgeManifest all fold chunk_count, so no public
    // call can produce the count/manifest DISAGREEMENT the verb exists to repair.
    //
    // These construct the damaged rows DIRECTLY in PG (superuser connection,
    // test scope only) and then exercise the engine reads the verb consumes:
    // all_documents -> listDocuments, get_manifest(s), atomic replace,
    // resyncChunkCount. They pin the reads' FIDELITY to the damage — a
    // reconcile that cannot see the gap cannot repair it.
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Seed a document row bypassing every count-folding write path.
     * Superuser connection: RLS is FORCE'd for the svc role, and the point of
     * this helper is to write states the service role's own code paths refuse
     * to produce.
     */
    private void injectDamagedDoc(String tumbler, String collection, int chunkCount,
                                  String metadataJson) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, content_type, physical_collection, "
                + " chunk_count, metadata, file_path, corpus, head_hash, indexed_at, source_uri) "
                + "VALUES (?, ?, ?, 'code', ?, ?, CAST(? AS jsonb), '', '', '', '', ?)")) {
                ps.setString(1, TENANT);
                ps.setString(2, tumbler);
                ps.setString(3, "damaged-" + tumbler);
                ps.setString(4, collection);
                ps.setInt(5, chunkCount);
                ps.setString(6, metadataJson);
                ps.setString(7, "file:///defects70/damaged/" + tumbler + ".md");
                ps.executeUpdate();
            }
        }
    }

    /**
     * Seed one row in {@code nexus.chunks} (RDR-191 unified; formerly {@code
     * chunks_1024}) so {@code resolveChash} has a chunk to find (it returns
     * null before ever reaching the doc_id lookup otherwise). Mirrors the
     * seeding shape used by ManifestCollectionStampTest.
     */
    private void seedChunkRow(String collection, String chash, String text) throws Exception {
        // nexus.chunks carries an FK to catalog_collections on (tenant_id, collection).
        repo.upsertCollection(TENANT, Map.of(
            "name", collection, "content_type", "code",
            "embedding_model", "voyage-code-3", "model_version", "v1"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var st = su.createStatement()) {
                st.execute(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024) "
                    + "VALUES ('" + TENANT + "', '" + collection + "', decode('" + chash + "', 'hex'), '"
                    + text.replace("'", "''") + "', "
                    + "('[' || repeat('0.1,', 1023) || '0.1]')::vector) "
                    + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
            }
        }
    }

    /**
     * Force ``chunk_count`` to a value that DISAGREES with the manifest, via a
     * direct superuser write that bypasses both fold paths.
     *
     * <p>Review L1: without this, the update-path re-derivation tests were
     * confounded by e4gel's OTHER half — {@code appendManifestChunks} now folds
     * the count itself, so the count was already correct before
     * {@code updateDocument} ran and reverting the update-path fix alone left
     * those tests passing. Seeding a stale count is the only way to falsify the
     * update path independently of the append path.
     */
    private void forceStaleChunkCount(String tumbler, int staleCount) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "UPDATE nexus.catalog_documents SET chunk_count = ? "
                + "WHERE tenant_id = ? AND tumbler = ?")) {
                ps.setInt(1, staleCount);
                ps.setString(2, TENANT);
                ps.setString(3, tumbler);
                int n = ps.executeUpdate();
                if (n != 1) {
                    throw new IllegalStateException(
                        "forceStaleChunkCount seeded " + n + " rows for " + tumbler + ", expected 1");
                }
            }
        }
    }

    /** GH #1371: chunk_count > 0 with an EMPTY manifest — the original gap. */
    @Test
    void a3kbf_countWithoutManifestIsVisibleToTheReconcileReads() throws Exception {
        String tumbler = "9001.1";
        String coll = "code__defects70-recon1__voyage-code-3__v1";
        injectDamagedDoc(tumbler, coll, 7, "{\"content_hash\":\"deadbeef\"}");

        var doc = repo.getDocument(TENANT, tumbler);
        assertThat(doc).isNotNull();
        assertThat(doc.get("chunk_count"))
            .as("the verb classifies off chunk_count; the read must report the DAMAGED value")
            .isEqualTo(7);
        assertThat(repo.getManifest(TENANT, tumbler))
            .as("...against an empty manifest — that disagreement IS the gap")
            .isEmpty();
        assertThat(repo.getManifestMany(TENANT, List.of(tumbler)))
            .as("batch read omits a doc with no manifest rows rather than erroring")
            .doesNotContainKey(tumbler);

        // The repair path the verb drives.
        writeManifestSeeded(TENANT, tumbler, coll, List.of(
            row(0, ch("recon1-0")), row(1, ch("recon1-1"))));
        assertThat(repo.getManifest(TENANT, tumbler)).hasSize(2);
        assertThat(repo.getDocument(TENANT, tumbler).get("chunk_count"))
            .as("the atomic replace folds the count back into agreement")
            .isEqualTo(2);
    }

    /** GH #1397 (nexus-94fxl): the chunk_count == 0 ghost. The verb must be able
     *  to tell a rebuildable ghost (content_hash recorded) from a register-only
     *  one, so the metadata has to survive the read intact. */
    @Test
    void a3kbf_zeroCountGhostKeepsTheMetadataTheVerbClassifiesOn() throws Exception {
        String rebuildable = "9002.1";
        String registerOnly = "9002.2";
        String coll = "code__defects70-recon2__voyage-code-3__v1";
        injectDamagedDoc(rebuildable, coll, 0, "{\"content_hash\":\"cafebabe\"}");
        injectDamagedDoc(registerOnly, coll, 0, "{}");

        var withHash = repo.getDocument(TENANT, rebuildable);
        @SuppressWarnings("unchecked")
        var meta = (Map<String, Object>) withHash.get("metadata");
        assertThat(meta).containsEntry("content_hash", "cafebabe");
        assertThat(withHash.get("physical_collection")).isEqualTo(coll);

        var withoutHash = repo.getDocument(TENANT, registerOnly);
        @SuppressWarnings("unchecked")
        var bare = (Map<String, Object>) withoutHash.get("metadata");
        // Empty jsonb may surface as an empty map or as null; either way the
        // classifying key must be ABSENT — that absence is what distinguishes a
        // register-only ghost from a rebuildable one.
        assertThat(bare == null ? Map.<String, Object>of() : bare)
            .as("a register-only ghost must be DISTINGUISHABLE, not merely absent")
            .doesNotContainKey("content_hash");

        // Both are reachable through the collection scan the verb walks.
        assertThat(repo.documentsByCollection(TENANT, coll, 0, 0))
            .extracting(d -> d.get("tumbler"))
            .contains(rebuildable, registerOnly);
    }

    /** nexus-8g0ch: batching + paging. all_documents(limit=0) pages the engine;
     *  the pages must partition the set — no dupes, no drops at a boundary. */
    @Test
    void a3kbf_listDocumentsPagesWithoutDroppingOrDuplicating() throws Exception {
        String coll = "code__defects70-recon3__voyage-code-3__v1";
        for (int i = 1; i <= 70; i++) {
            injectDamagedDoc("9003." + i, coll, i % 3, "{}");
        }

        var all = repo.documentsByCollection(TENANT, coll, 0, 0);
        assertThat(all).hasSize(70);

        java.util.Set<Object> paged = new java.util.LinkedHashSet<>();
        int seen = 0;
        for (int offset = 0; ; offset += 64) {
            var page = repo.listDocuments(TENANT, 64, offset);
            if (page.isEmpty()) break;
            for (var d : page) {
                if (coll.equals(d.get("physical_collection"))) {
                    assertThat(paged.add(d.get("tumbler")))
                        .as("a tumbler must not appear on two pages")
                        .isTrue();
                    seen++;
                }
            }
            if (page.size() < 64) break;
        }
        assertThat(seen).as("paging must cover the whole damaged set").isEqualTo(70);
    }

    /** Batch-failure containment: one bad doc_id must not take its siblings
     *  down — the verb reconciles document-at-a-time over a batched wire. */
    @Test
    void a3kbf_writeManifestManyContainsPerDocFailure() throws Exception {
        String coll = "code__defects70-recon4__voyage-code-3__v1";
        injectDamagedDoc("9004.1", coll, 5, "{}");
        injectDamagedDoc("9004.2", coll, 5, "{}");

        var result = writeManifestManySeeded(TENANT, List.of(
            Map.of("doc_id", "9004.1", "rows", List.of(row(0, ch("recon4-a")))),
            // No such document — the FK on catalog_document_chunks rejects it.
            Map.of("doc_id", "9004.404", "rows", List.of(row(0, ch("recon4-x")))),
            Map.of("doc_id", "9004.2", "rows", List.of(row(0, ch("recon4-b"))))), coll);

        @SuppressWarnings("unchecked")
        var failed = (List<String>) result.get("failed_doc_ids");
        assertThat(failed).containsExactly("9004.404");
        assertThat(result.get("docs")).isEqualTo(2);
        assertThat(repo.getManifest(TENANT, "9004.1")).hasSize(1);
        assertThat(repo.getManifest(TENANT, "9004.2"))
            .as("a doc AFTER the failing one must still be written — containment, not abort")
            .hasSize(1);
    }

    /** UNAVAILABLE-collection continuation: a document whose physical_collection
     *  holds no chunks at all is a REPORT, not an error — the verb must be able
     *  to walk past it to the next document. */
    @Test
    void a3kbf_documentInEmptyCollectionStillReadsAndResyncs() throws Exception {
        String coll = "code__defects70-recon5-unavailable__voyage-code-3__v1";
        injectDamagedDoc("9005.1", coll, 4, "{\"content_hash\":\"feedface\"}");

        assertThat(repo.chashesForCollection(TENANT, coll))
            .as("nothing was ever indexed into this collection")
            .isEmpty();
        assertThat(repo.getManifest(TENANT, "9005.1")).isEmpty();

        // resync is the honest repair when T3 has nothing to rebuild from:
        // it makes the count match the (empty) manifest instead of leaving a lie.
        assertThat(repo.resyncChunkCount(TENANT, "9005.1")).isEqualTo(1);
        assertThat(repo.getDocument(TENANT, "9005.1").get("chunk_count")).isEqualTo(0);
    }

    /** Rename cascade count-passthrough: moving a damaged document between
     *  collections must not silently "fix" or further corrupt its count. */
    @Test
    void a3kbf_collectionMovePreservesTheDamagedCount() throws Exception {
        String from = "code__defects70-recon6-a__voyage-code-3__v1";
        String to   = "code__defects70-recon6-b__voyage-code-3__v1";
        injectDamagedDoc("9006.1", from, 11, "{}");

        assertThat(repo.updateDocumentCollection(TENANT, "9006.1", to)).isEqualTo(1);

        var doc = repo.getDocument(TENANT, "9006.1");
        assertThat(doc.get("physical_collection")).isEqualTo(to);
        assertThat(doc.get("chunk_count"))
            .as("a collection move is not a repair — the gap must survive it, visibly")
            .isEqualTo(11);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-4j80w — link created_at stamping + non-empty guard on before-filters
    //
    // Port-parity sweep D1: the client never sends created_at, and upsertLink's
    // nne() default of '' sorted before EVERY real timestamp, so
    // queryLinks/bulkDeleteLinks's created_at_before predicate matched every
    // service-written link (bulk_unlink deleted the whole graph). Fixed both
    // legs: upsertLink stamps a real ISO-8601 UTC timestamp when absent, and
    // every created_at_before predicate gained a non-empty guard.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void j80w_upsertLinkStampsRealTimestampWhenCreatedAtAbsent() {
        var p = linkPair("j80w-stamp");
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "j80w-test"));

        Object createdAt = onlyEdge(p[0], p[1], "cites").get("created_at");
        assertThat(createdAt)
            .as("a new link must not be stamped with an empty created_at")
            .isNotNull().isNotEqualTo("");
        // must parse as a real ISO-8601 timestamp — throws if the default is malformed.
        java.time.OffsetDateTime.parse(String.valueOf(createdAt));
    }

    @Test
    void j80w_upsertLinkPreservesCallerSuppliedCreatedAt() {
        var p = linkPair("j80w-caller-supplied");
        String explicit = "2000-01-01T00:00:00.000000+00:00";
        repo.upsertLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "j80w-test", "created_at", explicit));

        assertThat(onlyEdge(p[0], p[1], "cites").get("created_at")).isEqualTo(explicit);
    }

    @Test
    void j80w_emptyCreatedAtRowIsUnmatchableByBeforeFilter() {
        var p = linkPair("j80w-empty-unmatchable");
        // importLink is the ETL leg — out of scope for this fix — and still
        // writes '' when the caller supplies no created_at, giving a realistic
        // pre-fix-shaped row to probe the guard against.
        repo.importLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "j80w-test"));

        var results = repo.queryLinks(TENANT, p[0], null, null, null,
            "9999-12-31T00:00:00+00:00", 100, 0, "both", null);
        assertThat(results)
            .as("a '' created_at row must be UNMATCHABLE by a before-filter (fail-safe)")
            .isEmpty();

        int deleted = repo.bulkDeleteLinks(TENANT, p[0], null, null, null, "9999-12-31T00:00:00+00:00");
        assertThat(deleted).isZero();
        assertThat(repo.linksFrom(TENANT, p[0], List.of("cites"))).hasSize(1);
    }

    @Test
    void j80w_realTimestampRowStillMatchableByBeforeFilter() {
        var p = linkPair("j80w-real-matchable");
        repo.importLink(TENANT, Map.of(
            "from_tumbler", p[0], "to_tumbler", p[1], "link_type", "cites",
            "created_by", "j80w-test", "created_at", "2000-01-01T00:00:00+00:00"));

        var results = repo.queryLinks(TENANT, p[0], null, null, null,
            "9999-12-31T00:00:00+00:00", 100, 0, "both", null);
        assertThat(results)
            .as("a link with a real timestamp before the cutoff must still match")
            .hasSize(1);
    }

    @Test
    void j80w_bulkDeleteLinksWithBeforeFilterDeletesOnlyMatched() {
        String owner = freshOwner();
        String a = repo.registerDocument(TENANT, owner, Map.of(
            "title", "bulkdel-from", "content_type", "paper",
            "source_uri", "file:///defects70/j80w-bulkdel/from.md"));
        String past = repo.registerDocument(TENANT, owner, Map.of(
            "title", "bulkdel-past", "content_type", "paper",
            "source_uri", "file:///defects70/j80w-bulkdel/past.md"));
        String future = repo.registerDocument(TENANT, owner, Map.of(
            "title", "bulkdel-future", "content_type", "paper",
            "source_uri", "file:///defects70/j80w-bulkdel/future.md"));
        String blank = repo.registerDocument(TENANT, owner, Map.of(
            "title", "bulkdel-blank", "content_type", "paper",
            "source_uri", "file:///defects70/j80w-bulkdel/blank.md"));

        repo.importLink(TENANT, Map.of("from_tumbler", a, "to_tumbler", past,
            "link_type", "cites", "created_by", "j80w-test", "created_at", "2000-01-01T00:00:00+00:00"));
        repo.importLink(TENANT, Map.of("from_tumbler", a, "to_tumbler", future,
            "link_type", "cites", "created_by", "j80w-test", "created_at", "2999-01-01T00:00:00+00:00"));
        repo.importLink(TENANT, Map.of("from_tumbler", a, "to_tumbler", blank,
            "link_type", "cites", "created_by", "j80w-test"));

        int deleted = repo.bulkDeleteLinks(TENANT, a, null, null, null, "2500-01-01T00:00:00+00:00");
        assertThat(deleted).as("only the PAST-timestamped edge is before the cutoff").isEqualTo(1);

        var remaining = repo.linksFrom(TENANT, a, List.of("cites"));
        assertThat(remaining).hasSize(2);
        assertThat(remaining.stream().map(e -> e.get("to_tumbler")).toList())
            .as("the future-dated and blank-created_at edges must both survive")
            .containsExactlyInAnyOrder(future, blank);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-pzdol — collectionForTuple numeric version ordering
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void pzdol_collectionForTupleTakesMaxNumericVersionNotLexical() {
        String owner = freshOwner();
        String contentType = "docs";
        String embeddingModel = "voyage-context-3";
        for (int v : new int[]{1, 2, 9, 10}) {
            repo.upsertCollection(TENANT, Map.of(
                "name", contentType + "__" + owner + "__" + embeddingModel + "__v" + v,
                "content_type", contentType, "owner_id", owner,
                "embedding_model", embeddingModel, "model_version", "v" + v));
        }

        var found = repo.collectionForTuple(TENANT, contentType, owner, embeddingModel);
        assertThat(found).isNotNull();
        assertThat(found.get("model_version"))
            .as("v10 must win over v9 under NUMERIC ordering — lexical ordering picks v9")
            .isEqualTo("v10");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-h77a2 — lookupDocByCollectionAndPath title-probe + duplicate handling
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void h77a2_titleProbeResolvesDocRegisteredWithTitleEqualToPath() {
        String owner = freshOwner();
        String coll = "knowledge__defects70-h77a2-title__voyage-context-3__v1";
        String absPath = "/Users/somebody/git/nexus-clone/papers/foo.md";
        String t = repo.registerDocument(TENANT, owner, Map.of(
            "title", absPath, "content_type", "paper",
            "file_path", "papers/foo.md", "physical_collection", coll,
            "source_uri", "file:///defects70/h77a2/foo.md"));

        // file_path leg still resolves (unchanged).
        assertThat(repo.lookupDocByCollectionAndPath(TENANT, coll, "papers/foo.md")).isEqualTo(t);

        // title leg: file_path does NOT equal absPath, but title does.
        assertThat(repo.lookupDocByCollectionAndPath(TENANT, coll, absPath))
            .as("a doc registered with title == probed path must resolve via the title leg")
            .isEqualTo(t);
    }

    @Test
    void h77a2_missesCleanlyWhenNeitherLegMatches() {
        String owner = freshOwner();
        String coll = "knowledge__defects70-h77a2-miss__voyage-context-3__v1";
        repo.registerDocument(TENANT, owner, Map.of(
            "title", "papers/known.md", "content_type", "paper",
            "file_path", "papers/known.md", "physical_collection", coll,
            "source_uri", "file:///defects70/h77a2miss/known.md"));

        assertThat(repo.lookupDocByCollectionAndPath(
            TENANT, coll, "/Users/nobody/git/ghost/papers/stray.md")).isNull();
    }

    /** engine fetchOne() previously threw TooManyRowsException on a duplicate
     *  (collection, file_path); the local arm's LIMIT-1 quietly returned one row. */
    @Test
    void h77a2_duplicateCollectionAndPathReturnsDeterministicSingleResultNotException() {
        String owner1 = freshOwner();
        String owner2 = freshOwner();
        String coll = "knowledge__defects70-h77a2-dup__voyage-context-3__v1";
        String samePath = "papers/dup.md";
        String first = repo.registerDocument(TENANT, owner1, Map.of(
            "title", "dup-1", "content_type", "paper",
            "file_path", samePath, "physical_collection", coll,
            "source_uri", "file:///defects70/h77a2dup/dup1.md"));
        String second = repo.registerDocument(TENANT, owner2, Map.of(
            "title", "dup-2", "content_type", "paper",
            "file_path", samePath, "physical_collection", coll,
            "source_uri", "file:///defects70/h77a2dup/dup2.md"));
        assertThat(first).isNotEqualTo(second);

        String expectedWinner = first.compareTo(second) < 0 ? first : second;
        assertThat(repo.lookupDocByCollectionAndPath(TENANT, coll, samePath))
            .as("must not throw; must deterministically pick the lexically-lowest tumbler")
            .isEqualTo(expectedWinner);
    }
}
