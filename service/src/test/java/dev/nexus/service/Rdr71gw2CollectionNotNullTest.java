// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.vectors.DimTables;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.postgresql.util.PSQLException;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-191 nexus-71gw2 (Phase C) — {@code catalog-025-collection-not-null.xml}.
 *
 * <p>Design of record: Hal's ruling 2026-08-12 (T2 {@code
 * rdr-191-p0-burndown-state-2026-08-12}, superseding both the rev-1
 * sentinel design and the rev-2 per-row C-membership/sibling-tiebreak
 * resolution CTE). Writers now SEND the collection on every manifest write
 * (see {@code CatalogRepository.writeManifestRows}/{@code
 * appendManifestChunks}/{@code importChunksBatch}/{@code doImportChunk});
 * the engine never infers one. This changeset's job is strictly
 * backward-looking: an unresolvable (NULL) or dangling (stamped but
 * unverified) row left behind by an OLD write path is DELETED outright —
 * never resolved, never guessed — then the column is closed with NOT NULL,
 * no sentinel, no DEFAULT.
 *
 * <p><strong>Why this replays the changeset's raw SQL directly, not through
 * Liquibase bookkeeping:</strong> {@code @BeforeAll} runs the FULL master
 * changelog once, so {@code catalog-025-0} is already recorded applied (and,
 * on a fresh container, a no-op — zero NULL rows exist yet). Every scenario
 * below needs a corpus that GENUINELY STARTS with NULL/dangling rows, so
 * each test temporarily {@code DROP}s the NOT NULL constraint, seeds its own
 * fixture, then re-executes the changeset's own {@code <sql>} block VERBATIM
 * (read from the changelog file at test time via {@link #changesetSql()},
 * never a hand-copied duplicate — the two can never drift apart) — proving
 * the ACTUAL production SQL converges on the fixture, not a stand-in for it.
 * The PostgreSQL JDBC simple-query protocol executes a semicolon-separated
 * multi-statement string as one implicit transaction when none is already
 * open, matching Liquibase's own per-changeset transaction shape.
 *
 * <p>Every scenario is seeded with a DISTINCT tenant so the changeset's
 * fleet-wide (no GUC) scan cannot cross-contaminate between tests even
 * though it necessarily operates on every tenant's rows at once.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr71gw2CollectionNotNullTest {

    private static final Path CHANGESET_PATH = Path.of(
        "src", "main", "resources", "db", "changelog", "catalog-025-collection-not-null.xml");

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    /** Defensive, best-effort cleanup after EVERY test (pass or fail): the
     *  NOT NULL constraint and the null-collection population are GLOBAL
     *  schema state shared across every test method in this class (JUnit's
     *  method execution order is unspecified without an explicit
     *  {@code @TestMethodOrder}), so a test that fails partway through its
     *  own drop/seed/run cycle must not leave the column nullable (or a
     *  stray NULL row) for the next test to trip over. Swallows its own
     *  failures deliberately -- the real signal is the test's own
     *  assertion, not this cleanup. */
    @AfterEach
    void restoreNotNullInvariant() {
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute(
                "DELETE FROM nexus.catalog_document_chunks WHERE collection IS NULL");
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks ALTER COLUMN collection SET NOT NULL");
        } catch (Exception ignored) {
            // best-effort; do not mask the test's own failure
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // Changeset replay plumbing
    // ══════════════════════════════════════════════════════════════════════════

    /** Reads {@code catalog-025-collection-not-null.xml} and returns the raw
     *  SQL body of changeset {@code catalog-025-0}, XML-unescaped — the same
     *  text Liquibase itself executes, never a hand-copied duplicate. */
    private static String changesetSql() throws Exception {
        String xml = Files.readString(CHANGESET_PATH);
        Matcher m = Pattern.compile("<sql[^>]*>(.*?)</sql>", Pattern.DOTALL).matcher(xml);
        assertThat(m.find()).as("catalog-025-0's <sql> block must be present in the changelog file").isTrue();
        return m.group(1)
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&amp;", "&");
    }

    /** Undo just the NOT NULL constraint (not the whole changeset) so a test
     *  can seed a genuinely-NULL corpus, mirroring this changeset's own
     *  rollback. Idempotent: DROP NOT NULL on an already-nullable column is a
     *  no-op, so tests may run in any order. */
    private static void dropNotNullForTest(Connection su) throws Exception {
        su.createStatement().execute(
            "ALTER TABLE nexus.catalog_document_chunks ALTER COLUMN collection DROP NOT NULL");
    }

    /** Runs the changeset's SQL as ONE explicit transaction (autoCommit off
     *  around the call) -- {@code LOCK TABLE} requires a real transaction
     *  block (Postgres rejects it standalone with "LOCK TABLE can only be
     *  used in transaction blocks"), and an explicit transaction also
     *  matches what Liquibase itself wraps this changeset in. Restores the
     *  connection's prior autoCommit mode afterward regardless of outcome. */
    private static void runChangeset(Connection su) throws Exception {
        boolean priorAutoCommit = su.getAutoCommit();
        su.setAutoCommit(false);
        try (Statement st = su.createStatement()) {
            st.execute(changesetSql());
            su.commit();
        } catch (Exception e) {
            su.rollback();
            throw e;
        } finally {
            su.setAutoCommit(priorAutoCommit);
        }
    }

    private static boolean collectionIsNotNull(Connection su) throws Exception {
        try (Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT attnotnull FROM pg_attribute "
                 + "WHERE attrelid = 'nexus.catalog_document_chunks'::regclass "
                 + "AND attname = 'collection'")) {
            assertThat(rs.next()).as("collection column must exist").isTrue();
            return rs.getBoolean(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // Fixture helpers
    // ══════════════════════════════════════════════════════════════════════════

    private static String chash(String seed) {
        return Chash.ofText(seed).toHex();
    }

    /** nexus.chunks (RDR-191 unified; formerly chunks_384/768/1024) carries an FK
     *  on (tenant_id, collection) -> catalog_collections in the eventual Phase 5
     *  shape (not yet landed as of this bead) -- registering first keeps every
     *  fixture forward-compatible regardless of when that FK ships. */
    private static void insertCollection(Connection su, String tenantId, String name) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) "
            + "VALUES ('" + tenantId + "', '" + name + "') ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    private static void insertDoc(Connection su, String tenantId, String tumbler) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
            + "VALUES ('" + tenantId + "', '" + tumbler + "', 'Test Doc " + tumbler + "') "
            + "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    private static void insertManifestRowNull(Connection su, String tenantId, String docId,
                                               int position, String chashHex) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks "
            + "  (tenant_id, doc_id, position, chash) "
            + "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", "
            + "decode('" + chashHex + "', 'hex')) "
            + "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
    }

    private static void insertManifestRowStamped(Connection su, String tenantId, String docId,
                                                  int position, String chashHex, String collection)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks "
            + "  (tenant_id, doc_id, position, chash, collection) "
            + "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", "
            + "decode('" + chashHex + "', 'hex'), '" + collection + "') "
            + "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
    }

    /** RDR-191 Phase 4: nexus.chunks_<dim> unified into nexus.chunks -- dim now
     *  selects the target embedding_<dim> column, not a table. */
    private static void insertChunk(Connection su, int dim, String tenantId,
                                     String collection, String chashHex, int vecLen) throws Exception {
        insertCollection(su, tenantId, collection);
        su.createStatement().execute(
            "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(dim) + ") "
            + "VALUES ('" + tenantId + "', '" + collection + "', decode('" + chashHex + "', 'hex'), "
            + "'chunk text', ('[1" + ",0".repeat(vecLen - 1) + "]')::vector) "
            + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }

    private static void insertChunk1024(Connection su, String tenantId, String collection,
                                         String chashHex) throws Exception {
        insertChunk(su, 1024, tenantId, collection, chashHex, 1024);
    }

    private static String collectionOf(Connection su, String tenantId, String docId, int position)
            throws Exception {
        try (Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT collection FROM nexus.catalog_document_chunks "
                 + "WHERE tenant_id = '" + tenantId + "' AND doc_id = '" + docId
                 + "' AND position = " + position)) {
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private static boolean rowExists(Connection su, String tenantId, String docId, int position)
            throws Exception {
        try (Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT 1 FROM nexus.catalog_document_chunks "
                 + "WHERE tenant_id = '" + tenantId + "' AND doc_id = '" + docId
                 + "' AND position = " + position)) {
            return rs.next();
        }
    }

    private static int chunkCountOf(Connection su, String tenantId, String docId) throws Exception {
        try (Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT chunk_count FROM nexus.catalog_documents "
                 + "WHERE tenant_id = '" + tenantId + "' AND tumbler = '" + docId + "'")) {
            assertThat(rs.next()).isTrue();
            return rs.getInt(1);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // Step 1 — NULL-collection rows: unresolvable by definition, deleted
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void nullCollectionRow_isDeletedAndChunkCountResynced() throws Exception {
        String tenant = "rdr71gw2-t-nullrow";
        String doc = "d.1";
        String ch = chash("71gw2-nullrow");

        try (Connection su = pg.createConnection("")) {
            dropNotNullForTest(su);
            insertDoc(su, tenant, doc);
            insertManifestRowNull(su, tenant, doc, 0, ch);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET chunk_count = 1 "
                + "WHERE tenant_id = '" + tenant + "' AND tumbler = '" + doc + "'");

            assertThat(collectionOf(su, tenant, doc, 0)).isNull();

            runChangeset(su);

            assertThat(rowExists(su, tenant, doc, 0))
                .as("a NULL-collection row has nothing to resolve it from -- deleted, never guessed")
                .isFalse();
            assertThat(chunkCountOf(su, tenant, doc))
                .as("chunk_count must resync down to 0 -- the tombstone-scoped resync, not left stale")
                .isEqualTo(0);
            assertThat(collectionIsNotNull(su)).isTrue();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // Step 2 — dangling rows: a NON-NULL collection that does not actually
    // contain the chash (nexus-c30ew's exact failure shape) -- deleted
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void danglingStampedRow_notBackedByAnyChunk_isDeletedAndChunkCountResynced() throws Exception {
        String tenant = "rdr71gw2-t-dangling";
        String doc = "d.2";
        String wrongColl = "code__71gw2-dangling-wrong__voyage-code-3__v1";
        String rightColl = "code__71gw2-dangling-right__voyage-code-3__v1";
        String ch = chash("71gw2-dangling-chash");

        try (Connection su = pg.createConnection("")) {
            dropNotNullForTest(su);
            insertDoc(su, tenant, doc);
            // The chash is only actually embedded under `rightColl` -- the
            // manifest row is stamped `wrongColl`, which does NOT contain it.
            insertChunk1024(su, tenant, rightColl, ch);
            insertManifestRowStamped(su, tenant, doc, 0, ch, wrongColl);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET chunk_count = 1 "
                + "WHERE tenant_id = '" + tenant + "' AND tumbler = '" + doc + "'");

            runChangeset(su);

            assertThat(rowExists(su, tenant, doc, 0))
                .as("a stamped collection that does not actually contain the chash is dangling -- "
                    + "deleted, exactly what the Phase 5 FK would reject")
                .isFalse();
            assertThat(chunkCountOf(su, tenant, doc))
                .as("chunk_count must resync down to 0")
                .isEqualTo(0);
            assertThat(collectionIsNotNull(su)).isTrue();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // A row whose stamped collection IS verified by a real chunk survives
    // untouched -- never re-derived, never re-stamped.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void rowBackedByARealChunk_survivesUntouched() throws Exception {
        String tenant = "rdr71gw2-t-healthy";
        String doc = "d.3";
        String coll = "code__71gw2-healthy__voyage-code-3__v1";
        String ch = chash("71gw2-healthy-chash");

        try (Connection su = pg.createConnection("")) {
            dropNotNullForTest(su);
            insertDoc(su, tenant, doc);
            insertChunk1024(su, tenant, coll, ch);
            insertManifestRowStamped(su, tenant, doc, 0, ch, coll);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET chunk_count = 1 "
                + "WHERE tenant_id = '" + tenant + "' AND tumbler = '" + doc + "'");

            runChangeset(su);

            assertThat(collectionOf(su, tenant, doc, 0))
                .as("a row whose stamped collection genuinely contains the chash is untouched")
                .isEqualTo(coll);
            assertThat(chunkCountOf(su, tenant, doc)).isEqualTo(1);
            assertThat(collectionIsNotNull(su)).isTrue();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // The resync must never write through a tombstoned document
    // (nexus-mqd6t's non-resurrection rule, carried into this changeset)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void deletedRows_neverResyncChunkCountOnATombstonedDocument() throws Exception {
        String tenant = "rdr71gw2-t-tombstone";
        String doc = "d.4";
        String ch = chash("71gw2-tombstone-never-embedded");

        try (Connection su = pg.createConnection("")) {
            dropNotNullForTest(su);
            insertDoc(su, tenant, doc);
            insertManifestRowNull(su, tenant, doc, 0, ch);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET chunk_count = 9, deleted_at = now() "
                + "WHERE tenant_id = '" + tenant + "' AND tumbler = '" + doc + "'");

            runChangeset(su);

            assertThat(rowExists(su, tenant, doc, 0)).isFalse();
            assertThat(chunkCountOf(su, tenant, doc))
                .as("a tombstoned document's chunk_count must NOT be touched by the resync "
                    + "(deleted_at IS NULL guard) -- the stale 9 survives untouched")
                .isEqualTo(9);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // The boot-brick: SET NOT NULL must SUCCEED on a corpus mixing NULL,
    // dangling, and healthy rows at once -- proven, not asserted.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void setNotNullSucceedsOnAMixedCorpusOfNullDanglingAndHealthyRows() throws Exception {
        String tenant = "rdr71gw2-t-mixed";
        String coll = "code__71gw2-mixed__voyage-code-3__v1";
        String wrongColl = "code__71gw2-mixed-wrong__voyage-code-3__v1";

        try (Connection su = pg.createConnection("")) {
            dropNotNullForTest(su);
            assertThat(collectionIsNotNull(su))
                .as("CONTROL: the constraint must genuinely be gone before seeding")
                .isFalse();

            // NULL row -- deleted.
            insertDoc(su, tenant, "m.1");
            insertManifestRowNull(su, tenant, "m.1", 0, chash("71gw2-mixed-null"));

            // dangling row -- deleted.
            insertDoc(su, tenant, "m.2");
            String chDangling = chash("71gw2-mixed-dangling");
            insertChunk1024(su, tenant, coll, chDangling);
            insertManifestRowStamped(su, tenant, "m.2", 0, chDangling, wrongColl);

            // healthy row -- survives.
            insertDoc(su, tenant, "m.3");
            String chHealthy = chash("71gw2-mixed-healthy");
            insertChunk1024(su, tenant, coll, chHealthy);
            insertManifestRowStamped(su, tenant, "m.3", 0, chHealthy, coll);

            assertThatCode(() -> runChangeset(su))
                .as("the DELETE steps remove every row the ALTER would have rejected, so "
                    + "SET NOT NULL cannot fail regardless of the starting NULL population")
                .doesNotThrowAnyException();

            assertThat(collectionIsNotNull(su))
                .as("the boot-brick is dissolved: NOT NULL is live on a corpus that started "
                    + "with NULL and dangling rows")
                .isTrue();
            assertThat(rowExists(su, tenant, "m.1", 0)).isFalse();
            assertThat(rowExists(su, tenant, "m.2", 0)).isFalse();
            assertThat(collectionOf(su, tenant, "m.3", 0)).isEqualTo(coll);
            try (Statement st = su.createStatement();
                 ResultSet rs = st.executeQuery(
                     "SELECT count(*) FROM nexus.catalog_document_chunks WHERE collection IS NULL")) {
                rs.next();
                assertThat(rs.getLong(1)).isEqualTo(0L);
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // Unrepresentability: after the change, writing collection = NULL throws.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void insertingNullCollectionThrowsAfterTheChangeIsLive() throws Exception {
        String tenant = "rdr71gw2-t-throws";
        String doc = "d.5";

        try (Connection su = pg.createConnection("")) {
            assertThat(collectionIsNotNull(su))
                .as("class-level migration already applied catalog-025 on a fresh, "
                    + "zero-NULL container")
                .isTrue();
            insertDoc(su, tenant, doc);

            PSQLException ex = assertThrows(PSQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) "
                    + "VALUES ('" + tenant + "', '" + doc + "', 0, "
                    + "decode('" + chash("71gw2-throws") + "', 'hex'), NULL)"));
            assertThat(ex.getSQLState())
                .as("the state is unrepresentable -- assert the not-null-violation SQLSTATE "
                    + "directly, not merely a row count of 0")
                .isEqualTo("23502");
        }
    }

    @Test
    void theConvertedIndexCarriesNoPartialPredicate() throws Exception {
        try (Connection su = pg.createConnection("");
             Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT indexdef FROM pg_indexes "
                 + "WHERE schemaname = 'nexus' AND indexname = 'idx_catalog_chunks_collection'")) {
            assertThat(rs.next()).as("the index must still exist post-conversion").isTrue();
            assertThat(rs.getString(1))
                .as("the WHERE collection IS NOT NULL predicate is vacuous under NOT NULL and "
                    + "must be dropped, or the index risks degrading to a seq scan the moment a "
                    + "caller stops spelling the predicate out explicitly")
                .doesNotContainIgnoringCase("where");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // Fourth boot-brick: LOCK TABLE ... ACCESS EXCLUSIVE genuinely serializes a
    // concurrent writer around the delete/resync/ALTER window.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void concurrentWriterCannotSlipANullRowPastTheLock() throws Exception {
        String tenant = "rdr71gw2-t-lockwindow";
        String doc = "d.6";
        String ch = chash("71gw2-lockwindow");

        try (Connection su = pg.createConnection("")) {
            dropNotNullForTest(su);
            insertDoc(su, tenant, doc);
        }

        ExecutorService executor = Executors.newSingleThreadExecutor();
        try (Connection holder = DriverManager.getConnection(
                pg.getJdbcUrl(), pg.getUsername(), pg.getPassword())) {
            holder.setAutoCommit(false);
            try (Statement st = holder.createStatement()) {
                // The exact first statement catalog-025-0 opens with.
                st.execute("LOCK TABLE nexus.catalog_document_chunks IN ACCESS EXCLUSIVE MODE");
            }

            Future<?> writer = executor.submit(() -> {
                try (Connection su2 = pg.createConnection("")) {
                    su2.createStatement().execute(
                        "INSERT INTO nexus.catalog_document_chunks "
                        + "(tenant_id, doc_id, position, chash) "
                        + "VALUES ('" + tenant + "', '" + doc + "', 0, "
                        + "decode('" + ch + "', 'hex'))");
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });

            assertThatCode(() -> writer.get(750, TimeUnit.MILLISECONDS))
                .as("a concurrent writer attempting to insert a NULL-collection row must BLOCK "
                    + "while the changeset's opening LOCK is held -- a missing/broken lock would "
                    + "let this complete immediately")
                .isInstanceOf(TimeoutException.class);

            holder.commit();
            writer.get(15, TimeUnit.SECONDS);
        } finally {
            executor.shutdownNow();
        }

        try (Connection su = pg.createConnection("")) {
            assertThat(rowExists(su, tenant, doc, 0))
                .as("the writer proceeded once the lock released -- it must have actually landed, "
                    + "proving the lock serialized rather than silently dropped it")
                .isTrue();
            // Cleanup: restore the invariant so no NULL row from this test
            // leaks into any other test's fleet-wide resolution scan.
            su.createStatement().execute(
                "DELETE FROM nexus.catalog_document_chunks WHERE tenant_id = '" + tenant + "'");
            assertThatCode(() ->
                su.createStatement().execute(
                    "ALTER TABLE nexus.catalog_document_chunks ALTER COLUMN collection SET NOT NULL"))
                .doesNotThrowAnyException();
        }
    }
}
