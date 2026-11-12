// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.PdfStubMetadataReads;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/stub-metadata?limit=N}, the doctor's PDF placeholder-metadata row in one bounded
 * tenant-scoped read, split out of {@link CatalogHandler} (which is at its file-size cap).
 * {@link PdfStubMetadataReads} has the rule (the client's, held to one case table) and the bounds.
 *
 * <p>{@code {collections:[{collection, stub_chunks, capped, examples} | {collection, timed_out:true}], limit}}: every
 * knowledge and docs collection of the tenant in name order, stub-free ones included (a collection listed with 0 was
 * examined). {@code stub_chunks} is at most {@code limit}; {@code capped} true means MORE than {@code limit} exist and the
 * count is "at least" that, so a client reads it as a lower bound. {@code examples} are at most 10 owning-document names,
 * the documents with the most stub chunks first. {@code limit} is 1 to {@link PdfStubMetadataReads#MAX_LIMIT} (default
 * {@link PdfStubMetadataReads#DEFAULT_LIMIT}), per collection; anything else is a 400. A new path rather than a
 * parameter on {@code /list} (the {@code /docs/collection-counts-all} rule, nexus-8tnz2).
 *
 * <p><b>One slow collection does not blank the others.</b> Each collection is read by its own bounded statement
 * ({@link PdfStubMetadataReads} has the bounds: 10 s a collection, 25 s for the call). A collection that could not be read
 * in time, or that the call's budget never reached, is {@code {collection, timed_out:true}} with no count and no example:
 * "cannot say", never 0. The status stays 200 while the collection list was read. An old engine answers 404, and a call
 * whose collection LIST outlasts its bound answers 503 {@code {"error":..., "reason":"statement_timeout"}} with no body of
 * the answer; a client reads both as "cannot say" for every collection.
 */
final class CatalogStubMetadataRoute {

    private static final Logger log = LoggerFactory.getLogger(CatalogStubMetadataRoute.class);

    private final PdfStubMetadataReads reads;

    CatalogStubMetadataRoute(PdfStubMetadataReads reads) {
        this.reads = reads;
    }

    void stubMetadata(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Integer limit = CatalogBoundedReadRoutes.limitParam(exchange, PdfStubMetadataReads.DEFAULT_LIMIT,
            PdfStubMetadataReads.MAX_LIMIT);
        if (limit == null) return;
        PdfStubMetadataReads.Page page;
        try {
            page = reads.read(tenant, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_stub_metadata_timeout tenant={} limit={}", tenant, limit);
            CatalogBoundedReadRoutes.timeout(exchange, "stub metadata read exceeded its time bound");
            return;
        }
        List<Map<String, Object>> collections = new ArrayList<>(page.collections().size());
        for (var c : page.collections()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("collection", c.collection());
            if (c.timedOut()) {
                m.put("timed_out", true);
            } else {
                m.put("stub_chunks", c.stubChunks());
                m.put("capped", c.capped());
                m.put("examples", c.examples());
            }
            collections.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collections", collections);
        body.put("limit", page.limit());
        CatalogBoundedReadRoutes.send(exchange, 200, body);
    }
}
