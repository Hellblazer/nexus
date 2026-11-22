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
 * nexus-mp8ys: the combined chunk-plus-owner writes ({@code manifest/write_many} and {@code
 * manifest/append}) to a collection whose catalog row carries {@code superseded_by} are refused
 * with a typed 409 {@code collection_superseded} naming the successor. Both entry points are
 * covered because the check sits in two places in {@code CombinedWriteService}: the embed-path
 * resolve and the supplied-vector validation that runs ahead of it.
 *
 * <p>The retire is committed through {@code CatalogRepository#supersedeCollection} AFTER a live
 * write has warmed the registry, so a stale cached live row would let the write through. The
 * control writes to a live sibling with the same request shape and must succeed, so a refusal
 * that fired for every collection would fail it.
 */
class CollectionSupersededCombinedWriteTest extends AtomicWriteTestBase {

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

    private Map<String, Object> writeManyBody(Fx f, String text, Map<String, Object> extraChunkFields,
                                              Map<String, Object> extraTop) {
        String chash = ch(text);
        Map<String, Object> chunk = new LinkedHashMap<>(chunk(chash, text));
        chunk.putAll(extraChunkFields);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("chunks", List.of(chunk));
        body.put("docs", List.of(doc(f.docId(), List.of(row(0, chash)))));
        body.putAll(extraTop);
        return body;
    }

    /** Warm the registry with a live write, then retire the name; returns the successor. */
    private String warmThenRetire(Fx f) throws Exception {
        Resp warm = send("/v1/catalog/manifest/write_many", writeManyBody(f, "warm-" + f.docId(), Map.of(), Map.of()));
        assertThat(warm.status()).as("the live write that warms the cache: %s", warm.body()).isEqualTo(200);
        String successor = f.collection().replace("__v1", "__v2");
        assertThat(repo.supersedeCollection(TENANT, f.collection(), successor, "")).isEqualTo(1);
        return successor;
    }

    private void assertRefusal(Resp r, Fx f, String successor) throws Exception {
        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(409);
        JsonNode j = r.json();
        assertThat(j.get("error").asText()).isEqualTo("collection_superseded");
        assertThat(j.get("reason").asText()).isEqualTo("collection_superseded");
        assertThat(j.get("collection").asText()).isEqualTo(f.collection());
        assertThat(j.get("superseded_by").asText()).isEqualTo(successor);
        assertThat(j.get("message").asText()).doesNotContain("is not registered").doesNotContain("register it first");
    }

    @Test
    void writeMany_toARetiredName_is409NamingTheSuccessor_andEmbedsAndStoresNothing() throws Exception {
        Fx f = fixture("sup-wm");
        String successor = warmThenRetire(f);
        long storedBefore = chunkCount(f.collection());
        int embedsBefore = embedder.calls.get();

        Resp r = send("/v1/catalog/manifest/write_many", writeManyBody(f, "after-retire-" + f.docId(), Map.of(), Map.of()));

        assertRefusal(r, f, successor);
        assertThat(chunkCount(f.collection())).as("no chunk strands under the tombstone").isEqualTo(storedBefore);
        assertThat(embedder.calls.get() - embedsBefore).as("a new chunk on a live name would embed").isZero();
        assertThat(chunkExists(f.collection(), ch("after-retire-" + f.docId()))).isFalse();
    }

    @Test
    void append_toARetiredName_is409NamingTheSuccessor() throws Exception {
        Fx f = fixture("sup-ap");
        String successor = warmThenRetire(f);
        String text = "append-after-retire-" + f.docId();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("doc_id", f.docId());
        body.put("chunks", List.of(chunk(ch(text), text)));
        body.put("rows", List.of(row(1, ch(text))));

        Resp r = send("/v1/catalog/manifest/append", body);

        assertRefusal(r, f, successor);
        assertThat(chunkExists(f.collection(), ch(text))).isFalse();
    }

    @Test
    void writeMany_withSuppliedVectors_toARetiredName_is409_notTheModelMismatch400() throws Exception {
        // The supplied-vector validation resolves the collection row first. A retired name must be
        // reported as retired even when the request's embedding_model would not match.
        Fx f = fixture("sup-vec");
        String successor = warmThenRetire(f);
        List<Double> vec = new ArrayList<>();
        for (int i = 0; i < 384; i++) vec.add(i == 0 ? 1.0 : 0.0);

        Resp r = send("/v1/catalog/manifest/write_many", writeManyBody(f, "vec-" + f.docId(),
            Map.of("embedding", vec), Map.of("embedding_model", "not-the-collections-model")));

        assertRefusal(r, f, successor);
    }

    @Test
    void control_aLiveSiblingAcceptsTheSameRequestShape() throws Exception {
        Fx f = fixture("sup-live");
        String text = "live-" + f.docId();

        Resp r = send("/v1/catalog/manifest/write_many", writeManyBody(f, text, Map.of(), Map.of()));

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(chunkExists(f.collection(), ch(text))).isTrue();
    }
}
