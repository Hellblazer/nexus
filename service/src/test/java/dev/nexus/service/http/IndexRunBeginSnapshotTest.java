// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

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
 * RDR-223 (bead nexus-z0o2p.10): {@code POST /v1/catalog/index-run/begin} with
 * {@code snapshot_manifest:true} returns the document's PRE-RUN manifest, read in the same
 * transaction as the {@code indexing} stamp: {@code prior_chashes} (distinct, position order) and
 * {@code prior_count} (manifest ROW count). A multi-batch writer computes its deferred sweep from
 * it, so a lost-and-resent first batch cannot lose the previous run's tail.
 */
class IndexRunBeginSnapshotTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();

    private CatalogHandler handler;

    @BeforeAll
    void wireHandler() {   // runs after AtomicWriteTestBase#startAll (superclass lifecycle methods first)
        handler = new CatalogHandler(repo, svc);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> send(String path, Map<String, Object> body) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create(path), JSON.writeValueAsString(body));
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        assertThat(ex.status).as("%s -> %s", path, ex.bodyString()).isEqualTo(200);
        return JSON.readValue(ex.bodyString(), Map.class);
    }

    private void writeManifest(Fx f, List<String> texts) throws Exception {
        List<Map<String, Object>> rows = new ArrayList<>();
        List<Map<String, Object>> chunks = new ArrayList<>();
        for (int i = 0; i < texts.size(); i++) {
            rows.add(row(i, ch(texts.get(i))));
            chunks.add(chunk(ch(texts.get(i)), texts.get(i)));
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("docs", List.of(doc(f.docId(), rows)));
        body.put("chunks", chunks);
        send("/v1/catalog/manifest/write_many", body);
    }

    private Map<String, Object> begin(Fx f, boolean snapshot) throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("doc_id", f.docId());
        body.put("content_hash", "h");
        body.put("run_id", "r");
        body.put("collection", f.collection());
        if (snapshot) body.put("snapshot_manifest", true);
        return send("/v1/catalog/index-run/begin", body);
    }

    @Test
    void snapshotHoldsDistinctChashesInPositionOrderAndTheRowCount() throws Exception {
        Fx f = fixture("snap");
        // s1 appears at positions 0 and 2: two rows, one distinct chash.
        writeManifest(f, List.of("snap/s1", "snap/s2", "snap/s1", "snap/s3"));

        Map<String, Object> resp = begin(f, true);

        assertThat(resp.get("ok")).isEqualTo(true);
        assertThat(resp.get("prior_chashes")).isEqualTo(List.of(ch("snap/s1"), ch("snap/s2"), ch("snap/s3")));
        assertThat(resp.get("prior_count")).isEqualTo(4);
    }

    @Test
    void withoutTheFlagTheResponseIsExactlyOkTrue() throws Exception {
        Fx f = fixture("plain");
        writeManifest(f, List.of("plain/p1", "plain/p2"));

        assertThat(begin(f, false)).isEqualTo(Map.of("ok", true));
    }

    @Test
    void aSnapshotOfANewDocumentIsEmpty() throws Exception {
        Fx f = fixture("empty");

        Map<String, Object> resp = begin(f, true);

        assertThat(resp.get("prior_chashes")).isEqualTo(List.of());
        assertThat(resp.get("prior_count")).isEqualTo(0);
    }

    @Test
    void aResentBeginBeforeAnyWriteSnapshotsTheSamePreRunManifest() throws Exception {
        Fx f = fixture("resend");
        writeManifest(f, List.of("resend/r1", "resend/r2"));

        Map<String, Object> first = begin(f, true);
        Map<String, Object> resent = begin(f, true);

        // Non-vacuity: the snapshot both times is the real manifest, not two empty ones.
        assertThat(first.get("prior_chashes")).isEqualTo(List.of(ch("resend/r1"), ch("resend/r2")));
        assertThat(first.get("prior_count")).isEqualTo(2);
        assertThat(resent).isEqualTo(first);
    }

    @Test
    void aSnapshotBeginStillStampsTheDocumentIndexing() throws Exception {
        Fx f = fixture("stamp");
        writeManifest(f, List.of("stamp/s1"));

        begin(f, true);

        assertThat(repo.getDocument(TENANT, f.docId()).get("index_state")).isEqualTo("indexing");
    }

    @Test
    void aSnapshotBeginOnATombstonedDocumentIsRefusedAndReturnsNoManifest() throws Exception {
        Fx f = fixture("tomb");
        writeManifest(f, List.of("tomb/t1"));
        repo.deleteDocument(TENANT, f.docId());

        CapturingExchange ex = new CapturingExchange("POST",
            URI.create("/v1/catalog/index-run/begin"),
            JSON.writeValueAsString(Map.of("doc_id", f.docId(), "content_hash", "h", "run_id", "r",
                "collection", f.collection(), "snapshot_manifest", true)));
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }

        assertThat(ex.status).as(ex.bodyString()).isNotEqualTo(200);
        assertThat(ex.bodyString()).doesNotContain("prior_chashes");
    }

    @Test
    void aSnapshotNeverShowsAnotherTenantsManifest() throws Exception {
        Fx f = fixture("tenant");
        writeManifest(f, List.of("tenant/x1", "tenant/x2"));

        // The same doc_id asked for under a different tenant: RLS hides the document and its rows.
        Map<String, Object> other = repo.beginIndexRun("other-tenant-z0o2p10", f.docId(), "h", "r",
            f.collection(), true);

        assertThat(other.get("prior_chashes")).isEqualTo(List.of());
        assertThat(other.get("prior_count")).isEqualTo(0);
        // ...and the owning tenant still sees its own.
        assertThat(begin(f, true).get("prior_count")).isEqualTo(2);
    }

    @Test
    void theSnapshotShowsTheManifestAtBeginNotTheOneAWriteThenReplacesItWith() throws Exception {
        Fx f = fixture("replace");
        writeManifest(f, List.of("replace/old1", "replace/old2"));

        Map<String, Object> resp = begin(f, true);
        writeManifest(f, List.of("replace/new1"));       // the run's batch 1 replaces the manifest

        // What the snapshot said stays the pre-run list; the manifest itself has moved on.
        assertThat(resp.get("prior_chashes")).isEqualTo(List.of(ch("replace/old1"), ch("replace/old2")));
        assertThat(manifestChashes(f.docId())).containsExactly(ch("replace/new1"));
    }
}
