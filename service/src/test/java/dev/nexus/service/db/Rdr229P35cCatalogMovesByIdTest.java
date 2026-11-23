// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RegistryRenameSupport;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5c (linda 2026-11-23): the document move and the owner fold find, lock and match the collections of their documents by the
 * registry id from gate 3, never by the label a document's {@code physical_collection} carries (a one-row rename leaves it stale, and the
 * label may now be another collection's). The state every case starts from is what
 * {@code CollectionRouteFixture#renameInPlaceBeforeTheWritesRead} leaves in the gap: the registry row X keeps its id and bears a new label C,
 * a NEW row Y bears the old label B, and the documents and their aspect and highlight rows still carry B. The move routes name no collection
 * at the route boundary (a tumbler or an owner), so the hook has nothing to fire on there; the rename is made before the move starts.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cCatalogMovesByIdTest {

    private static final String SVC_ROLE = "svc_p35c_moves_by_id";
    private static final String SVC_PASS = "svc_p35c_moves_by_id_pass";
    private static final String COLL_A = "code__p35cmoves-a__voyage-code-3__v1";
    private static final String COLL_B = "code__p35cmoves-b__voyage-code-3__v1";
    private static final String OLD_ROOT = "/old/p35c";
    private static final String NEW_ROOT = "/new/p35c";
    private static final String HASH_A = "aaaa3501";
    private static final String HASH_B = "bbbb3502";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    final AtomicBoolean latched = new AtomicBoolean();
    CatalogRepository repo;
    CatalogMoves moves;
    AspectRepository aspects;
    int tenantSeq;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(DSL.using(su, SQLDialect.POSTGRES));   // a one-row rename cascades nowhere
            // the name unique of the aspects (until P3.7 / P4.1 drops it): a collection that took the old label may hold a row at the path the move targets
            DSL.using(su, SQLDialect.POSTGRES).alterTable(DOCUMENT_ASPECTS).dropConstraintIfExists(DSL.name("document_aspects_tenant_col_path_uq")).execute();
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
        repo = new CatalogRepository(scope, latched::get, RenameGuard.open());
        moves = new CatalogMoves(repo);
        aspects = new AspectRepository(scope);
    }

    @AfterAll
    void stopAll() {
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

    private void register(String tenant, String label) {
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, label);
            return null;
        });
    }

    private long idOf(String tenant, String label) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    /** The one-row rename of the gap: the registry row keeps its id and takes a new label; a NEW row takes the old label. Returns {X, C}. */
    private Object[] renameAndRetake(String tenant, String label) {
        long x = idOf(tenant, label);
        String c = label.replace("__v1", "") + "-renamed__v1";
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, c)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x)).execute());
        register(tenant, label);
        assertThat(idOf(tenant, label)).as("setup: the old label is a NEW row's now").isNotEqualTo(x);
        assertThat(idOf(tenant, c)).as("setup: the renamed row keeps its id").isEqualTo(x);
        return new Object[] {x, c};
    }

    private String tenant() {
        return "p35c-moves-" + (++tenantSeq);
    }

    private void curatorOwner(String t, String prefix, String name) {
        repo.upsertOwner(t, Map.of("tumbler_prefix", prefix, "name", name, "owner_type", "curator"));
    }

    private void repoOwner(String t, String prefix, String name, String hash, String root) {
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("tumbler_prefix", prefix);
        o.put("name", name);
        o.put("owner_type", "repo");
        o.put("repo_hash", hash);
        o.put("repo_root", root);
        repo.upsertOwner(t, o);
    }

    private String registered(String t, String prefix, String title, String sourceUri, String filePath, String coll) {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", title);
        f.put("file_path", filePath);
        f.put("content_type", "code");
        f.put("corpus", "code");
        f.put("physical_collection", coll);
        if (sourceUri != null) f.put("source_uri", sourceUri);
        return repo.registerDocument(t, prefix, f);
    }

    private void highlight(String t, String docId, String coll, String sourceUri, String text) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("doc_id", docId);
        b.put("collection", coll);
        b.put("source_uri", sourceUri);
        b.put("highlights_md", text);
        b.put("ingested_at", OffsetDateTime.now().toString());
        aspects.upsertHighlight(t, b);
    }

    /** One aspect row, inserted directly: the aspects upsert arbitrates on the name unique this class drops (P3.7 / P4.1 drop it). */
    private void aspect(String t, String docId, String coll, String sourcePath, String sourceUri, String text) {
        long id = idOf(t, coll);
        su(ctx -> ctx.insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, t).set(DOCUMENT_ASPECTS.COLLECTION, coll).set(DOCUMENT_ASPECTS.COLLECTION_ID, id)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, sourcePath).set(DOCUMENT_ASPECTS.SOURCE_URI, sourceUri).set(DOCUMENT_ASPECTS.DOC_ID, docId)
            .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION, text).set(DOCUMENT_ASPECTS.CONFIDENCE, 0.9)
            .set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.now()).set(DOCUMENT_ASPECTS.MODEL_VERSION, "v1")
            .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test").execute());
    }

    private record Hl(String collection, Long collectionId) {}

    private Hl highlightOf(String t, String docId) {
        var r = su(ctx -> ctx.select(DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.COLLECTION_ID).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(t)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId)).fetchOne());
        return r == null ? null : new Hl(r.value1(), r.value2());
    }

    // ---- the document move ---------------------------------------------------------------------------------------------------------

    /**
     * S and T are curator documents of collection B (X); T holds the new URI and has a highlights row, S none. After X is renamed to C and
     * Y takes B, the move absorbs T into S and re-keys T's highlights to S's collection: the row X, by its id, under its CURRENT label C. By the
     * stored label it would be Y's row (the highlights would land in another collection) and be written under B.
     */
    @Test
    void aDocumentMoveRekeysTheAbsorbedDocumentsHighlightsToTheRenamedCollectionByItsId() throws Exception {
        String t = tenant();
        latched.set(false);
        register(t, COLL_A);
        curatorOwner(t, "1.1", "pdfs");
        String s = registered(t, "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", COLL_A);
        String tt = registered(t, "1.1", "t", "file:///new/a.pdf", "/new/a.pdf", COLL_A);
        highlight(t, tt, COLL_A, "file:///new/a.pdf", "marks on the new copy");
        Object[] gap = renameAndRetake(t, COLL_A);
        long x = (Long) gap[0];
        String c = (String) gap[1];
        long y = idOf(t, COLL_A);
        latched.set(true);

        var report = moves.moveDocument(t, s, "file:///new/a.pdf", "/new/a.pdf", false).toMap();

        assertThat(report).containsEntry("highlight_rows_rewritten", 1);
        Hl moved = highlightOf(t, s);
        assertThat(moved).as("the absorbed document's highlights now belong to S").isNotNull();
        assertThat(moved.collectionId()).as("they are filed under the renamed collection's id, not the new holder of the old label").isEqualTo(x).isNotEqualTo(y);
        assertThat(moved.collection()).as("under the locked row's CURRENT label, never the stale one S carries").isEqualTo(c);
    }

    /**
     * S has an aspect row in collection B (X). A DIFFERENT collection Y that took the label B has a row, for a document that no longer
     * exists, at the path S moves to. Matching by the stored label would take Y's row for X's and delete it; by the id it is left alone, and S's
     * row is the one rewritten.
     */
    @Test
    void aDocumentMoveNeverTouchesTheAspectRowsOfTheCollectionThatTookTheOldLabel() throws Exception {
        String t = tenant();
        latched.set(false);
        register(t, COLL_A);
        curatorOwner(t, "1.1", "pdfs");
        String s = registered(t, "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", COLL_A);
        aspect(t, s, COLL_A, "/old/a.pdf", "file:///old/a.pdf", "S's extraction");
        Object[] gap = renameAndRetake(t, COLL_A);
        long x = (Long) gap[0];
        long y = idOf(t, COLL_A);
        // the new holder's row of a TRASHED document, at the path S moves to (written directly: the stored label B is Y's now)
        String ghost = registered(t, "1.1", "ghost", null, "/ghost.pdf", COLL_A);
        su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(ghost)).execute());
        su(ctx -> ctx.insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, t).set(DOCUMENT_ASPECTS.COLLECTION, COLL_A).set(DOCUMENT_ASPECTS.COLLECTION_ID, y)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, "/new/a.pdf").set(DOCUMENT_ASPECTS.SOURCE_URI, "file:///gone/a.pdf")
            .set(DOCUMENT_ASPECTS.DOC_ID, ghost).set(DOCUMENT_ASPECTS.CONFIDENCE, 0.5)
            .set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.now()).set(DOCUMENT_ASPECTS.MODEL_VERSION, "v1")
            .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test").execute());
        latched.set(true);

        moves.moveDocument(t, s, "file:///new/a.pdf", "/new/a.pdf", false);

        var rows = su(ctx -> ctx.select(DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.COLLECTION_ID, DOCUMENT_ASPECTS.SOURCE_PATH)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.TENANT_ID.eq(t)).orderBy(DOCUMENT_ASPECTS.DOC_ID).fetch());
        assertThat(rows).as("S's row followed S to the new path, in X; the new holder's row is still there").hasSize(2);
        assertThat(rows.get(0).value1()).isEqualTo(s);
        assertThat(rows.get(0).value2()).isEqualTo(x);
        assertThat(rows.get(0).value3()).isEqualTo("/new/a.pdf");
        assertThat(rows.get(1).value1()).isEqualTo(ghost);
        assertThat(rows.get(1).value2()).isEqualTo(y);
    }

    /**
     * A move whose collection cannot be resolved is refused and writes nothing: the lock of the registry row of a document's collection is
     * never skipped (linda 2026-11-23). S's collection id names no registry row.
     */
    @Test
    void aDocumentMoveWhoseCollectionNamesNoRegistryRowIsRefusedAndWritesNothing() throws Exception {
        String t = tenant();
        latched.set(false);
        register(t, COLL_A);
        curatorOwner(t, "1.1", "pdfs");
        String s = registered(t, "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", COLL_A);
        su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, 987654321L)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(s)).execute());
        latched.set(true);

        assertThatThrownBy(() -> moves.moveDocument(t, s, "file:///new/a.pdf", "/new/a.pdf", false))
            .as("a lock that cannot be taken fails the move").isInstanceOf(RuntimeException.class);

        var uri = su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(s)).fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
        assertThat(uri).as("nothing was written").isEqualTo("file:///old/a.pdf");
    }

    /**
     * Before the latch, labels are exact, so an unregistered physical_collection has no registry row: there is no lock to take, and no verb can
     * race the move. The move goes ahead, and the WARN names the tenant, the document and the label (an unregistered collection is a data
     * problem). The post-latch case above is refused. (linda 2026-11-23)
     */
    @Test
    void beforeTheLatchADocumentWhoseCollectionHasNoRegistryRowStillMoves_andTheWarnNamesIt() throws Exception {
        String t = tenant();
        latched.set(false);
        register(t, COLL_A);
        curatorOwner(t, "1.1", "pdfs");
        String s = registered(t, "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", COLL_A);
        su(ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(t)).and(CATALOG_COLLECTIONS.NAME.eq(COLL_A)).execute());
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(CatalogMoves.class);
        var appender = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        appender.start();
        logger.setLevel(ch.qos.logback.classic.Level.WARN);
        logger.addAppender(appender);
        try {
            moves.moveDocument(t, s, "file:///new/a.pdf", "/new/a.pdf", false);
        } finally {
            logger.detachAppender(appender);
        }

        var uri = su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(s)).fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
        assertThat(uri).as("the move went ahead without a lock").isEqualTo("file:///new/a.pdf");
        assertThat(appender.list).as("and the WARN names the tenant, the document and the label").anyMatch(e ->
            e.getLevel() == ch.qos.logback.classic.Level.WARN && e.getFormattedMessage().contains("tenant=" + t)
                && e.getFormattedMessage().contains("doc_id=" + s) && e.getFormattedMessage().contains("collection=" + COLL_A));
    }

    // ---- the owner fold ------------------------------------------------------------------------------------------------------------

    /**
     * A (1.1) and B (1.2) hold the same file x; A's x is in collection COLL_A, B's in COLL_B, and B's x has a highlights row. After A's collection is
     * renamed and another row takes its old label, the fold re-keys B's highlights onto A's x in A's collection: the renamed row by its id under
     * its current label.
     */
    @Test
    void anOwnerFoldRekeysTheFoldedDocumentsHighlightsToTheRenamedCollectionByItsId() throws Exception {
        String t = tenant();
        latched.set(false);
        register(t, COLL_A);
        register(t, COLL_B);
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        String x = registered(t, "1.1", "docs/x.md", null, "docs/x.md", COLL_A);
        repoOwner(t, "1.2", "proj-renamed", HASH_B, NEW_ROOT);
        String bx = registered(t, "1.2", "docs/x.md", null, "docs/x.md", COLL_B);
        String bxUri = su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(bx)).fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
        highlight(t, bx, COLL_B, bxUri, "B marks on x");
        Object[] gap = renameAndRetake(t, COLL_A);
        long idA = (Long) gap[0];
        String c = (String) gap[1];
        long y = idOf(t, COLL_A);
        latched.set(true);

        moves.moveOwner(t, "1.1", NEW_ROOT, HASH_B, false);

        Hl moved = highlightOf(t, x);
        assertThat(moved).as("B's highlights moved to A's x").isNotNull();
        assertThat(moved.collectionId()).as("filed under the renamed collection's id, not the new holder of its old label")
            .isEqualTo(idA).isNotEqualTo(y);
        assertThat(moved.collection()).as("under its current label").isEqualTo(c);
    }

    // ---- CM-FIX: the move holds the registry row of every collection it writes in, before any document row -------------------------------

    /**
     * Probes, from a second session inside the move's own transaction (after its last write), whether the registry row of {@code collectionId} is
     * held against a retiring verb: a verb's first step is {@code FOR UPDATE} on that row, which a held {@code FOR KEY SHARE} refuses (55P03 on
     * NOWAIT). Without the lock the probe takes the row and the verb would run beside the move.
     */
    private boolean heldAgainstAVerbDuring(String tenant, long collectionId, Runnable move) {
        java.util.concurrent.atomic.AtomicReference<Boolean> held = new java.util.concurrent.atomic.AtomicReference<>();
        CatalogMoves.afterWritesHookForTests = () -> {
            try (Connection c = pg.createConnection("")) {
                c.setAutoCommit(false);
                try {
                    DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId))
                        .forUpdate().noWait().fetch();
                    held.set(false);
                } catch (org.jooq.exception.DataAccessException e) {
                    held.set(true);   // 55P03: the move holds it
                } finally {
                    c.rollback();
                }
            } catch (java.sql.SQLException e) {
                throw new IllegalStateException(e);
            }
        };
        try {
            move.run();
        } finally {
            CatalogMoves.afterWritesHookForTests = null;
        }
        assertThat(held.get()).as("non-vacuity: the probe ran inside the move").isNotNull();
        return held.get();
    }

    private static final String COLL_K = "code__p35cmoves-k__voyage-code-3__v1";

    /** S has an aspect row filed under another collection K: the move holds K's registry row too, so a verb on K waits for it. */
    @Test
    void aDocumentMoveHoldsTheRegistryRowOfACollectionItsAspectRowsAreFiledUnder() throws Exception {
        String t = tenant();
        latched.set(false);
        register(t, COLL_A);
        register(t, COLL_K);
        curatorOwner(t, "1.1", "pdfs");
        String s = registered(t, "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", COLL_A);
        aspect(t, s, COLL_K, "/old/a.pdf", "file:///old/a.pdf", "filed under K");
        long k = idOf(t, COLL_K);
        latched.set(true);

        assertThat(heldAgainstAVerbDuring(t, k, () -> moves.moveDocument(t, s, "file:///new/a.pdf", "/new/a.pdf", false)))
            .as("a verb's FOR UPDATE on K is refused while the move runs").isTrue();
    }

    /** An absorbed T has a document U filed under collection K2 whose alias_of names it: the absorb re-points U, so K2's row is held. */
    @Test
    void anAbsorbHoldsTheRegistryRowOfACollectionOfADocumentThatAliasesTheAbsorbedOne() throws Exception {
        String t = tenant();
        latched.set(false);
        register(t, COLL_A);
        register(t, COLL_K);
        curatorOwner(t, "1.1", "pdfs");
        String s = registered(t, "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", COLL_A);
        String tt = registered(t, "1.1", "t", "file:///new/a.pdf", "/new/a.pdf", COLL_A);
        String u = registered(t, "1.1", "u", null, "/u.pdf", COLL_K);
        su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, tt)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(u)).execute());
        long k = idOf(t, COLL_K);
        latched.set(true);

        assertThat(heldAgainstAVerbDuring(t, k, () -> moves.moveDocument(t, s, "file:///new/a.pdf", "/new/a.pdf", false)))
            .as("a verb's FOR UPDATE on the alias row's collection is refused while the absorb runs").isTrue();
    }

    /** The fold deletes the absorbed B document's aspect rows, filed under collection K: the fold holds K's registry row. */
    @Test
    void anOwnerFoldHoldsTheRegistryRowOfACollectionTheAbsorbedDocumentsAspectRowsAreFiledUnder() throws Exception {
        String t = tenant();
        latched.set(false);
        register(t, COLL_A);
        register(t, COLL_B);
        register(t, COLL_K);
        repoOwner(t, "1.1", "proj", HASH_A, OLD_ROOT);
        registered(t, "1.1", "docs/x.md", null, "docs/x.md", COLL_A);
        repoOwner(t, "1.2", "proj-renamed", HASH_B, NEW_ROOT);
        String bx = registered(t, "1.2", "docs/x.md", null, "docs/x.md", COLL_B);
        String bxUri = su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t)).and(CATALOG_DOCUMENTS.TUMBLER.eq(bx)).fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
        aspect(t, bx, COLL_K, "docs/x.md", bxUri, "B's extraction, filed under K");
        long k = idOf(t, COLL_K);
        latched.set(true);

        assertThat(heldAgainstAVerbDuring(t, k, () -> moves.moveOwner(t, "1.1", NEW_ROOT, HASH_B, false)))
            .as("a verb's FOR UPDATE on K is refused while the fold runs").isTrue();
    }
}
