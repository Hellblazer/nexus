// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.JSONB;
import org.jooq.Query;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/index-run/census} on the shape the doctor rows were slow on: a tenant of 4,000
 * documents, 300 of them non-complete (120 stale {@code indexing}, 100 {@code failed} with their chunks stored and no
 * manifest, 80 {@code failed} that keep a manifest), the other 3,700 half complete (in shared runs) and half never stamped,
 * with 3,500 chunk rows in one collection. The route must answer every bucket exactly inside its bound; the timings are
 * printed for the record (the client's walk of the same tenant is 44 requests of a 43.7k-document tenant, 22 s).
 */
class IndexRunCensusLegacyShapeTest extends IndexRunCensusTestBase {

    @Test
    void theLegacyShapeIsAnsweredExactlyWithinItsBound_andTheTimingsAreRecorded() throws Exception {
        collection(TENANT, C1);
        OffsetDateTime old = OffsetDateTime.now(ZoneOffset.UTC).minusHours(30);
        OffsetDateTime start = OffsetDateTime.parse("2026-08-10T00:00:00+00:00");
        long seedStart = System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, C1);
            var docs = new ArrayList<Query>();
            var chunks = new ArrayList<Query>();
            var manifest = new ArrayList<Query>();
            long chashSeq = 0;
            for (int i = 0; i < 4_000; i++) {
                String tumbler = "8.2." + (i + 1);
                String state;
                if (i < 120) state = "indexing";
                else if (i < 300) state = "failed";
                else state = i % 2 == 0 ? "complete" : null;
                var q = dsl.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
                    .set(CATALOG_DOCUMENTS.TITLE, "d" + i).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "code")
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///legacy/" + i).set(CATALOG_DOCUMENTS.INDEX_STATE, state)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, C1).set(CATALOG_DOCUMENTS.CHUNK_COUNT, 3)
                    .set(CATALOG_DOCUMENTS.INDEXED_AT, start.plusMinutes(i));
                if (state != null && state.equals("complete")) q = q.set(CATALOG_DOCUMENTS.INDEX_RUN_ID, "run" + i / 50);
                if (i < 120) q = q.set(CATALOG_DOCUMENTS.INDEX_STARTED_AT, old);
                if (i >= 120 && i < 220) {
                    String hash = "fh" + i;
                    q = q.set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{\"content_hash\":\"" + hash + "\"}"));
                    for (int k = 0; k < 3; k++) chunks.add(chunkRow(dsl, model, ++chashSeq, "{\"content_hash\":\"" + hash + "\"}"));
                }
                if (i >= 220 && i < 300) {
                    long id = ++chashSeq;
                    chunks.add(chunkRow(dsl, model, id, "{}"));
                    manifest.add(dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                        .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, tumbler)
                        .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, 0)
                        .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash(id)).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, C1)
                        .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model));
                }
                docs.add(q);
            }
            // the bulk of the collection: legacy rows of the complete documents
            for (int i = 0; i < 3_000; i++) chunks.add(chunkRow(dsl, model, ++chashSeq, "{\"content_hash\":\"legacy" + i + "\"}"));
            for (int i = 0; i < docs.size(); i += 1000) dsl.batch(docs.subList(i, Math.min(i + 1000, docs.size()))).execute();
            for (int i = 0; i < chunks.size(); i += 500) dsl.batch(chunks.subList(i, Math.min(i + 500, chunks.size()))).execute();
            for (int i = 0; i < manifest.size(); i += 500) dsl.batch(manifest.subList(i, Math.min(i + 500, manifest.size()))).execute();
            for (var table : List.of(CATALOG_DOCUMENTS, CATALOG_DOCUMENT_CHUNKS, CHUNKS)) PgContainerHelper.analyzeTable(su, table);
        }
        System.out.println("CENSUS-LEGACY-SHAPE seeded 4000 documents (300 non-complete), " + (3_000 + 300 + 80) + " chunk rows in "
            + (System.nanoTime() - seedStart) / 1_000_000 + " ms");

        List<Long> routeMs = new ArrayList<>();
        java.util.Map<String, Object> body = null;
        for (int run = 0; run < 3; run++) {
            long t0 = System.nanoTime();
            body = census("");
            routeMs.add((System.nanoTime() - t0) / 1_000_000);
        }
        System.out.println("CENSUS-LEGACY-SHAPE route ms (3 runs, the first one cold): " + routeMs);

        assertThat(bucket(body, "stale")).containsEntry("count", 120).containsEntry("capped", false);
        var fence = bucket(body, "fence");
        assertThat(fence).containsEntry("checked", 120 + 180 + 1_850).containsEntry("reported_null", 1_850);
        assertThat(bucket(fence, "anchor")).containsEntry("kind", "proven");
        var fh = bucket(body, "failed_hidden");
        assertThat(bucket(fh, "failed")).containsEntry("count", 180).containsEntry("capped", false);
        assertThat(fh).containsEntry("count", 100).containsEntry("capped", false);
        for (String b : List.of("stale", "fence", "failed_hidden")) assertThat(bucket(body, b).keySet()).as(b).doesNotContain("timed_out");
        assertThat(routeMs.get(routeMs.size() - 1)).as("warm route, ms").isLessThan(10_000);
    }

    private static byte[] chash(long id) {
        byte[] chash = new byte[32];
        for (int b = 0; b < 8; b++) chash[b] = (byte) (id >> (8 * b));
        chash[31] = 1;
        return chash;
    }

    private static Query chunkRow(org.jooq.DSLContext dsl, String model, long id, String meta) {
        return dsl.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, C1).set(CHUNKS.CHASH, chash(id))
            .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, "t")
            .set(CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.1f)).set(CHUNKS.METADATA, JSONB.valueOf(meta));
    }
}
