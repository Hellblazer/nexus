// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgActivityProbe;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a row 14 (linda 2026-11-24, CatalogMoves before the latch): a document move writes in the collections of the rows it touches besides
 * the document's own: the document's aspect and highlight rows (each carries its own collection) and the documents that name it as {@code alias_of}.
 * From gate 3 it reads all of their registry rows in one id-ordered statement ({@code collectionIdsOf}); before the latch it read only the documents'
 * own labels, so a verb that held the registry row of a collection only an aspect, a highlight or an alias row was filed under ran beside the move.
 * Each case holds that collection's row {@code FOR UPDATE} on a hand session (what a retiring verb's first step holds) and starts a pre-latch move
 * of the document: the move must wait on that row (the registry read, FOR KEY SHARE) before it writes, and complete once the session lets go. The
 * wait is observed through {@code pg_stat_activity}, never a sleep.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogMovesPreLatchLockTest {

    private static final String SVC_ROLE = "svc_moves_prelatch";
    private static final String SVC_PASS = "svc_moves_prelatch_pass";
    private static final String HOME = "code__prelatch-home__voyage-code-3__v1";
    private static final String OTHER = "code__prelatch-other__voyage-code-3__v1";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    CatalogMoves moves;
    AspectRepository aspects;
    ExecutorService pool;
    int seq;

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
        var scope = new TenantScope(svcDs);
        repo = new CatalogRepository(scope, () -> false, RenameGuard.open());   // gate 3 has not latched
        moves = new CatalogMoves(repo);
        aspects = new AspectRepository(scope);
        pool = Executors.newCachedThreadPool();
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T su(java.util.function.Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** A tenant with the home collection, the other collection, a curator owner and one document S of the home collection. Returns {tenant, S}. */
    private String[] setUp() {
        String t = "moves-prelatch-" + (++seq);
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, t, HOME);
            PgContainerHelper.insertCollection(ctx, t, OTHER);
            return null;
        });
        repo.upsertOwner(t, Map.of("tumbler_prefix", "1.1", "name", "pdfs", "owner_type", "curator"));
        return new String[] {t, document(t, "s", "file:///old/a.pdf", "/old/a.pdf", HOME)};
    }

    private String document(String t, String title, String uri, String path, String collection) {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", title);
        f.put("file_path", path);
        f.put("content_type", "code");
        f.put("corpus", "code");
        f.put("physical_collection", collection);
        if (uri != null) f.put("source_uri", uri);
        return repo.registerDocument(t, "1.1", f);
    }

    /** The registry row of {@code label} held {@code FOR UPDATE} by a hand session, as a retiring verb's first step holds it. */
    private Connection verbHolding(String t, String label) throws Exception {
        Connection verb = pg.createConnection("");
        verb.setAutoCommit(false);
        DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t)).and(CATALOG_COLLECTIONS.NAME.eq(label)).forUpdate().fetch();
        return verb;
    }

    /** Starts the pre-latch move of S while a verb holds {@code OTHER}; asserts it waits for the registry row, then finishes after the release. */
    private void theMoveWaitsForTheRowOfTheOtherCollection(String t, String s) throws Exception {
        Future<Map<String, Object>> move;
        try (Connection verb = verbHolding(t, OTHER)) {
            move = pool.submit(() -> moves.moveDocument(t, s, "file:///new/a.pdf", "/new/a.pdf", false).toMap());
            assertThat(PgActivityProbe.waitsOnALock(pg, "%key share%catalog_collections%"))
                .as("the move waits on the registry row of the collection only a row of the document is filed under").isTrue();
            assertThat(move.isDone()).as("and has not finished").isFalse();
            String stillOld = su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(s)).fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
            assertThat(stillOld).as("it had written nothing when it met the verb").isEqualTo("file:///old/a.pdf");
            verb.rollback();
        }
        Map<String, Object> report = move.get(60, TimeUnit.SECONDS);
        assertThat(report).as("the move completed once the verb let go").isNotNull();
        String moved = su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(s)).fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
        assertThat(moved).isEqualTo("file:///new/a.pdf");
    }

    @Timeout(180)
    @Test
    void aPreLatchMove_waitsForTheRegistryRowOfACollectionOnlyAnAspectRowOfTheDocumentIsFiledUnder() throws Exception {
        String[] c = setUp();
        String t = c[0];
        String s = c[1];
        long other = su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t)).and(CATALOG_COLLECTIONS.NAME.eq(OTHER)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        su(ctx -> ctx.insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, t).set(DOCUMENT_ASPECTS.COLLECTION, OTHER).set(DOCUMENT_ASPECTS.COLLECTION_ID, other)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, "/old/a.pdf").set(DOCUMENT_ASPECTS.SOURCE_URI, "file:///old/a.pdf").set(DOCUMENT_ASPECTS.DOC_ID, s)
            .set(DOCUMENT_ASPECTS.CONFIDENCE, 0.9).set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.now())
            .set(DOCUMENT_ASPECTS.MODEL_VERSION, "v1").set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test").execute());
        theMoveWaitsForTheRowOfTheOtherCollection(t, s);
    }

    @Timeout(180)
    @Test
    void aPreLatchMove_waitsForTheRegistryRowOfACollectionOnlyAHighlightRowOfTheDocumentIsFiledUnder() throws Exception {
        String[] c = setUp();
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("doc_id", c[1]);
        b.put("collection", OTHER);
        b.put("source_uri", "file:///old/a.pdf");
        b.put("highlights_md", "marks");
        b.put("ingested_at", OffsetDateTime.now().toString());
        aspects.upsertHighlight(c[0], b);
        theMoveWaitsForTheRowOfTheOtherCollection(c[0], c[1]);
    }

    @Timeout(180)
    @Test
    void aPreLatchMove_waitsForTheRegistryRowOfACollectionOnlyAnAliasOfTheDocumentIsFiledUnder() throws Exception {
        String[] c = setUp();
        String alias = document(c[0], "alias", null, "/alias.pdf", OTHER);
        su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, c[1])
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(c[0])).and(CATALOG_DOCUMENTS.TUMBLER.eq(alias)).execute());
        theMoveWaitsForTheRowOfTheOtherCollection(c[0], c[1]);
    }
}
