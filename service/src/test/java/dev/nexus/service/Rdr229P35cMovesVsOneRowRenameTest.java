// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogMoves;
import dev.nexus.service.db.CatalogMovesTestSupport;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionOneRowRenameTestSupport;
import dev.nexus.service.db.RenameGuard;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpResponse;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), follow-up 5 of the census evidence pass (bead comment of 2026-11-23 21:09), the rename half: "a real rename
 * or delete waiting behind move, absorb or fold". {@code Rdr229P35cCatalogMovesByIdTest} runs the real delete by id; this class runs the real
 * ONE-ROW RENAME on a real engine (the latch row is there, the rename route is the production one) while a document move, an absorb and an owner
 * fold are in flight. The one-row rename takes the registry rows through {@code CollectionTombstones.lockRowsOfOneRowRename}, bounded: while the
 * move holds the registry row of a collection it writes in (FOR KEY SHARE, from the lock set {@code CatalogMoves.collectionIdsOf} builds, by the
 * aspect, highlight and alias arm), the rename of THAT collection does not run beside it: it answers 503 {@code rename_busy} within the bound and
 * changes nothing. A move whose lock set missed the arm would let the rename through (200) and relabel a collection the move is writing in.
 *
 * <p>The bound is set to 1 s for the test (the production bound is 10 s) and restored.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cMovesVsOneRowRenameTest {

    private static final String OLD_ROOT = "/old/p35cr";
    private static final String NEW_ROOT = "/new/p35cr";

    Rdr229RenameEngine engine;
    CatalogRepository repo;
    AspectRepository aspects;
    CatalogMoves moves;
    int n;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
        repo = new CatalogRepository(engine.scope(), () -> true, RenameGuard.open());
        aspects = new AspectRepository(engine.scope());
        moves = new CatalogMoves(repo);
    }

    @AfterAll
    void stop() {
        if (engine != null) engine.close();
    }

    /** A tenant and its labels, unique to it: the engine's registry caches are process-wide, so two tenants bearing one label would meet. */
    private record Tenant(String name, String token, String a, String b, String k, String k2) {
    }

    private Tenant tenant() throws Exception {
        int i = ++n;
        String t = "p35c-mvr-" + i;
        String tail = "__minilm-l6-v2-384__v1";
        return new Tenant(t, engine.newTenant(t), "knowledge__p35crename-a" + i + tail, "knowledge__p35crename-b" + i + tail,
            "knowledge__p35crename-k" + i + tail, "knowledge__p35crename-k" + i + "x" + tail);
    }

    /**
     * A registered collection holding two chunks (and a ring row): a collection that holds nothing is a ghost the tenant's first catalog request
     * sweeps (RDR-204), which would remove the rows the test is about before the move runs.
     */
    private void register(String tenant, String label) {
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, label);
            return null;
        });
    }

    private long idOf(String tenant, String label) {
        return engine.<Long>su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
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

    private void aspect(String t, String docId, String coll, String sourcePath, String sourceUri, String text) {
        long id = idOf(t, coll);
        engine.su(ctx -> ctx.insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, t).set(DOCUMENT_ASPECTS.COLLECTION, coll).set(DOCUMENT_ASPECTS.COLLECTION_ID, id)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, sourcePath).set(DOCUMENT_ASPECTS.SOURCE_URI, sourceUri).set(DOCUMENT_ASPECTS.DOC_ID, docId)
            .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION, text).set(DOCUMENT_ASPECTS.CONFIDENCE, 0.9)
            .set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.now()).set(DOCUMENT_ASPECTS.MODEL_VERSION, "v1")
            .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test").execute());
    }

    /**
     * Runs {@code move}; from inside its transaction (after its last write) the one-row rename of {@code label} to a new label is sent to the real
     * route, on a thread of its own with a deadline (a rename that waited on something the move holds would otherwise hang the move behind it).
     * Returns what the route answered.
     */
    private HttpResponse<String> renameDuring(Tenant t, String label, Runnable move) throws Exception {
        AtomicReference<CompletableFuture<HttpResponse<String>>> sent = new AtomicReference<>();
        java.util.concurrent.atomic.AtomicInteger fired = new java.util.concurrent.atomic.AtomicInteger();
        try (var bound = CollectionOneRowRenameTestSupport.rowLockTimeoutMs("1000");
             var hook = CatalogMovesTestSupport.afterWrites(() -> {
                 if (fired.getAndIncrement() > 0) return;   // a move may run its transaction again; the rename is sent once
                 var f = CompletableFuture.supplyAsync(() -> {
                     try {
                         return engine.call("POST", "/v1/catalog/collections/rename", t.token(),
                             "{\"old_name\":\"" + label + "\",\"new_name\":\"" + t.k2() + "\"}");
                     } catch (Exception e) {
                         throw new IllegalStateException(e);
                     }
                 });
                 sent.set(f);
                 try {
                     f.get(20, TimeUnit.SECONDS);
                 } catch (java.util.concurrent.TimeoutException | InterruptedException | java.util.concurrent.ExecutionException e) {
                     // reported below, from the future
                 }
             })) {
            move.run();
        }
        assertThat(sent.get()).as("non-vacuity: the hook ran inside the move (fired " + fired.get() + " times)").isNotNull();
        assertThat(sent.get().isDone()).as("the rename route answered within its bound and did not hang behind the move").isTrue();
        return sent.get().get();
    }

    private void assertRenameRefusedBusy(Tenant t, String label, long id, HttpResponse<String> r) throws Exception {
        assertThat(r.statusCode()).as("the rename waits for the registry row the move holds, then is refused: " + r.body()).isEqualTo(503);
        assertThat(Rdr229RenameEngine.MAPPER.readTree(r.body()).path("reason").asText()).isEqualTo("rename_busy");
        assertThat(idOf(t.name(), label)).as("and nothing was renamed: the collection still bears its label").isEqualTo(id);
    }

    @Test
    void aOneRowRenameOfACollectionTheDocumentMoveWritesInIsRefusedBusy_viaAnAspectRow() throws Exception {
        Tenant t = tenant();
        register(t.name(), t.a());
        register(t.name(), t.k());
        curatorOwner(t.name(), "1.1", "pdfs");
        String s = registered(t.name(), "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", t.a());
        aspect(t.name(), s, t.k(), "/old/a.pdf", "file:///old/a.pdf", "filed under K");
        long k = idOf(t.name(), t.k());

        var r = renameDuring(t, t.k(), () -> moves.moveDocument(t.name(), s, "file:///new/a.pdf", "/new/a.pdf", false));

        assertRenameRefusedBusy(t, t.k(), k, r);
    }

    @Test
    void aOneRowRenameOfACollectionTheDocumentMoveWritesInIsRefusedBusy_viaAHighlightRow() throws Exception {
        Tenant t = tenant();
        register(t.name(), t.a());
        register(t.name(), t.k());
        curatorOwner(t.name(), "1.1", "pdfs");
        String s = registered(t.name(), "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", t.a());
        highlight(t.name(), s, t.k(), "file:///old/a.pdf", "filed under K");
        long k = idOf(t.name(), t.k());

        var r = renameDuring(t, t.k(), () -> moves.moveDocument(t.name(), s, "file:///new/a.pdf", "/new/a.pdf", false));

        assertRenameRefusedBusy(t, t.k(), k, r);
    }

    @Test
    void aOneRowRenameOfACollectionTheAbsorbWritesInIsRefusedBusy_viaAnAliasingDocument() throws Exception {
        Tenant t = tenant();
        register(t.name(), t.a());
        register(t.name(), t.k());
        curatorOwner(t.name(), "1.1", "pdfs");
        String s = registered(t.name(), "1.1", "s", "file:///old/a.pdf", "/old/a.pdf", t.a());
        String tt = registered(t.name(), "1.1", "t", "file:///new/a.pdf", "/new/a.pdf", t.a());
        String u = registered(t.name(), "1.1", "u", null, "/u.pdf", t.k());
        engine.su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, tt)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t.name())).and(CATALOG_DOCUMENTS.TUMBLER.eq(u)).execute());
        long k = idOf(t.name(), t.k());

        var r = renameDuring(t, t.k(), () -> moves.moveDocument(t.name(), s, "file:///new/a.pdf", "/new/a.pdf", false));

        assertRenameRefusedBusy(t, t.k(), k, r);
    }

    @Test
    void aOneRowRenameOfACollectionTheOwnerFoldWritesInIsRefusedBusy() throws Exception {
        Tenant t = tenant();
        register(t.name(), t.a());
        register(t.name(), t.b());
        register(t.name(), t.k());
        repoOwner(t.name(), "1.1", "proj", "aaaa3501", OLD_ROOT);
        registered(t.name(), "1.1", "docs/x.md", null, "docs/x.md", t.a());
        repoOwner(t.name(), "1.2", "proj-renamed", "bbbb3502", NEW_ROOT);
        String bx = registered(t.name(), "1.2", "docs/x.md", null, "docs/x.md", t.b());
        String bxUri = engine.<String>su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t.name())).and(CATALOG_DOCUMENTS.TUMBLER.eq(bx)).fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
        aspect(t.name(), bx, t.k(), "docs/x.md", bxUri, "B's extraction, filed under K");
        long k = idOf(t.name(), t.k());

        var r = renameDuring(t, t.k(), () -> moves.moveOwner(t.name(), "1.1", NEW_ROOT, "bbbb3502", false));

        assertRenameRefusedBusy(t, t.k(), k, r);
    }

    /** Non-vacuity of the four above: the same rename with no move in flight succeeds, so the 503 is the move's. */
    @Test
    void theSameRenameWithNoMoveInFlightSucceeds() throws Exception {
        Tenant t = tenant();
        register(t.name(), t.k());
        long k = idOf(t.name(), t.k());

        var r = engine.call("POST", "/v1/catalog/collections/rename", t.token(),
            "{\"old_name\":\"" + t.k() + "\",\"new_name\":\"" + t.k2() + "\"}");

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(idOf(t.name(), t.k2())).as("the registry row keeps its id under the new label").isEqualTo(k);
    }
}
