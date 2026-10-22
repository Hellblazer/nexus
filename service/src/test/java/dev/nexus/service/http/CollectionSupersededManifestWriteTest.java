// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-mp8ys, review fold-in: the manifest writes that carry NO {@code chunks} key never pass through
 * {@code CombinedWriteService#resolveChunks}, so the refusal there does not cover them. They reach {@code
 * CatalogRepository#insertManifestChunkRows} directly, and a manifest row written under a retired
 * (superseded) name strands just as a chunk does. This class pins, per route, that a manifest-only write to
 * a retired name is a typed 409 {@code collection_superseded} and leaves the document's manifest untouched:
 * {@code manifest/write}, {@code manifest/append} (plain and with {@code sweep_chashes}), {@code
 * manifest/write_many} and {@code manifest/append_many}. It also pins {@code append_many} WITH chunks (the
 * combined path, which had no route test), that the fidelity-preserving import still carries a tombstoned
 * collection, and that a revive through the real registration path ({@code CatalogRepository#upsertCollection})
 * makes the name writable again.
 *
 * <p>Every refusal test seeds the document with a position-0 row through a live write first, so a refusal
 * that left the manifest half-replaced (the delete committed, the insert refused) would show as a changed
 * position set. The live-sibling controls send the same request shapes and must succeed, so a refusal that
 * fired for every collection would fail them.
 */
class CollectionSupersededManifestWriteTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();

    private CatalogHandler handler;

    @BeforeAll
    void wireHandler() {
        handler = new CatalogHandler(repo, svc);
    }

    private record Resp(int status, String body) {
        JsonNode json() throws Exception {
            return JSON.readTree(body);
        }
    }

    private Resp send(String path, Map<String, Object> body) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create(path), JSON.writeValueAsString(body));
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Resp(ex.status, ex.bodyString());
    }

    /** A collection, a registered document, and the chash of a chunk the document already references. */
    private record Seeded(Fx fx, String chash) {}

    /** Live write_many: chunk plus a position-0 row. Warms the registry cache with the live row. */
    private Seeded seed(String tag) throws Exception {
        Fx f = fixture(tag);
        String text = "seed-" + f.docId();
        String chash = ch(text);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("chunks", List.of(chunk(chash, text)));
        body.put("docs", List.of(doc(f.docId(), List.of(row(0, chash)))));
        Resp r = send("/v1/catalog/manifest/write_many", body);
        assertThat(r.status()).as("the live seed write: %s", r.body()).isEqualTo(200);
        assertThat(positions(f.docId())).containsExactly("0");
        return new Seeded(f, chash);
    }

    private String retire(Seeded s) {
        String successor = s.fx().collection().replace("__v1", "__v2");
        assertThat(repo.supersedeCollection(TENANT, s.fx().collection(), successor, "")).isEqualTo(1);
        return successor;
    }

    private List<String> positions(String docId) {
        List<String> out = new ArrayList<>();
        for (var r : repo.getManifest(TENANT, docId)) {
            out.add(String.valueOf(r.get("position")));
        }
        return out;
    }

    private void assertRefusal(Resp r, Seeded s, String successor) throws Exception {
        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(409);
        JsonNode j = r.json();
        assertThat(j.get("error").asText()).isEqualTo("collection_superseded");
        assertThat(j.get("reason").asText()).isEqualTo("collection_superseded");
        assertThat(j.get("collection").asText()).isEqualTo(s.fx().collection());
        assertThat(j.get("superseded_by").asText()).isEqualTo(successor);
        assertThat(positions(s.fx().docId())).as("the refused write changed nothing in the manifest")
            .containsExactly("0");
    }

    private Map<String, Object> manifestBody(Seeded s, int position) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", s.fx().collection());
        body.put("doc_id", s.fx().docId());
        body.put("rows", List.of(row(position, s.chash())));
        return body;
    }

    private Map<String, Object> manyBody(Seeded s, int position) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", s.fx().collection());
        body.put("docs", List.of(doc(s.fx().docId(), List.of(row(position, s.chash())))));
        return body;
    }

    @Test
    void manifestWrite_toARetiredName_is409_andTheReplaceRollsBack() throws Exception {
        Seeded s = seed("sup-mw");
        String successor = retire(s);

        Resp r = send("/v1/catalog/manifest/write", manifestBody(s, 5));

        assertRefusal(r, s, successor);
    }

    @Test
    void manifestAppend_withoutChunks_toARetiredName_is409() throws Exception {
        Seeded s = seed("sup-ma");
        String successor = retire(s);

        Resp r = send("/v1/catalog/manifest/append", manifestBody(s, 5));

        assertRefusal(r, s, successor);
    }

    @Test
    void manifestAppend_withSweepChashes_toARetiredName_is409() throws Exception {
        Seeded s = seed("sup-mas");
        String successor = retire(s);
        Map<String, Object> body = manifestBody(s, 5);
        body.put("sweep_chashes", List.of(ch("some-dropped-chunk")));

        Resp r = send("/v1/catalog/manifest/append", body);

        assertRefusal(r, s, successor);
    }

    @Test
    void writeMany_withoutChunks_toARetiredName_is409_notAPerDocumentFailureWith200() throws Exception {
        // write_many catches a per-document failure into failed_doc_ids and answers 200, so a refusal raised
        // only inside the document's transaction would read as a partial success. The request-level check
        // is what makes this a 409.
        Seeded s = seed("sup-wmn");
        String successor = retire(s);

        Resp r = send("/v1/catalog/manifest/write_many", manyBody(s, 5));

        assertRefusal(r, s, successor);
    }

    @Test
    void appendMany_withoutChunks_toARetiredName_is409_notAPerDocumentFailureWith200() throws Exception {
        Seeded s = seed("sup-amn");
        String successor = retire(s);

        Resp r = send("/v1/catalog/manifest/append_many", manyBody(s, 5));

        assertRefusal(r, s, successor);
    }

    @Test
    void appendMany_withChunks_toARetiredName_is409_andStoresNothing() throws Exception {
        Seeded s = seed("sup-amc");
        String successor = retire(s);
        String text = "append-many-after-retire-" + s.fx().docId();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", s.fx().collection());
        body.put("chunks", List.of(chunk(ch(text), text)));
        body.put("docs", List.of(doc(s.fx().docId(), List.of(row(5, ch(text))))));
        long storedBefore = chunkCount(s.fx().collection());
        int embedsBefore = embedder.calls.get();

        Resp r = send("/v1/catalog/manifest/append_many", body);

        assertRefusal(r, s, successor);
        assertThat(chunkCount(s.fx().collection())).as("no chunk strands under the tombstone").isEqualTo(storedBefore);
        assertThat(chunkExists(s.fx().collection(), ch(text))).isFalse();
        assertThat(embedder.calls.get() - embedsBefore).as("nothing is embedded or billed").isZero();
    }

    @Test
    void control_aLiveSiblingAcceptsEveryManifestOnlyShape() throws Exception {
        Seeded s = seed("sup-live");

        assertThat(send("/v1/catalog/manifest/append", manifestBody(s, 1)).status()).isEqualTo(200);
        assertThat(send("/v1/catalog/manifest/write_many", manyBody(s, 2)).status()).isEqualTo(200);
        assertThat(send("/v1/catalog/manifest/append_many", manyBody(s, 3)).status()).isEqualTo(200);
        assertThat(send("/v1/catalog/manifest/write", manifestBody(s, 4)).status()).isEqualTo(200);
        assertThat(positions(s.fx().docId())).containsExactly("4");
    }

    @Test
    void theFidelityImport_stillCarriesATombstonedCollection() throws Exception {
        // UPSERT_IMPORT is the ETL / recovery-bundle path: it writes manifest rows under whatever collection the
        // bundle names, including a retired one. It must keep the plain registration check, not the refusal.
        Seeded s = seed("sup-imp");
        retire(s);

        Resp r = send("/v1/catalog/import/chunk", manifestBody(s, 7));

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(positions(s.fx().docId())).containsExactly("0", "7");
    }

    @Test
    void aRevive_throughTheRealRegistrationPath_makesTheNameWritableAgain() throws Exception {
        // Not a raw UPDATE of superseded_by: CatalogRepository#upsertCollection is what POST
        // /collections/upsert runs, and its ON CONFLICT arm is what clears the tombstone. The retire warmed
        // and then evicted the live row; the revive touches no cache, which works only because a tombstone
        // is never cached.
        Seeded s = seed("sup-rev");
        String successor = retire(s);
        assertRefusal(send("/v1/catalog/manifest/append", manifestBody(s, 5)), s, successor);

        repo.upsertCollection(TENANT, Map.of(
            "name", s.fx().collection(), "content_type", "code", "owner_id", TENANT));

        Resp after = send("/v1/catalog/manifest/append", manifestBody(s, 5));
        assertThat(after.status()).as("body: %s", after.body()).isEqualTo(200);
        assertThat(positions(s.fx().docId())).containsExactly("0", "5");

        String text = "chunk-after-revive-" + s.fx().docId();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", s.fx().collection());
        body.put("chunks", List.of(chunk(ch(text), text)));
        body.put("docs", List.of(doc(s.fx().docId(), List.of(row(6, ch(text))))));
        Resp combined = send("/v1/catalog/manifest/write_many", body);
        assertThat(combined.status()).as("body: %s", combined.body()).isEqualTo(200);
        assertThat(chunkExists(s.fx().collection(), ch(text))).isTrue();
    }
}
