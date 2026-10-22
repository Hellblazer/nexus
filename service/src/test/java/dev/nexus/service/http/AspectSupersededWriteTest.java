// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.db.AspectRepository;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-i15oc: the aspect, highlight and extraction-queue writers refuse a collection name whose catalog
 * row carries {@code superseded_by} with the typed 409 {@code collection_superseded} nexus-mp8ys gave the
 * chunk writers. Plain {@code requireRegistered} accepted the tombstone and stranded the rows.
 *
 * <p>Each test warms the registry with a LIVE write on the same route, retires the name through
 * {@code CatalogRepository#supersedeCollection} (which must evict the cached row), repeats the write and
 * demands the exact refusal body. The fidelity-import verbs and the rename re-homes stay plain on purpose;
 * {@link #importHighlights_ontoATombstone_isStillAccepted} pins that.
 */
class AspectSupersededWriteTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();

    private AspectHandler handler;
    private AspectRepository aspects;

    @BeforeAll
    void wireHandler() {
        aspects = new AspectRepository(tenantScope);
        handler = new AspectHandler(aspects);
    }

    private record Resp(int status, String body) {
        JsonNode json() throws Exception {
            return JSON.readTree(body);
        }
    }

    private Resp post(String op, Map<String, Object> body) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create("/v1/aspects" + op), JSON.writeValueAsString(body));
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Resp(ex.status, ex.bodyString());
    }

    private Map<String, Object> aspectBody(Fx f, String sourcePath) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", f.collection());
        b.put("source_path", sourcePath);
        b.put("problem_formulation", "p");
        b.put("proposed_method", "m");
        b.put("experimental_results", "r");
        b.put("confidence", 0.9);
        b.put("extracted_at", "2026-10-22T00:00:00.000000Z");
        b.put("model_version", "v1");
        b.put("extractor_name", "scholarly-paper");
        b.put("doc_id", f.docId());
        return b;
    }

    private Map<String, Object> highlightBody(Fx f) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("doc_id", f.docId());
        b.put("collection", f.collection());
        b.put("highlights_md", "## key\nfinding");
        b.put("ingested_at", "2026-10-22T00:00:00.000000Z");
        return b;
    }

    private Map<String, Object> queueBody(Fx f, String sourcePath) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", f.collection());
        b.put("source_path", sourcePath);
        b.put("doc_id", f.docId());
        b.put("content_hash", "h");
        b.put("content", "c");
        return b;
    }

    private String retire(Fx f) {
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

    // ── refusals ────────────────────────────────────────────────────────────────

    @Test
    void upsertAspect_intoARetiredName_is409NamingTheSuccessor_andStoresNothing() throws Exception {
        Fx f = fixture("as-asp");
        Resp warm = post("/upsert", aspectBody(f, "before.pdf"));
        assertThat(warm.status()).as("the live write that warms the cache: %s", warm.body()).isEqualTo(200);
        String successor = retire(f);

        Resp r = post("/upsert", aspectBody(f, "after.pdf"));

        assertRefusal(r, f, successor);
        assertThat(aspects.getAspect(TENANT, f.collection(), "after.pdf")).isEmpty();
        assertThat(aspects.getAspect(TENANT, f.collection(), "before.pdf")).isPresent();
    }

    @Test
    void upsertHighlight_intoARetiredName_is409NamingTheSuccessor_andStoresNothing() throws Exception {
        Fx f = fixture("as-hl");
        // Warm with a live write of a DIFFERENT doc, so the refused write below is the only one for f.docId().
        Map<String, Object> warmBody = highlightBody(f);
        warmBody.put("doc_id", freshDoc("as-hl2", f.collection()));
        Resp warm = post("/highlights/upsert", warmBody);
        assertThat(warm.status()).as("the live write that warms the cache: %s", warm.body()).isEqualTo(200);
        String successor = retire(f);

        Resp r = post("/highlights/upsert", highlightBody(f));

        assertRefusal(r, f, successor);
        assertThat(aspects.getHighlight(TENANT, f.docId())).isEmpty();
    }

    @Test
    void enqueue_intoARetiredName_is409NamingTheSuccessor_andQueuesNothing() throws Exception {
        Fx f = fixture("as-q");
        Resp warm = post("/queue/enqueue", queueBody(f, "before.pdf"));
        assertThat(warm.status()).as("the live write that warms the cache: %s", warm.body()).isEqualTo(200);
        String successor = retire(f);

        Resp r = post("/queue/enqueue", queueBody(f, "after.pdf"));

        assertRefusal(r, f, successor);
    }

    @Test
    void enqueueMany_intoARetiredName_is409NamingTheSuccessor() throws Exception {
        Fx f = fixture("as-qm");
        Resp warm = post("/queue/enqueue_many", Map.of("rows", List.of(queueBody(f, "before.pdf"))));
        assertThat(warm.status()).as("the live write that warms the cache: %s", warm.body()).isEqualTo(200);
        String successor = retire(f);

        Resp r = post("/queue/enqueue_many", Map.of("rows", List.of(queueBody(f, "after.pdf"))));

        assertRefusal(r, f, successor);
    }

    // ── controls and the sites that stay plain ──────────────────────────────────

    @Test
    void control_aLiveSiblingAcceptsTheSameRequestShapes() throws Exception {
        Fx f = fixture("as-live");

        assertThat(post("/upsert", aspectBody(f, "live.pdf")).status()).isEqualTo(200);
        Resp hl = post("/highlights/upsert", highlightBody(f));
        assertThat(hl.status()).as(hl.body()).isEqualTo(200);
        assertThat(post("/queue/enqueue", queueBody(f, "live.pdf")).status()).isEqualTo(200);
        assertThat(aspects.getAspect(TENANT, f.collection(), "live.pdf")).isPresent();
        assertThat(aspects.getHighlight(TENANT, f.docId())).isPresent();
    }

    @Test
    void importHighlights_ontoATombstone_isStillAccepted() {
        // The import* verbs are fidelity imports, as the manifest UPSERT_IMPORT is: they carry tombstoned
        // collections as exported. Stays on the plain registration check (nexus-i15oc).
        Fx f = fixture("as-imp");
        retire(f);

        int n = aspects.importHighlightsBatch(TENANT, List.of(highlightBody(f)));

        assertThat(n).isEqualTo(1);
    }
}
