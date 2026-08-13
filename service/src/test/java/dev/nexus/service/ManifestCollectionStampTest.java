package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-x6kdz — the 6.5.0 live-shakeout finding: NO writer populated
 * {@code catalog_document_chunks.collection}, the combined-query join key
 * (catalog-006/-008/-012: {@code m.collection = c.collection}). The only
 * stamper was the migration-leg {@code manifest_backfill()}, and the REPLACE
 * manifest writers wiped its work on every re-index — so on any live tenant
 * every post-migration manifest row was invisible to the combined queries
 * (silent-empty; the app-side {@code query()} fallback masked it). The seeded
 * parity tests never caught it because they INSERT manifest rows with
 * {@code collection} set directly, bypassing the writers.
 *
 * <p>These tests walk the REAL writers — {@code writeManifest} (REPLACE),
 * {@code appendManifestChunks}, {@code importChunksBatch} (the catalog-ETL
 * path) — and assert both the stamped column AND the end-to-end property the
 * shakeout found broken: {@code search_metadata_scoped_1024} returns the row.
 *
 * <p><b>RDR-191 evolution (Hal ruling 2026-08-12, catalog-025-collection-
 * not-null.xml's header, nexus-j862l reconciliation):</b> an earlier
 * increment of this class asserted a per-row chash-membership resolution
 * step (a ghost/sourceless document's manifest write would VERIFY each
 * row's chash against real chunk content and resolve its collection from
 * that, with a sibling-tiebreak and a skip-on-ambiguous fallback). Hal's
 * final ruling REJECTED that design outright — {@code
 * catalog_document_chunks.collection} is NOT NULL with no sentinel and no
 * DEFAULT, and every writer below ({@link CatalogRepository#writeManifest}/
 * {@link CatalogRepository#appendManifestChunks}/{@link
 * CatalogRepository#importChunksBatch}) now takes a REQUIRED, caller-
 * supplied {@code collection} argument that is stamped on every row of a
 * call VERBATIM, with zero verification. The tests below assert THAT
 * contract; where a prior test's whole premise was the rejected resolution
 * mechanism, it has been re-based (see individual javadocs) rather than
 * deleted outright, so the underlying scenario (upsert-on-conflict,
 * multi-row batches, a chash shared across collections) still has coverage.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestCollectionStampTest {

    private static final String TENANT = "mcs-tenant";
    private static final String COLL   = "knowledge__mcs__voyage-context-3__v1";
    // Full 64-hex canonical chashes (RDR-180: chunks_*/manifest columns are bytea(32))
    private static final String CH_A   = "a".repeat(64);
    private static final String CH_B   = "b".repeat(64);

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource ds;
    CatalogRepository repo;
    String docTumbler;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
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
        ds = PgContainerHelper.superuserDataSource(pg);
        repo = new CatalogRepository(new TenantScope(ds));

        // One registered document with a physical_collection, plus a matching
        // 1024-dim chunk row (the combined query joins chunks ⋈ manifest ⋈ docs).
        docTumbler = repo.registerDocument(TENANT, "9.1", Map.of(
            "title", "stamp doc", "content_type", "knowledge",
            "physical_collection", COLL));
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + COLL + "') ON CONFLICT DO NOTHING");
            st.execute("INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ") "
                + "VALUES ('" + TENANT + "', '" + COLL + "', decode('" + CH_A + "', 'hex'), 'alpha text', "
                + "('[' || repeat('0.1,', 1023) || '0.1]')::vector)");
        }
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    /**
     * Plants a REAL nexus.chunks row (RDR-191 unified; formerly chunks_1024) so a test can seed genuine chunk
     * content under an arbitrary collection name (used by the multi-row
     * batch tests below to prove the engine does NOT consult where a
     * chash's content actually lives when stamping the manifest).
     */
    private void seedChunkContent(String coll, String chash, String text) throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + coll + "') ON CONFLICT DO NOTHING");
            st.execute("INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ") "
                + "VALUES ('" + TENANT + "', '" + coll + "', decode('" + chash + "', 'hex'), '"
                + text + "', ('[' || repeat('0.1,', 1023) || '0.1]')::vector) "
                + "ON CONFLICT DO NOTHING");
        }
    }

    private String collectionOf(String chash) throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT collection FROM nexus.catalog_document_chunks "
                 + "WHERE tenant_id = '" + TENANT + "' AND chash = decode('" + chash + "', 'hex')")) {
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private int combinedQueryHits() throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT count(*) FROM nexus.search_metadata_scoped_1024("
                 + "('[' || repeat('0.1,', 1023) || '0.1]')::vector, "
                 + "ARRAY['" + COLL + "'], NULL::text, NULL::text, NULL::int, "
                 + "NULL::text, NULL::text, NULL::jsonb, 10)")) {
            rs.next();
            return rs.getInt(1);
        }
    }

    @Test
    void writeManifest_stampsCollection_andCombinedQuerySeesTheRow() throws Exception {
        repo.writeManifest(TENANT, docTumbler, COLL, List.of(
            Map.of("position", 0, "chash", CH_A)));
        assertThat(collectionOf(CH_A))
            .as("REPLACE writer stamps the caller-supplied collection")
            .isEqualTo(COLL);
        assertThat(combinedQueryHits())
            .as("the exact end-to-end property the live shakeout found broken: "
                + "a serving-written manifest row is visible to the combined query")
            .isEqualTo(1);

        // REPLACE again (re-index) — the stamp must SURVIVE, not be wiped
        // (pre-fix, re-indexing was what erased manifest_backfill's work).
        repo.writeManifest(TENANT, docTumbler, COLL, List.of(
            Map.of("position", 0, "chash", CH_A)));
        assertThat(collectionOf(CH_A)).isEqualTo(COLL);
        assertThat(combinedQueryHits()).isEqualTo(1);
    }

    @Test
    void appendAndImport_stampCollection() throws Exception {
        repo.appendManifestChunks(TENANT, docTumbler, COLL, List.of(
            Map.of("position", 1, "chash", CH_B)));
        assertThat(collectionOf(CH_B))
            .as("append writer stamps the caller-supplied collection too")
            .isEqualTo(COLL);

        // The catalog-ETL path (the shape every migrated tenant's rows took).
        repo.importChunksBatch(TENANT, docTumbler, COLL, List.of(
            Map.of("position", 2, "chash", "c".repeat(64))));
        assertThat(collectionOf("c".repeat(64)))
            .as("ETL import stamps too — migrated tenants stop regressing")
            .isEqualTo(COLL);
    }

    // Deliberately NOT `COLL` — the tests below leave rows permanently
    // stamped with a real collection outside the shared class fixture's
    // rename/count-scoped collection (renameCollection_reHomesManifestRows
    // asserts an EXACT row count re-homed by `WHERE collection = COLL`). A
    // leaked extra `COLL`-stamped row from these tests would inflate that
    // sibling test's count — isolate instead.
    private static final String COLL_F12C_GHOST = "knowledge__mcs-f12c-ghost__voyage-context-3__v1";

    /**
     * nexus-o8dil.4 / RDR-191 (Hal ruling 2026-08-12, nexus-j862l
     * reconciliation): the ORIGINAL nexus-o8dil.4 fix taught {@code
     * insertManifestChunkRows}' UPSERT_APPEND arm to COALESCE an incoming
     * NULL collection against the existing stamped value, so a
     * collection-less re-run could never demote a good stamp to NULL. The
     * final ruling made that scenario structurally unreachable instead: a
     * blank/null {@code collection} is rejected by {@code requireNonBlank}
     * before any row is written, for every caller, so there is no more
     * "collection-less source" to guard against — {@code
     * insertManifestChunkRows} now does a plain, unconditional {@code SET
     * collection = EXCLUDED.collection} (see its own RDR-191 comment).
     * What replaces the demotion-guard as the meaningful property to
     * test: an upsert for the SAME {@code (doc, position)} with a
     * DIFFERENT caller-supplied collection really does overwrite the
     * stamp — the plain SET is honored, not silently skipped or ignored.
     */
    @Test
    void appendManifestChunks_upsert_overwritesStampedCollectionOnConflict() throws Exception {
        String doc = repo.registerDocument(TENANT, "9.2", Map.of(
            "title", "f12c append doc", "content_type", "knowledge"));
        String ch = "e".repeat(64);
        repo.appendManifestChunks(TENANT, doc, COLL_F12C_GHOST, List.of(Map.of("position", 50, "chash", ch)));
        assertThat(collectionOf(ch))
            .as("first append: stamps the caller-supplied collection")
            .isEqualTo(COLL_F12C_GHOST);

        String reclassified = "knowledge__mcs-f12c-reclassified__voyage-context-3__v1";
        // A later append upsert for the SAME (doc, position) — e.g. a
        // reclassification re-run — with a DIFFERENT caller-supplied
        // collection must OVERWRITE the stamp, not preserve the old one.
        repo.appendManifestChunks(TENANT, doc, reclassified, List.of(Map.of("position", 50, "chash", ch)));
        assertThat(collectionOf(ch))
            .as("RDR-191: the plain SET on conflict honors the new "
                + "caller-supplied collection unconditionally")
            .isEqualTo(reclassified);
    }

    /** Same producer, the {@code importChunksBatch}/UPSERT_IMPORT arm
     *  (a SEPARATE code path from the append arm above — different SET
     *  clauses), covered independently per the original bead's acceptance
     *  criterion "do not assume they share a path". Same RDR-191 rebase as
     *  the append-arm test above. */
    @Test
    void importChunksBatch_upsert_overwritesStampedCollectionOnConflict() throws Exception {
        String doc = repo.registerDocument(TENANT, "9.3", Map.of(
            "title", "f12c import doc", "content_type", "knowledge"));
        String ch = "f".repeat(64);
        repo.importChunksBatch(TENANT, doc, COLL_F12C_GHOST, List.of(Map.of("position", 60, "chash", ch)));
        assertThat(collectionOf(ch))
            .as("first import: stamps the caller-supplied collection")
            .isEqualTo(COLL_F12C_GHOST);

        String reclassified = "knowledge__mcs-f12c-import-reclassified__voyage-context-3__v1";
        repo.importChunksBatch(TENANT, doc, reclassified, List.of(Map.of("position", 60, "chash", ch)));
        assertThat(collectionOf(ch))
            .as("RDR-191: the import-arm upsert must also honor a new "
                + "caller-supplied collection on conflict")
            .isEqualTo(reclassified);
    }

    /**
     * REPLACE re-stamps with whatever collection THIS call supplies —
     * there is no "resolve from the doc's prior stamped manifest" step
     * anymore (RDR-191 removed it entirely, catalog-025-collection-not-
     * null.xml's header). A REPLACE for a doc whose existing manifest was
     * stamped under one collection, called again with a DIFFERENT
     * collection, must land the new row under the NEW collection, and
     * chunk_count must fold the real written count, not a stale value
     * from a resolution step that no longer runs.
     */
    @Test
    void writeManifest_replace_restampsWithCallerSuppliedCollection_notThePriorStamp() throws Exception {
        String doc = repo.registerDocument(TENANT, "9.4", Map.of(
            "title", "case2 replace doc", "content_type", "knowledge"));
        String chA = "1".repeat(64);
        repo.writeManifest(TENANT, doc, COLL_F12C_GHOST, List.of(Map.of("position", 0, "chash", chA)));
        assertThat(collectionOf(chA))
            .as("first write: stamps the caller-supplied collection")
            .isEqualTo(COLL_F12C_GHOST);

        String reclassified = "knowledge__mcs-g4-reclassified__voyage-context-3__v1";
        String chB = "2".repeat(64);
        repo.writeManifest(TENANT, doc, reclassified, List.of(Map.of("position", 0, "chash", chB)));
        assertThat(collectionOf(chB))
            .as("RDR-191: REPLACE stamps whatever collection THIS call "
                + "supplies, never the doc's own prior stamp")
            .isEqualTo(reclassified);
        assertThat(repo.getManifest(TENANT, doc)).hasSize(1);
        assertThat(((Number) repo.getDocument(TENANT, doc).get("chunk_count")).intValue())
            .as("chunk_count must reflect the REAL manifest (manifestRowCount)")
            .isEqualTo(1);
    }

    /**
     * Proves a later upsert for the SAME position is a real UPDATE, not a
     * skip: varying BOTH the chash and the caller-supplied collection
     * across the two calls shows the row's chash, and its collection,
     * both move to the new call's values — the OLD chash's row is gone
     * entirely (same position, new chash: a real update, never a second
     * row left behind).
     */
    @Test
    void appendManifestChunks_upsert_refreshesChashAndCollectionOnSamePosition() throws Exception {
        String doc = repo.registerDocument(TENANT, "9.5", Map.of(
            "title", "case2 append refresh doc", "content_type", "knowledge"));
        String chOld = "3".repeat(64);
        repo.appendManifestChunks(TENANT, doc, COLL_F12C_GHOST, List.of(Map.of("position", 70, "chash", chOld)));
        assertThat(collectionOf(chOld)).isEqualTo(COLL_F12C_GHOST);

        String chNew = "4".repeat(64);
        String reclassified = "knowledge__mcs-g5-reclassified__voyage-context-3__v1";
        repo.appendManifestChunks(TENANT, doc, reclassified, List.of(Map.of("position", 70, "chash", chNew)));
        assertThat(collectionOf(chNew))
            .as("a later upsert must REFRESH both the chash and the collection")
            .isEqualTo(reclassified);
        assertThat(collectionOf(chOld))
            .as("the OLD chash's row must be gone -- proves this was a real "
                + "update (same position, new chash), not a second row left behind")
            .isNull();
    }

    /** Same refresh proof, the {@code importChunksBatch}/UPSERT_IMPORT arm —
     *  a separate code path from the append arm above, same discipline as
     *  the overwrite-on-conflict pair's own split. */
    @Test
    void importChunksBatch_upsert_refreshesChashAndCollectionOnSamePosition() throws Exception {
        String doc = repo.registerDocument(TENANT, "9.6", Map.of(
            "title", "case2 import refresh doc", "content_type", "knowledge"));
        String chOld = "5".repeat(64);
        repo.importChunksBatch(TENANT, doc, COLL_F12C_GHOST, List.of(Map.of("position", 80, "chash", chOld)));
        assertThat(collectionOf(chOld)).isEqualTo(COLL_F12C_GHOST);

        String chNew = "6".repeat(64);
        String reclassified = "knowledge__mcs-g6-reclassified__voyage-context-3__v1";
        repo.importChunksBatch(TENANT, doc, reclassified, List.of(Map.of("position", 80, "chash", chNew)));
        assertThat(collectionOf(chNew))
            .as("the import-arm upsert must also REFRESH both chash and collection")
            .isEqualTo(reclassified);
        assertThat(collectionOf(chOld)).isNull();
    }

    @Test
    void writeManifest_nonexistentDoc_throwsDocumentNotFoundException() {
        assertThatThrownBy(() -> repo.writeManifest(TENANT, "9.999-does-not-exist", COLL,
                List.of(Map.of("position", 0, "chash", "8".repeat(64)))))
            .isInstanceOf(CatalogRepository.DocumentNotFoundException.class);
    }

    /**
     * The original nexus-9kj5j "case 3" test asserted "no resolvable
     * collection anywhere -> row skipped, manifest stays truthfully
     * empty, chunk_count stays 0". RDR-191 removed the resolution step
     * that scenario depended on: {@code collection} is now a required,
     * caller-supplied argument, so "unresolvable" is no longer an input
     * the writer can even receive. {@code requireNonBlank} rejects a
     * blank/null one loudly, BEFORE any row is considered, rather than
     * silently skipping — that guard rail is the property worth testing
     * under the shipped contract.
     */
    @Test
    void writeManifest_blankOrNullCollection_rejectedLoudly_notSilentlySkipped() throws Exception {
        String doc = repo.registerDocument(TENANT, "9.7", Map.of(
            "title", "case3 doc", "content_type", "knowledge"));
        assertThatThrownBy(() -> repo.writeManifest(TENANT, doc, "", List.of(
                Map.of("position", 0, "chash", "9".repeat(64)))))
            .as("blank collection is rejected, not silently treated as unresolvable")
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> repo.writeManifest(TENANT, doc, null, List.of(
                Map.of("position", 0, "chash", "9".repeat(64)))))
            .as("null collection is rejected the same way")
            .isInstanceOf(IllegalArgumentException.class);
        assertThat(repo.getManifest(TENANT, doc))
            .as("a rejected write must not leave a partial manifest behind")
            .isEmpty();
    }

    // ── RDR-191 (Hal ruling 2026-08-12, nexus-j862l reconciliation): the
    // nexus-zqlmo six tests originally here asserted a per-row
    // chash-membership resolution CTE — each row of a heterogeneous batch
    // independently verified against real chunk content, with a
    // sibling-tiebreak and a skip-on-ambiguous/unresolved fallback when
    // verification disagreed or found nothing. Hal's final ruling REJECTED
    // that whole mechanism (catalog-025-collection-not-null.xml's header,
    // verbatim: "the rev-2 per-row C-membership + sibling-tiebreak
    // resolution CTE" is one of the two rejected designs) in favor of
    // writers sending the collection and the engine stamping it verbatim,
    // with zero verification. Under that contract a single write call has
    // exactly ONE collection for every row it contains, by construction —
    // there is no more "heterogeneous batch" or "partial resolution" for
    // these methods to get right or wrong, so the six paired (a)/(b) tests
    // collapse into ONE test per writer below: a multi-row batch is
    // stamped with the caller-supplied collection UNCONDITIONALLY, even
    // for a chash whose real content verifiably lives somewhere else
    // entirely — proving the engine performs NO membership verification,
    // the direct opposite of the rejected design.
    // ══════════════════════════════════════════════════════════════════════

    @Test
    void writeManifest_multiRowBatch_allRowsStampedWithCallerSuppliedCollection_noMembershipVerification()
            throws Exception {
        String collX = "knowledge__mcs-g10x__voyage-context-3__v1";
        String collY = "knowledge__mcs-g10y__voyage-context-3__v1";
        String chInX = "9".repeat(63) + "2";
        String chInY = "9".repeat(63) + "3";
        seedChunkContent(collX, chInX, "g10 content in X");
        seedChunkContent(collY, chInY, "g10 content in Y");

        String doc = repo.registerDocument(TENANT, "9.10", Map.of(
            "title", "g10 heterogeneous doc", "content_type", "knowledge"));
        repo.writeManifest(TENANT, doc, collX, List.of(
            Map.of("position", 1, "chash", chInX),
            Map.of("position", 2, "chash", chInY)));

        assertThat(collectionOf(chInX)).isEqualTo(collX);
        assertThat(collectionOf(chInY))
            .as("RDR-191: chInY's content genuinely lives in collY, but this "
                + "call supplied collX for the WHOLE batch -- the engine "
                + "performs no per-row verification and stamps every row "
                + "with exactly what the caller sent")
            .isEqualTo(collX);
    }

    @Test
    void appendManifestChunks_multiRowBatch_allRowsStampedWithCallerSuppliedCollection_noMembershipVerification()
            throws Exception {
        String collX = "knowledge__mcs-g12x__voyage-context-3__v1";
        String collY = "knowledge__mcs-g12y__voyage-context-3__v1";
        String chInX = "c".repeat(63) + "2";
        String chInY = "c".repeat(63) + "3";
        seedChunkContent(collX, chInX, "g12 content in X");
        seedChunkContent(collY, chInY, "g12 content in Y");

        String doc = repo.registerDocument(TENANT, "9.12", Map.of(
            "title", "g12 heterogeneous doc", "content_type", "knowledge"));
        repo.appendManifestChunks(TENANT, doc, collX, List.of(
            Map.of("position", 1, "chash", chInX),
            Map.of("position", 2, "chash", chInY)));

        assertThat(collectionOf(chInX)).isEqualTo(collX);
        assertThat(collectionOf(chInY))
            .as("RDR-191: append also stamps every row with the caller's "
                + "single collection, no per-row verification")
            .isEqualTo(collX);
    }

    @Test
    void importChunksBatch_multiRowBatch_allRowsStampedWithCallerSuppliedCollection_noMembershipVerification()
            throws Exception {
        String collX = "knowledge__mcs-g14x__voyage-context-3__v1";
        String collY = "knowledge__mcs-g14y__voyage-context-3__v1";
        String chInX = "1".repeat(63) + "8";
        String chInY = "1".repeat(63) + "9";
        seedChunkContent(collX, chInX, "g14 content in X");
        seedChunkContent(collY, chInY, "g14 content in Y");

        String doc = repo.registerDocument(TENANT, "9.14", Map.of(
            "title", "g14 heterogeneous doc", "content_type", "knowledge"));
        repo.importChunksBatch(TENANT, doc, collX, List.of(
            Map.of("position", 1, "chash", chInX),
            Map.of("position", 2, "chash", chInY)));

        assertThat(collectionOf(chInX)).isEqualTo(collX);
        assertThat(collectionOf(chInY))
            .as("RDR-191: the ETL import arm also stamps every row with the "
                + "caller's single collection, no per-row verification")
            .isEqualTo(collX);
    }

    /**
     * nexus-o8dil.7's original scenario tested that an AMBIGUOUS chash
     * (verified to live in two different real collections, with the doc's
     * own sibling stamp naming neither) must be SKIPPED rather than
     * guessed. RDR-191's final ruling removed the verification this
     * ambiguity check depended on entirely, so "ambiguous" is no longer a
     * concept the engine evaluates: even a chash that genuinely lives in
     * two OTHER collections is stamped with whatever THIS call's
     * caller-supplied collection says, unconditionally.
     */
    @Test
    void appendManifestChunks_chashAmbiguousAcrossRealCollections_stampedWithCallerSuppliedCollectionRegardless()
            throws Exception {
        String collX = "knowledge__mcs-g9x__voyage-context-3__v1";
        String collY = "knowledge__mcs-g9y__voyage-context-3__v1";
        String collZ = "knowledge__mcs-g9z__voyage-context-3__v1";
        String shared = "7".repeat(64);

        // The candidate chash genuinely lives in BOTH X and Y.
        seedChunkContent(collX, shared, "g9 shared content");
        seedChunkContent(collY, shared, "g9 shared content");

        String doc = repo.registerDocument(TENANT, "9.8", Map.of(
            "title", "g9 ambiguous doc", "content_type", "knowledge"));
        repo.appendManifestChunks(TENANT, doc, collZ, List.of(Map.of("position", 1, "chash", shared)));
        assertThat(collectionOf(shared))
            .as("RDR-191: no membership check runs, ambiguous or otherwise -- "
                + "the row is stamped with collZ exactly because the caller "
                + "said so, even though the content lives in X and Y, not Z")
            .isEqualTo(collZ);
    }

    @Test
    void renameCollection_reHomesManifestRows() throws Exception {
        // The second door back into the silently-empty state (critique Q2):
        // RDR-164 rename re-pointed docs + chunks + chash_index but NOT the
        // manifest's denormalized collection — post-rename the combined
        // queries would go empty again for that collection.
        String renamed = "knowledge__mcs-renamed__voyage-context-3__v1";
        repo.writeManifest(TENANT, docTumbler, COLL, List.of(
            Map.of("position", 0, "chash", CH_A)));
        Map<String, Integer> counts = repo.renameCollection(TENANT, COLL, renamed);
        assertThat(counts.get("catalog_document_chunks"))
            .as("rename re-homes the manifest join key").isEqualTo(1);
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT count(*) FROM nexus.search_metadata_scoped_1024("
                 + "('[' || repeat('0.1,', 1023) || '0.1]')::vector, "
                 + "ARRAY['" + renamed + "'], NULL::text, NULL::text, NULL::int, "
                 + "NULL::text, NULL::text, NULL::jsonb, 10)")) {
            rs.next();
            assertThat(rs.getInt(1))
                .as("combined query follows the rename end-to-end").isEqualTo(1);
        }
        // restore for sibling tests (PER_CLASS lifecycle, shared fixture)
        repo.renameCollection(TENANT, renamed, COLL);
    }

    @Test
    void catalog014_nullCollectionUnconditionallyRejected_evenWithForceRlsToggledOff() throws Exception {
        // RDR-191 (nexus-71gw2) rebase: catalog_document_chunks.collection is
        // NOT NULL as of catalog-025-collection-not-null.xml. catalog-014's
        // whole raison d'etre -- repairing a NULL-collection row that slipped
        // past FORCE RLS -- is now structurally impossible: there is no
        // NULL-collection row left to reconstruct, and re-running catalog-014
        // as a replayed NOSUPERUSER/NOBYPASSRLS owner would prove nothing new
        // (manifest_backfill() is a permanent 0-row no-op, RDR-191 plan §7.2
        // item 3). What still matters, and is NOT already covered elsewhere
        // in this suite, is that the NOT NULL constraint is unconditional: it
        // must hold even when FORCE ROW LEVEL SECURITY is toggled off and the
        // insert replays catalog-014's OWN toggle pattern -- proving the
        // constraint is independent of the RLS mechanism this changeset used
        // to rely on to reach the row at all.
        String chNull = "d".repeat(64);
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("ALTER TABLE nexus.catalog_document_chunks NO FORCE ROW LEVEL SECURITY");
            try {
                var ex = org.junit.jupiter.api.Assertions.assertThrows(
                    java.sql.SQLException.class, () ->
                        st.execute("INSERT INTO nexus.catalog_document_chunks "
                            + "(tenant_id, doc_id, position, chash, collection) "
                            + "VALUES ('" + TENANT + "', '" + docTumbler + "', 99, decode('"
                            + chNull + "', 'hex'), NULL)"));
                assertThat(ex.getSQLState())
                    .as("NOT NULL is a column constraint -- FORCE RLS being off must not exempt it")
                    .isEqualTo("23502");
            } finally {
                st.execute("ALTER TABLE nexus.catalog_document_chunks FORCE ROW LEVEL SECURITY");
            }
        }
    }
}
