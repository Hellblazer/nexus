// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * nexus-78n33: the register-path TOCTOU backstop (catalog-016).
 *
 * <p>registerDocument/registerDocumentMany check idempotency BEFORE claiming
 * a sequence number, under READ COMMITTED — two concurrent registrations of
 * the same NEW source_uri could both pass the SELECT and both INSERT. The
 * partial unique index {@code ux_catalog_documents_live_source_uri} plus
 * ON CONFLICT DO NOTHING + winner re-select makes the loser converge on the
 * winner's tumbler instead of minting a duplicate live document.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Catalog016SourceUriUniqueTest {

    private static final String TENANT   = "cat016-tenant";
    private static final String SVC_ROLE = "svc_cat016_test";
    private static final String SVC_PASS = "svc_cat016_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
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

    private int liveRowsForUri(String uri) throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents "
                + "WHERE tenant_id = '" + TENANT + "' AND source_uri = '" + uri + "' "
                + "AND deleted_at IS NULL");
            rs.next();
            return rs.getInt(1);
        }
    }

    private boolean liveTumblerExists(String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + tumbler + "' "
                + "AND deleted_at IS NULL");
            rs.next();
            return rs.getInt(1) == 1;
        }
    }

    @Test
    void deriveSourceUriMirrorsThePythonNormalizer() {
        // Parity with catalog.py _normalize_source_uri (RDR-096 + nexus-3e4s):
        // relative file_path anchors on the OWNER's repo_root, lexically.
        assertEquals("file:///repo/root/docs/a.md",
            CatalogRepository.deriveSourceUri("", "docs/a.md", "/repo/root"));
        assertEquals("file:///repo/root/docs/a.md",
            CatalogRepository.deriveSourceUri("", "sub/../docs/a.md", "/repo/root"));
        assertEquals("file:///abs/b.md",
            CatalogRepository.deriveSourceUri("", "/abs/b.md", ""));
        // Relative path with no anchor stays shapeless — the server must
        // never resolve against its own CWD (nexus-3e4s class).
        assertEquals("", CatalogRepository.deriveSourceUri("", "docs/a.md", ""));
        assertEquals("", CatalogRepository.deriveSourceUri("", "docs/a.md", "relative-root"));
        // Explicit source_uri always passes through untouched.
        assertEquals("chroma://x", CatalogRepository.deriveSourceUri("chroma://x", "docs/a.md", "/r"));
        assertEquals("", CatalogRepository.deriveSourceUri("", "", "/repo/root"));
    }

    /**
     * nexus-e7cys: the cross-project containment guard restored engine-side
     * on the 5-arg overload. The 3-arg overload above stays unchanged (no
     * owner_type to check against) — every case here uses the 5-arg form.
     */
    @Test
    void deriveSourceUri_outsideRepoRoot_rejectedWithNamedError() {
        var ex = assertThrows(CatalogRepository.CrossProjectSourceUriException.class, () ->
            CatalogRepository.deriveSourceUri(
                "file:///other/project/file.py", "", "/repo/root", "repo", false));
        assertTrue(ex.getMessage().contains("cross-project source_uri rejected"));
        assertTrue(ex.getMessage().contains("nexus-3e4s"));
    }

    @Test
    void deriveSourceUri_insideRepoRoot_passesVerbatim() {
        assertEquals("file:///repo/root/docs/a.md",
            CatalogRepository.deriveSourceUri(
                "file:///repo/root/docs/a.md", "", "/repo/root", "repo", false));
    }

    @Test
    void deriveSourceUri_overrideHonored_logsWarning() {
        ch.qos.logback.classic.Logger rootLogger =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        rootLogger.addAppender(logs);
        try {
            String result = CatalogRepository.deriveSourceUri(
                "file:///other/project/file.py", "", "/repo/root", "repo", true);
            assertEquals("file:///other/project/file.py", result);

            var warnings = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .filter(m -> m.startsWith("event=cross_project_source_uri_override_used"))
                .toList();
            assertEquals(1, warnings.size(), "the override bypass must be logged exactly once");
        } finally {
            rootLogger.detachAppender(logs);
            logs.stop();
        }
    }

    @Test
    void deriveSourceUri_nonRepoOwner_passesThrough() {
        assertEquals("file:///other/project/file.py",
            CatalogRepository.deriveSourceUri(
                "file:///other/project/file.py", "", "/repo/root", "curator", false));
    }

    @Test
    void deriveSourceUri_nonFileSchemes_passThrough() {
        assertEquals("chroma://x",
            CatalogRepository.deriveSourceUri("chroma://x", "", "/repo/root", "repo", false));
        assertEquals("https://example.com/doc",
            CatalogRepository.deriveSourceUri("https://example.com/doc", "", "/repo/root", "repo", false));
    }

    @Test
    void deriveSourceUri_recombinationLeg_unaffectedByContainmentGuard() {
        // The recombination leg (relative file_path anchored on repo_root) is
        // inherently containment-safe by construction — the 5-arg overload
        // must not regress it.
        assertEquals("file:///repo/root/docs/a.md",
            CatalogRepository.deriveSourceUri("", "docs/a.md", "/repo/root", "repo", false));
    }

    @Test
    void filePathOnlyRegistrationsDeriveUriAndAreRaceGuarded() throws Exception {
        // The critique's Critical 2: the dominant `nx index repo` path sends
        // NO source_uri — pre-derivation those rows bypassed the index
        // entirely. With server-side derivation they are guarded.
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", "17", "name", "derive-repo", "owner_type", "repo",
            "repo_hash", "d17", "description", "", "repo_root", "/repo/seventeen",
            "head_hash", "", "next_seq", 0L));
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            CountDownLatch start = new CountDownLatch(1);
            List<Future<String>> futures = new ArrayList<>();
            for (int t = 0; t < 2; t++) {
                futures.add(pool.submit(() -> {
                    start.await();
                    return repo.registerDocument(TENANT, "17", Map.of(
                        "title", "derived", "file_path", "docs/derived.md"));
                }));
            }
            start.countDown();
            String a = futures.get(0).get();
            String b = futures.get(1).get();
            assertEquals(a, b, "file_path-only racers must converge via the derived uri");
        } finally {
            pool.shutdownNow();
        }
        assertEquals(1, liveRowsForUri("file:///repo/seventeen/docs/derived.md"),
            "the stored row carries the DERIVED source_uri");

        // Batch path derives too: idempotent against the raced doc via the
        // derived uri, and intra-batch file_path dupes alias to one row.
        List<String> out = repo.registerDocumentMany(TENANT, "17", List.of(
            Map.of("title", "again", "file_path", "docs/derived.md"),
            Map.of("title", "n1", "file_path", "docs/new1.md"),
            Map.of("title", "n1-dup", "file_path", "docs/new1.md")));
        assertEquals(3, out.size());
        assertTrue(liveTumblerExists(out.get(0)));
        assertEquals(1, liveRowsForUri("file:///repo/seventeen/docs/derived.md"),
            "batch re-registration is idempotent via the derived uri");
        assertEquals(out.get(1), out.get(2), "intra-batch file_path dupes alias");
        assertEquals(1, liveRowsForUri("file:///repo/seventeen/docs/new1.md"));
    }

    @Test
    void ownerWithoutRepoRootKeepsShapelessUri() throws Exception {
        // Residual population (documented): relative path + no repo_root →
        // source_uri stays '' and the row rides file_path idempotency only.
        String t = repo.registerDocument(TENANT, "18", Map.of(
            "title", "shapeless", "file_path", "notes/x.md"));
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT source_uri FROM nexus.catalog_documents "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + t + "'");
            rs.next();
            assertEquals("", rs.getString(1));
        }
    }

    @Test
    void indexRefusesSecondLiveRowForSameSourceUri() throws Exception {
        // Deterministic proof of the backstop, independent of race timing:
        // a raw second live INSERT for the same (tenant, source_uri) violates
        // the partial unique index.
        repo.registerDocument(TENANT, "10", Map.of(
            "title", "det", "source_uri", "file:///det/a.md", "file_path", "a.md"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ex = assertThrows(java.sql.SQLException.class, () ->
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, source_uri) "
                    + "VALUES ('" + TENANT + "', '10.9999', 'dup', 'file:///det/a.md')"));
            assertTrue(ex.getMessage().contains("ux_catalog_documents_live_source_uri"),
                "expected the partial unique index to refuse: " + ex.getMessage());
        }
    }

    @Test
    void emptySourceUriRowsNeverCollide() throws Exception {
        String t1 = repo.registerDocument(TENANT, "11", Map.of(
            "title", "no-uri-1", "file_path", "one.md"));
        String t2 = repo.registerDocument(TENANT, "11", Map.of(
            "title", "no-uri-2", "file_path", "two.md"));
        assertTrue(liveTumblerExists(t1));
        assertTrue(liveTumblerExists(t2));
    }

    @Test
    void tombstonedUriCanBeReRegistered() throws Exception {
        String first = repo.registerDocument(TENANT, "12", Map.of(
            "title", "tomb", "source_uri", "file:///tomb/x.md", "file_path", "x.md"));
        repo.deleteDocument(TENANT, first);
        String second = repo.registerDocument(TENANT, "12", Map.of(
            "title", "tomb-2", "source_uri", "file:///tomb/x.md", "file_path", "x.md"));
        assertTrue(!first.equals(second), "re-registration after tombstone mints a new live doc");
        assertEquals(1, liveRowsForUri("file:///tomb/x.md"));
    }

    @Test
    void concurrentSingleDocRegistrationsConvergeOnOneTumbler() throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            for (int round = 0; round < 5; round++) {
                String uri = "file:///race/single-" + round + ".md";
                // file_path must be per-round too: the file_path idempotency
                // fallback is owner-scoped, and a reused path would resolve
                // later rounds to round 0's document without inserting.
                String path = "race-" + round + ".md";
                CountDownLatch start = new CountDownLatch(1);
                List<Future<String>> futures = new ArrayList<>();
                for (int t = 0; t < 2; t++) {
                    futures.add(pool.submit(() -> {
                        start.await();
                        return repo.registerDocument(TENANT, "13", Map.of(
                            "title", "race", "source_uri", uri, "file_path", path));
                    }));
                }
                start.countDown();
                String a = futures.get(0).get();
                String b = futures.get(1).get();
                assertEquals(a, b, "both racers must converge on one tumbler (round " + round + ")");
                assertEquals(1, liveRowsForUri(uri), "exactly one live row (round " + round + ")");
                assertTrue(liveTumblerExists(a), "returned tumbler must be a real live doc");
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void concurrentSingleDocRegistrations_exactlyOneReportsCreatedTrue() throws Exception {
        // nexus-vfef0: this is the SAME race as
        // concurrentSingleDocRegistrationsConvergeOnOneTumbler above, but
        // asserting the created-vs-matched wire signal that bead adds. The
        // ON-CONFLICT loser leg (:962-984 pre-fix line numbers) must report
        // created=false — pre-fix it silently returned the winner's tumbler
        // with no way to tell the two legs apart, which is exactly the
        // residual documented on the client's rollback_minted_catalog_entry.
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            for (int round = 0; round < 5; round++) {
                String uri = "file:///race/created-flag-" + round + ".md";
                String path = "race-created-flag-" + round + ".md";
                CountDownLatch start = new CountDownLatch(1);
                List<Future<CatalogRepository.RegisterOutcome>> futures = new ArrayList<>();
                for (int t = 0; t < 2; t++) {
                    futures.add(pool.submit(() -> {
                        start.await();
                        return repo.registerDocumentWithOutcome(TENANT, "13", Map.of(
                            "title", "race", "source_uri", uri, "file_path", path));
                    }));
                }
                start.countDown();
                var a = futures.get(0).get();
                var b = futures.get(1).get();
                assertEquals(a.tumbler(), b.tumbler(),
                    "both racers must converge on one tumbler (round " + round + ")");
                assertTrue(a.created() ^ b.created(),
                    "exactly one racer must report created=true, the other created=false "
                    + "(round " + round + ")");
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void batchIntraBatchDuplicateUrisAliasToOneDocument() throws Exception {
        var docs = List.<Map<String, Object>>of(
            Map.of("title", "b1", "source_uri", "file:///batch/dup.md", "file_path", "dup.md"),
            Map.of("title", "b2", "source_uri", "file:///batch/other.md", "file_path", "other.md"),
            Map.of("title", "b3", "source_uri", "file:///batch/dup.md", "file_path", "dup.md"));
        List<String> out = repo.registerDocumentMany(TENANT, "14", docs);
        assertEquals(3, out.size());
        assertEquals(out.get(0), out.get(2), "intra-batch same-uri docs alias to one tumbler");
        assertEquals(1, liveRowsForUri("file:///batch/dup.md"));
        for (String t : out) assertTrue(liveTumblerExists(t), "no dangling tumbler: " + t);
    }

    @Test
    void concurrentBatchesConvergeWithNoDanglingTumblers() throws Exception {
        List<Map<String, Object>> docs = new ArrayList<>();
        for (int i = 0; i < 6; i++) {
            docs.add(Map.of(
                "title", "cb-" + i,
                "source_uri", "file:///race/batch-" + i + ".md",
                "file_path", "batch-" + i + ".md"));
        }
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            CountDownLatch start = new CountDownLatch(1);
            List<Future<List<String>>> futures = new ArrayList<>();
            for (int t = 0; t < 2; t++) {
                futures.add(pool.submit(() -> {
                    start.await();
                    return repo.registerDocumentMany(TENANT, "15", docs);
                }));
            }
            start.countDown();
            List<String> a = futures.get(0).get();
            List<String> b = futures.get(1).get();
            assertEquals(a, b, "both batch racers must converge on the same tumblers");
            for (int i = 0; i < docs.size(); i++) {
                assertEquals(1, liveRowsForUri("file:///race/batch-" + i + ".md"));
                assertTrue(liveTumblerExists(a.get(i)), "no dangling tumbler: " + a.get(i));
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void dedupBackfillSqlTombstonesLosersKeepingMostChunks() throws Exception {
        // Fidelity test of the SHIPPED 016-0 SQL: seed legacy-shaped dupes with
        // the index dropped, run the changeset's own <sql> text verbatim, and
        // assert the winner rule (most chunks, then earliest indexed_at, then
        // lowest tumbler). Re-creates the index afterwards via 016-1's SQL.
        String xml = new String(
            getClass().getClassLoader()
                .getResourceAsStream("db/changelog/catalog-016-source-uri-unique.xml")
                .readAllBytes(),
            StandardCharsets.UTF_8);
        String dedupSql = extractSql(xml, 0);
        String indexSql = extractSql(xml, 1);
        assertTrue(dedupSql.contains("NO FORCE ROW LEVEL SECURITY"),
            "016-0 must carry the catalog-013-1b FORCE-RLS toggle");

        // nexus-cefa1.2: 016-0 is a run-once, ALREADY-SHIPPED changeset — its
        // <sql> body must NEVER be edited, and on every real migration chain
        // (fresh install or upgrade) it executes BEFORE catalog-031-1-
        // documents-temporal, while indexed_at is still TEXT, so the shipped
        // NULLIF(indexed_at,'') is correct there and stays untouched in the
        // changelog. This TEST, though, replays that text verbatim against a
        // schema where catalog-031 has ALREADY run earlier in the SAME chain
        // (Liquibase applies changesets in file order, and 016 precedes 031),
        // so indexed_at is timestamptz by the time this test's own DROP
        // INDEX / replay runs — NULLIF(timestamptz, unknown) fails to bind
        // the '' literal at all ("invalid input syntax for type timestamp
        // with time zone: \"\""). Apply the type-adjusted equivalent ONLY to
        // this test's in-memory copy of the SQL text: bare `indexed_at`
        // (NULL still sorts last under "ASC NULLS LAST", identical ordering
        // outcome to NULLIF('','')=NULL on the pre-migration TEXT column).
        // Asserted below so a future rewrite of 016-0's shipped text fails
        // this test LOUDLY instead of silently replaying an unsubstituted
        // (and now wrong) expression.
        String legacyNullSentinelExpr = "NULLIF(indexed_at, '')";
        assertTrue(dedupSql.contains(legacyNullSentinelExpr),
            "016-0's shipped <sql> no longer contains " + legacyNullSentinelExpr
            + " verbatim — the type-adjusted replay substitution below is stale; "
            + "re-derive it against the current changeset text before trusting this test");
        dedupSql = dedupSql.replace(legacyNullSentinelExpr, "indexed_at");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DROP INDEX nexus.ux_catalog_documents_live_source_uri");
            try {
                runDedupAndAssert(su, dedupSql);
            } catch (Exception primary) {
                // Review (78n33): the index is SHARED suite state — an
                // assertion failure above must not leave it dropped for
                // every later test in this PER_CLASS container. But a
                // recreate failure (e.g. duplicate rows survived because the
                // dedup itself failed) must not MASK the primary exception —
                // that masking is exactly what turned a clear "invalid input
                // syntax" root cause into an opaque "no unique or exclusion
                // constraint matching the ON CONFLICT" report one layer up.
                // Attach as suppressed instead of letting it propagate.
                try {
                    for (String stmt : indexSql.split(";")) {
                        if (!stmt.isBlank()) su.createStatement().execute(stmt);
                    }
                } catch (Exception recreateFailure) {
                    primary.addSuppressed(recreateFailure);
                }
                throw primary;
            }
            // Success path: recreate the index normally — a genuine failure
            // here (not masking anything) surfaces as itself.
            for (String stmt : indexSql.split(";")) {
                if (!stmt.isBlank()) su.createStatement().execute(stmt);
            }
        }
    }

    private void runDedupAndAssert(Connection su, String dedupSql) throws Exception {
        {
            String uri = "file:///legacy/dup.md";
            // nexus-cefa1.2: indexed_at is timestamptz now (catalog-031-1-documents-temporal)
            // — 16.3's undated row uses NULL (the '' -> NULL post-migration shape), not the
            // legacy '' sentinel the pre-migration NULLIF(indexed_at,'') dedup ORDER BY was
            // written to tolerate; NULL still sorts last under "ASC NULLS LAST".
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, source_uri, chunk_count, indexed_at) VALUES "
                + "('" + TENANT + "', '16.1', 'loser-few-chunks',  '" + uri + "', 2, '2026-01-01T00:00:00Z'),"
                + "('" + TENANT + "', '16.2', 'winner-most-chunks','" + uri + "', 9, '2026-01-02T00:00:00Z'),"
                + "('" + TENANT + "', '16.3', 'loser-no-chunks',   '" + uri + "', 0, NULL)");
            for (String stmt : dedupSql.split(";")) {
                if (!stmt.isBlank()) su.createStatement().execute(stmt);
            }
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT tumbler FROM nexus.catalog_documents "
                + "WHERE tenant_id = '" + TENANT + "' AND source_uri = '" + uri + "' AND deleted_at IS NULL");
            assertTrue(rs.next(), "one live winner must survive");
            assertEquals("16.2", rs.getString(1), "winner is the most-chunk-bearing row");
            assertTrue(!rs.next(), "exactly one live row survives");
            ResultSet tomb = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents "
                + "WHERE tenant_id = '" + TENANT + "' AND source_uri = '" + uri + "' AND deleted_at IS NOT NULL");
            tomb.next();
            assertEquals(2, tomb.getInt(1), "losers are tombstoned, not deleted");
        }
    }

    /** Extract the Nth &lt;sql&gt; block's text content from changeset XML. */
    private static String extractSql(String xml, int n) {
        int idx = -1;
        for (int i = 0; i <= n; i++) idx = xml.indexOf("<sql", idx + 1);
        assertTrue(idx >= 0, "changeset <sql> block " + n + " not found");
        int open = xml.indexOf('>', idx) + 1;
        int close = xml.indexOf("</sql>", open);
        return xml.substring(open, close)
                  .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&");
    }
}
