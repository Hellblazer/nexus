// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;

import java.io.InputStream;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: the SQL of {@code GET /v1/catalog/stub-metadata} agrees with the client's
 * {@code nexus.health._is_pdf_stub_metadata} on every case of ONE shared table,
 * {@code pdf-stub-metadata-cases.json}. {@code tests/catalog/test_pdf_stub_metadata_cases.py} reads the same file and
 * holds the Python function to it, so a change to either rule that the other does not follow fails one of the two.
 *
 * <p>Each case is seeded as one live document with one manifest chunk, in a collection of its own (so a case's answer
 * is its collection's entry), and the route must count that chunk exactly when the table says it is a stub, and name the
 * document exactly as the table says the client does.
 */
class CatalogStubMetadataParityTest extends StubMetadataTestBase {

    private JsonNode cases() throws Exception {
        try (InputStream in = CatalogStubMetadataParityTest.class.getResourceAsStream("/pdf-stub-metadata-cases.json")) {
            assertThat(in).as("resource pdf-stub-metadata-cases.json").isNotNull();
            return mapper.readTree(in).get("cases");
        }
    }

    @Test
    void theTableIsReadWhole() throws Exception {
        // A table that lost its rows would pass the parity test below with nothing to check.
        JsonNode cases = cases();
        int stubs = 0;
        for (JsonNode c : cases) if (c.get("expect").get("stub").asBoolean()) stubs++;
        assertThat(cases.size()).isGreaterThanOrEqualTo(40);
        assertThat(stubs).isGreaterThanOrEqualTo(15);
        assertThat(cases.size() - stubs).isGreaterThanOrEqualTo(15);
    }

    @Test
    void theRouteCountsExactlyTheChunksTheTableCallsStubs_andNamesTheirDocumentsAsTheTableDoes() throws Exception {
        JsonNode cases = cases();
        List<String> collections = new ArrayList<>();
        int n = 0;
        for (JsonNode c : cases) {
            String coll = "knowledge__parity-" + (n++) + "__" + MODEL + "__v1";
            collections.add(coll);
            collection(TENANT, coll);
            byte[] chash = nextChash();
            chunk(TENANT, coll, chash, mapper.writeValueAsString(c.get("chunk")));
            JsonNode d = c.get("document");
            String tumbler = "7.5" + n + ".1";
            document(TENANT, tumbler, coll, d.get("content_type").asText(), d.get("file_path").asText(),
                d.get("source_uri").asText(), mapper.writeValueAsString(d.get("meta")), false);
            manifest(TENANT, tumbler, 0, coll, chash);
        }
        var body = stubs("");
        assertThat(collections(body)).hasSize(cases.size());
        int expectedStubs = 0;
        for (int i = 0; i < cases.size(); i++) {
            JsonNode expect = cases.get(i).get("expect");
            String id = cases.get(i).get("id").asText();
            var entry = entry(body, collections.get(i));
            if (expect.get("stub").asBoolean()) {
                expectedStubs++;
                assertThat(entry).as(id).containsEntry("stub_chunks", 1);
                assertThat(entry.get("examples")).as(id).isEqualTo(List.of(expect.get("source").asText()));
            } else {
                assertThat(entry).as(id).containsEntry("stub_chunks", 0);
                assertThat(entry.get("examples")).as(id).isEqualTo(List.of());
            }
        }
        long total = collections(body).stream().mapToLong(c -> ((Number) c.get("stub_chunks")).longValue()).sum();
        assertThat(total).isEqualTo(expectedStubs);
    }

    // ── scenarios: several documents, chunks and manifest rows ──────────────

    private JsonNode scenarios() throws Exception {
        try (InputStream in = CatalogStubMetadataParityTest.class.getResourceAsStream("/pdf-stub-metadata-cases.json")) {
            return mapper.readTree(in).get("scenarios");
        }
    }

    private static byte[] chash(String scenario, String key) throws Exception {
        // the same identity as the Python half's sha256 hex of "<scenario>/<key>"
        return MessageDigest.getInstance("SHA-256").digest((scenario + "/" + key).getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    @Test
    void theScenariosAreReadWhole() throws Exception {
        JsonNode scenarios = scenarios();
        assertThat(scenarios.size()).isGreaterThanOrEqualTo(8);
        int owners = 0;
        for (JsonNode s : scenarios) owners += s.get("expect").get("main").size();
        assertThat(owners).isGreaterThanOrEqualTo(8);
    }

    @Test
    void theRouteCountsEachScenariosOwnersAsTheClientDoes_owningDocumentCountUnderEachHolderPositionsAndCollections() throws Exception {
        JsonNode scenarios = scenarios();
        int n = 0;
        Map<String, Map<String, String>> collectionsOf = new HashMap<>();
        for (JsonNode sc : scenarios) {
            String id = sc.get("id").asText();
            int i = n++;
            Map<String, String> names = Map.of(
                "main", "knowledge__scn" + i + "-main__" + MODEL + "__v1", "other", "knowledge__scn" + i + "-other__" + MODEL + "__v1");
            collectionsOf.put(id, names);
            collection(TENANT, names.get("main"));
            collection(TENANT, names.get("other"));
            Map<String, byte[]> chashes = new HashMap<>();
            Map<String, List<String>> chunkCollections = new HashMap<>();
            for (JsonNode c : sc.get("chunks")) {
                String key = c.get("key").asText();
                chashes.put(key, chash(id, key));
                // one collection name, or a list when the same chunk (so the same chash) is a row of several
                var which = new ArrayList<String>();
                if (c.get("collection").isArray()) c.get("collection").forEach(w -> which.add(names.get(w.asText())));
                else which.add(names.get(c.get("collection").asText()));
                chunkCollections.put(key, which);
                for (String coll : which) chunk(TENANT, coll, chashes.get(key), mapper.writeValueAsString(c.get("metadata")));
            }
            Map<String, String> tumblers = new HashMap<>();
            Map<String, String> docCollection = new HashMap<>();
            int d = 0;
            for (JsonNode doc : sc.get("documents")) {
                String key = doc.get("key").asText();
                String tumbler = "7.4" + i + "." + (++d);
                tumblers.put(key, tumbler);
                docCollection.put(key, names.get(doc.get("collection").asText()));
                document(TENANT, tumbler, docCollection.get(key), doc.get("content_type").asText(), doc.get("file_path").asText(),
                    doc.get("source_uri").asText(), mapper.writeValueAsString(doc.get("meta")), false);
            }
            for (JsonNode row : sc.get("manifest")) {
                String chunkKey = row.get("chunk").asText();
                String docKey = row.get("doc").asText();
                String coll = row.has("collection") ? names.get(row.get("collection").asText()) : docCollection.get(docKey);
                assertThat(chunkCollections.get(chunkKey)).as(id + ": the manifest row names a collection its chunk is in").contains(coll);
                manifest(TENANT, tumblers.get(docKey), row.get("position").asInt(), coll, chashes.get(chunkKey));
            }
        }
        var body = stubs("");
        for (JsonNode sc : scenarios) {
            String id = sc.get("id").asText();
            for (var e = sc.get("expect").fields(); e.hasNext(); ) {
                var which = e.next();
                var entry = entry(body, collectionsOf.get(id).get(which.getKey()));
                int total = 0;
                var want = new ArrayList<Map.Entry<String, Integer>>();
                for (var src = which.getValue().fields(); src.hasNext(); ) {
                    var kv = src.next();
                    total += kv.getValue().asInt();
                    want.add(Map.entry(kv.getKey(), kv.getValue().asInt()));
                }
                want.sort(Map.Entry.<String, Integer>comparingByValue(Comparator.reverseOrder()).thenComparing(Map.Entry.comparingByKey()));
                assertThat(entry).as(id + " / " + which.getKey()).containsEntry("stub_chunks", total).containsEntry("capped", false);
                assertThat(entry.get("examples")).as(id + " / " + which.getKey())
                    .isEqualTo(want.stream().map(Map.Entry::getKey).toList());
            }
        }
    }
}
