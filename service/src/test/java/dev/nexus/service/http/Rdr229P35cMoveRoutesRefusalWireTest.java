// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), the wire of the move routes' by-id refusal of a collection the REQUEST DID NOT NAME.
 * {@code POST /v1/catalog/documents/move} and {@code POST /v1/catalog/owners/move} name a tumbler or an owner; from gate 3
 * {@code CatalogMoves.lockCollectionsOfDocuments} locks, by registry id, every collection the move writes in: the documents' own, and
 * their aspect, highlight and alias rows'. A registry row that is retired answers 409 {@code collection_superseded}; an id with no
 * registry row answers 404 {@code unknown_collection_id}. Both are the bodies the by-id routes send, here reached through
 * {@link CatalogHandler}'s ladder ({@code HttpUtil#sendTypedDbError}), and the move writes nothing.
 *
 * <p>{@code Rdr229P35cCatalogMovesByIdTest} pins the refusal below the route (it asserts a RuntimeException); this class pins the status and body a
 * client sees. Each refusal has a control in the same fixture with the collection live, which answers 200 and moves, so the 409 and the
 * 404 are the retired row's and the missing row's answers and not the fixture's.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cMoveRoutesRefusalWireTest {

    private static final String SVC_ROLE = "svc_p35c_move_wire";
    private static final String SVC_PASS = "svc_p35c_move_wire_pass";
    private static final String COLL_A = "code__p35cwire-a__voyage-code-3__v1";
    private static final String COLL_K = "code__p35cwire-k__voyage-code-3__v1";
    private static final String COLL_SUCCESSOR = "code__p35cwire-s__voyage-code-3__v1";
    private static final long NO_SUCH_ID = 987654321L;
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String OLD_URI = "file:///old/a.pdf";
    private static final String NEW_URI = "file:///new/a.pdf";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    final AtomicBoolean latched = new AtomicBoolean();
    CatalogRepository repo;
    AspectRepository aspects;
    CatalogHandler handler;
    int tenantSeq;

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
        repo = new CatalogRepository(scope, latched::get, RenameGuard.open());
        aspects = new AspectRepository(scope);
        handler = new CatalogHandler(repo);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ---- the document move route ----------------------------------------------------------------------------------------------------

    @Test
    void documentsMove_controlWithEveryCollectionLiveAnswers200AndMoves() throws Exception {
        var f = fixture();
        long k = f.register(COLL_K);
        f.aspectUnder(f.s, COLL_K, k);
        f.highlightUnder(f.s, COLL_K);
        latched.set(true);

        var r = f.moveDocument();
        assertThat(r.status).as(r.body()).isEqualTo(200);
        assertThat(f.sourceUriOf(f.s)).as("the control moved").isEqualTo(NEW_URI);
    }

    @Test
    void documentsMove_aRetiredCollectionOfTheDocumentIs409CollectionSuperseded() throws Exception {
        var f = fixture();
        long a = f.idOf(COLL_A);
        f.retire(COLL_A);
        latched.set(true);

        var r = f.moveDocument();
        assertSuperseded(r, a, COLL_A);
        assertThat(f.sourceUriOf(f.s)).as("nothing moved").isEqualTo(OLD_URI);
    }

    @Test
    void documentsMove_aDryRunIsRefusedToo() throws Exception {
        var f = fixture();
        long a = f.idOf(COLL_A);
        f.retire(COLL_A);
        latched.set(true);

        var r = send(f.tenant, "/v1/catalog/documents/move", "{\"tumbler\":\"" + f.s + "\",\"new_source_uri\":\"" + NEW_URI
            + "\",\"new_file_path\":\"/new/a.pdf\",\"dry_run\":true}");
        assertSuperseded(r, a, COLL_A);
    }

    @Test
    void documentsMove_aRetiredCollectionOfTheDocumentsAspectRowIs409CollectionSuperseded() throws Exception {
        var f = fixture();
        long k = f.register(COLL_K);
        f.aspectUnder(f.s, COLL_K, k);
        f.retire(COLL_K);
        latched.set(true);

        var r = f.moveDocument();
        assertSuperseded(r, k, COLL_K);
        assertThat(f.sourceUriOf(f.s)).as("nothing moved").isEqualTo(OLD_URI);
        assertThat(f.aspectSourceUri(f.s)).as("the aspect row was not re-pointed").isEqualTo(OLD_URI);
    }

    @Test
    void documentsMove_aRetiredCollectionOfTheDocumentsHighlightRowIs409CollectionSuperseded() throws Exception {
        var f = fixture();
        long k = f.register(COLL_K);
        f.highlightUnder(f.s, COLL_K);
        f.retire(COLL_K);
        latched.set(true);

        var r = f.moveDocument();
        assertSuperseded(r, k, COLL_K);
        assertThat(f.sourceUriOf(f.s)).as("nothing moved").isEqualTo(OLD_URI);
    }

    @Test
    void documentsMove_aRetiredCollectionOfADocumentThatAliasesTheAbsorbedOneIs409CollectionSuperseded() throws Exception {
        var f = fixture();
        String t = f.registerDoc("t", NEW_URI, "/new/a.pdf", COLL_A);
        long k = f.register(COLL_K);
        String u = f.registerDoc("u", null, "/u.pdf", COLL_K);
        f.su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, t)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(f.tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(u)).execute());
        f.retire(COLL_K);
        latched.set(true);

        var r = f.moveDocument();
        assertSuperseded(r, k, COLL_K);
        assertThat(f.sourceUriOf(f.s)).as("nothing moved").isEqualTo(OLD_URI);
        assertThat(f.isLive(t)).as("the absorbed row was not trashed").isTrue();
    }

    @Test
    void documentsMove_aCollectionIdWithNoRegistryRowIs404UnknownCollectionId() throws Exception {
        var f = fixture();
        f.su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, NO_SUCH_ID)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(f.tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(f.s)).execute());
        latched.set(true);

        var r = f.moveDocument();
        assertUnknown(r, NO_SUCH_ID);
        assertThat(f.sourceUriOf(f.s)).as("nothing moved").isEqualTo(OLD_URI);
    }

    // ---- the owner move route -------------------------------------------------------------------------------------------------------

    @Test
    void ownersMove_controlWithEveryCollectionLiveAnswers200AndMoves() throws Exception {
        var f = fixture();
        f.repoOwnerWithDoc();
        latched.set(true);

        var r = f.moveOwner();
        assertThat(r.status).as(r.body()).isEqualTo(200);
        assertThat(f.sourceUriOf(f.ownerDoc)).as("the control moved").startsWith("file:///new/root");
        assertThat(f.ownerRoot()).isEqualTo("/new/root");
    }

    @Test
    void ownersMove_aRetiredCollectionOfTheOwnersDocumentIs409CollectionSuperseded() throws Exception {
        var f = fixture();
        f.repoOwnerWithDoc();
        long a = f.idOf(COLL_A);
        f.retire(COLL_A);
        latched.set(true);

        var r = f.moveOwner();
        assertSuperseded(r, a, COLL_A);
        assertThat(f.sourceUriOf(f.ownerDoc)).as("nothing moved").isEqualTo(f.ownerUri);
        assertThat(f.ownerRoot()).as("the owner's root was not rewritten").isEqualTo("/old/root");
    }

    @Test
    void ownersMove_aRetiredCollectionOfTheOwnersDocumentsAspectRowIs409CollectionSuperseded() throws Exception {
        var f = fixture();
        f.repoOwnerWithDoc();
        long k = f.register(COLL_K);
        f.aspectUnder(f.ownerDoc, COLL_K, k);
        f.retire(COLL_K);
        latched.set(true);

        var r = f.moveOwner();
        assertSuperseded(r, k, COLL_K);
        assertThat(f.sourceUriOf(f.ownerDoc)).as("nothing moved").isEqualTo(f.ownerUri);
        assertThat(f.ownerRoot()).as("the owner's root was not rewritten").isEqualTo("/old/root");
    }

    @Test
    void ownersMove_aCollectionIdWithNoRegistryRowIs404UnknownCollectionId() throws Exception {
        var f = fixture();
        f.repoOwnerWithDoc();
        f.su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, NO_SUCH_ID)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(f.tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(f.ownerDoc)).execute());
        latched.set(true);

        var r = f.moveOwner();
        assertUnknown(r, NO_SUCH_ID);
        assertThat(f.sourceUriOf(f.ownerDoc)).as("nothing moved").isEqualTo(f.ownerUri);
        assertThat(f.ownerRoot()).as("the owner's root was not rewritten").isEqualTo("/old/root");
    }

    // ---- assertions -----------------------------------------------------------------------------------------------------------------

    @SuppressWarnings("unchecked")
    private static void assertSuperseded(Reply r, long id, String label) throws Exception {
        assertThat(r.status).as(r.body()).isEqualTo(409);
        Map<String, Object> body = MAPPER.readValue(r.body(), Map.class);
        assertThat(body).containsEntry("reason", "collection_superseded").containsEntry("error", "collection_superseded")
            .containsEntry("collection", label);
        assertThat(((Number) body.get("collection_id")).longValue()).isEqualTo(id);
    }

    @SuppressWarnings("unchecked")
    private static void assertUnknown(Reply r, long id) throws Exception {
        assertThat(r.status).as(r.body()).isEqualTo(404);
        Map<String, Object> body = MAPPER.readValue(r.body(), Map.class);
        assertThat(body).containsEntry("reason", "unknown_collection_id");
        assertThat(((Number) body.get("collection_id")).longValue()).isEqualTo(id);
    }

    // ---- fixture --------------------------------------------------------------------------------------------------------------------

    private Fixture fixture() {
        latched.set(false);
        var f = new Fixture("p35c-move-wire-" + (++tenantSeq));
        f.register(COLL_A);
        f.curatorOwner();
        f.s = f.registerDoc("s", OLD_URI, "/old/a.pdf", COLL_A);
        return f;
    }

    private record Reply(int status, String body) {
    }

    /** One tenant: curator owner 1.1 with document S in COLL_A (live), built before the latch. */
    private final class Fixture {
        final String tenant;
        String s;
        String ownerDoc;
        String ownerUri;

        Fixture(String tenant) {
            this.tenant = tenant;
        }

        <T> T su(java.util.function.Function<DSLContext, T> work) {
            try (Connection c = pg.createConnection("")) {
                c.setAutoCommit(true);
                return work.apply(DSL.using(c, SQLDialect.POSTGRES));
            } catch (java.sql.SQLException e) {
                throw new IllegalStateException(e);
            }
        }

        long register(String label) {
            su(ctx -> {
                PgContainerHelper.insertCollection(ctx, tenant, label);
                return null;
            });
            return idOf(label);
        }

        long idOf(String label) {
            return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        }

        /** Retires {@code label}'s registry row: it names a live successor, as a supersede leaves it. */
        void retire(String label) {
            register(COLL_SUCCESSOR);
            long successor = idOf(COLL_SUCCESSOR);
            su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, COLL_SUCCESSOR)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, successor)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label)).execute());
        }

        void curatorOwner() {
            repo.upsertOwner(tenant, Map.of("tumbler_prefix", "1.1", "name", "pdfs", "owner_type", "curator"));
        }

        String registerDoc(String title, String sourceUri, String filePath, String coll) {
            Map<String, Object> f = new LinkedHashMap<>();
            f.put("title", title);
            f.put("file_path", filePath);
            f.put("content_type", "code");
            f.put("corpus", "code");
            f.put("physical_collection", coll);
            if (sourceUri != null) f.put("source_uri", sourceUri);
            return repo.registerDocument(tenant, "1.1", f);
        }

        /** Repo owner 1.2 rooted at /old/root, with one root-derived document in COLL_A. */
        void repoOwnerWithDoc() {
            Map<String, Object> o = new LinkedHashMap<>();
            o.put("tumbler_prefix", "1.2");
            o.put("name", "proj");
            o.put("owner_type", "repo");
            o.put("repo_hash", "aaaa3511");
            o.put("repo_root", "/old/root");
            repo.upsertOwner(tenant, o);
            Map<String, Object> f = new LinkedHashMap<>();
            f.put("title", "docs/x.md");
            f.put("file_path", "docs/x.md");
            f.put("content_type", "code");
            f.put("corpus", "code");
            f.put("physical_collection", COLL_A);
            ownerDoc = repo.registerDocument(tenant, "1.2", f);   // a repo owner's document: the engine derives its URI from the root
            ownerUri = sourceUriOf(ownerDoc);
            assertThat(ownerUri).as("setup: the derived URI is under the old root").startsWith("file:///old/root");
        }

        void aspectUnder(String docId, String coll, long collId) {
            String uri = sourceUriOf(docId);
            su(ctx -> ctx.insertInto(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.TENANT_ID, tenant).set(DOCUMENT_ASPECTS.COLLECTION, coll).set(DOCUMENT_ASPECTS.COLLECTION_ID, collId)
                .set(DOCUMENT_ASPECTS.SOURCE_PATH, "/old/a.pdf").set(DOCUMENT_ASPECTS.SOURCE_URI, uri).set(DOCUMENT_ASPECTS.DOC_ID, docId)
                .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION, "filed under " + coll).set(DOCUMENT_ASPECTS.CONFIDENCE, 0.9)
                .set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.now()).set(DOCUMENT_ASPECTS.MODEL_VERSION, "v1")
                .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test").execute());
        }

        void highlightUnder(String docId, String coll) {
            Map<String, Object> b = new LinkedHashMap<>();
            b.put("doc_id", docId);
            b.put("collection", coll);
            b.put("source_uri", sourceUriOf(docId));
            b.put("highlights_md", "filed under " + coll);
            b.put("ingested_at", OffsetDateTime.now().toString());
            aspects.upsertHighlight(tenant, b);
        }

        String sourceUriOf(String docId) {
            return su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                .fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
        }

        String aspectSourceUri(String docId) {
            return su(ctx -> ctx.select(DOCUMENT_ASPECTS.SOURCE_URI).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant)).and(DOCUMENT_ASPECTS.DOC_ID.eq(docId))
                .fetchOne(DOCUMENT_ASPECTS.SOURCE_URI));
        }

        boolean isLive(String docId) {
            return su(ctx -> ctx.select(CATALOG_DOCUMENTS.DELETED_AT).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                .fetchOne(CATALOG_DOCUMENTS.DELETED_AT)) == null;
        }

        String ownerRoot() {
            return su(ctx -> ctx.select(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS.REPO_ROOT)
                .from(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS)
                .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS.TENANT_ID.eq(tenant))
                .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS.TUMBLER_PREFIX.eq("1.2"))
                .fetchOne(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS.REPO_ROOT));
        }

        Reply moveDocument() throws Exception {
            return send(tenant, "/v1/catalog/documents/move", "{\"tumbler\":\"" + s + "\",\"new_source_uri\":\"" + NEW_URI
                + "\",\"new_file_path\":\"/new/a.pdf\",\"dry_run\":false}");
        }

        Reply moveOwner() throws Exception {
            return send(tenant, "/v1/catalog/owners/move",
                "{\"owner\":\"1.2\",\"new_root\":\"/new/root\",\"new_repo_hash\":\"bbbb3512\",\"dry_run\":false}");
        }
    }

    private Reply send(String tenant, String path, String body) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create(path), body);
        RequestContext.set(new RequestContext.Principal(tenant, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Reply(ex.status, ex.bodyString());
    }

    /** Minimal {@link HttpExchange} that captures the response status + body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
