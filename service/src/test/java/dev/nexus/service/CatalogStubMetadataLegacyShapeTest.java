// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.PdfStubMetadataReads;
import dev.nexus.service.db.TenantScope;
import org.jooq.JSONB;
import org.jooq.Query;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/stub-metadata} on the LEGACY shape the doctor row was slow on (nexus-iho0e, measured
 * 2026-11-03 on the local tenant: 24,517 chunks in 244 PDF documents that record no extraction method, mostly healthy
 * titled rows indexed before the placeholder was retired). Here: 244 such documents across three collections holding
 * 24,484 chunk rows, 241 of the documents healthy and three stub documents of 128 chunks each, so the converged case (which
 * cannot stop early) reads every candidate chunk. The route must answer every collection exactly inside its bound, and the
 * timings (the route, and EXPLAIN ANALYZE of each collection's statement) are printed for the record.
 */
class CatalogStubMetadataLegacyShapeTest extends StubMetadataTestBase {

    private static final int[] DOCS_PER_COLLECTION = {150, 70, 24};
    private static final int CHUNKS_PER_HEALTHY_DOC = 100;
    private static final int CHUNKS_PER_STUB_DOC = 128;

    @Test
    void theLegacyShapeIsAnsweredExactlyWithinItsBound_andTheTimingsAreRecorded() throws Exception {
        List<String> names = List.of(KNOW_A, KNOW_B, DOCS_A);
        int[] stubDocsIn = {1, 1, 1};
        int totalChunks = 0;
        long seedStart = System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            int docSeq = 0;
            long chashSeq = 0;
            for (int c = 0; c < names.size(); c++) {
                String coll = names.get(c);
                collection(TENANT, coll);
                String model = PgContainerHelper.collectionModel(dsl, TENANT, coll);
                var docs = new ArrayList<Query>();
                var chunks = new ArrayList<Query>();
                var manifest = new ArrayList<Query>();
                for (int d = 0; d < DOCS_PER_COLLECTION[c]; d++) {
                    boolean stubDoc = d < stubDocsIn[c];
                    String tumbler = "7.900." + (++docSeq);
                    String file = "/legacy/" + coll.charAt(0) + docSeq + ".pdf";
                    docs.add(dsl.insertInto(CATALOG_DOCUMENTS)
                        .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
                        .set(CATALOG_DOCUMENTS.TITLE, "paper " + docSeq).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "pdf")
                        .set(CATALOG_DOCUMENTS.FILE_PATH, file).set(CATALOG_DOCUMENTS.SOURCE_URI, "file://" + file)
                        .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, coll).set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{}")));
                    int n = stubDoc ? CHUNKS_PER_STUB_DOC : CHUNKS_PER_HEALTHY_DOC;
                    for (int p = 0; p < n; p++) {
                        byte[] chash = new byte[32];
                        long id = ++chashSeq;
                        for (int b = 0; b < 8; b++) chash[b] = (byte) (id >> (8 * b));
                        chash[31] = 1;
                        // the legacy healthy row: titled, with the position and document keys of before the placeholder was retired
                        String meta = stubDoc
                            ? "{\"title\":\"\",\"content_hash\":\"" + Long.toHexString(id) + "\",\"page_number\":" + p + "}"
                            : "{\"title\":\"paper " + docSeq + ":page-" + p + "\",\"content_hash\":\"" + Long.toHexString(id)
                                + "\",\"page_number\":" + p + ",\"source_path\":\"" + file + "\",\"chunk_index\":" + p + "}";
                        chunks.add(dsl.insertInto(CHUNKS)
                            .set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, coll).set(CHUNKS.CHASH, chash)
                            .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, "t")
                            .set(CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.1f)).set(CHUNKS.METADATA, JSONB.valueOf(meta)));
                        manifest.add(dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, tumbler)
                            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, p).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, p)
                            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, coll)
                            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model));
                        totalChunks++;
                    }
                }
                dsl.batch(docs).execute();
                for (int i = 0; i < chunks.size(); i += 1000) dsl.batch(chunks.subList(i, Math.min(i + 1000, chunks.size()))).execute();
                for (int i = 0; i < manifest.size(); i += 2000) dsl.batch(manifest.subList(i, Math.min(i + 2000, manifest.size()))).execute();
            }
            for (var table : List.of(CATALOG_DOCUMENTS, CATALOG_DOCUMENT_CHUNKS, CHUNKS)) PgContainerHelper.analyzeTable(su, table);
        }
        System.out.println("LEGACY-SHAPE seeded " + totalChunks + " chunk rows in 244 documents in "
            + (System.nanoTime() - seedStart) / 1_000_000 + " ms");
        assertThat(totalChunks).isEqualTo(150 * 100 + 70 * 100 + 24 * 100 + 3 * (CHUNKS_PER_STUB_DOC - CHUNKS_PER_HEALTHY_DOC));

        List<Long> routeMs = new ArrayList<>();
        Map<String, Object> body = null;
        for (int run = 0; run < 3; run++) {
            long t0 = System.nanoTime();
            body = stubs("");
            routeMs.add((System.nanoTime() - t0) / 1_000_000);
        }
        System.out.println("LEGACY-SHAPE route ms (3 runs, the first one cold): " + routeMs);
        for (String coll : names) {
            assertThat(entry(body, coll)).as(coll).containsEntry("stub_chunks", CHUNKS_PER_STUB_DOC).containsEntry("capped", false);
            assertThat(entry(body, coll).keySet()).doesNotContain("timed_out");
        }
        assertThat(routeMs.get(routeMs.size() - 1)).as("warm route, ms").isLessThan(10_000);

        var scope = new TenantScope(svcDs);
        Pattern execution = Pattern.compile("Execution Time: ([0-9.]+) ms");
        for (String coll : names) {
            String plan = scope.withTenant(TENANT, ctx -> PgContainerHelper.explainPlanWith(ctx,
                PdfStubMetadataReads.query(ctx, TENANT, coll, MODEL, 1001), PgContainerHelper.ExplainOptions.ANALYZE_VERBOSE_COSTS_OFF));
            var m = execution.matcher(plan);
            assertThat(m.find()).as(plan).isTrue();
            System.out.println("LEGACY-SHAPE EXPLAIN ANALYZE " + coll + ": execution " + m.group(1) + " ms");
            if (coll.equals(KNOW_A)) System.out.println("LEGACY-SHAPE plan of " + coll + ":\n" + plan);
        }
    }
}
