// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;

import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

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
}
