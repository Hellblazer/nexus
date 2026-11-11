// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.PdfStubMetadataReads;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.net.http.HttpRequest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/stub-metadata?limit=N}, the doctor's PDF placeholder-metadata row in one bounded
 * tenant-scoped read. Real PostgreSQL, the real route. Each exclusion (another collection type, a tombstone, another
 * tenant, a different collection, a healthy chunk) is shown beside a control stub in the same fixture, so an empty
 * answer cannot come from a fixture that holds no stub. The rule itself (title, keys, extraction_method, PDF-ness,
 * naming) is held to the client's by {@link CatalogStubMetadataParityTest}.
 */
class CatalogStubMetadataRouteTest extends StubMetadataTestBase {

    // ── the answer ──────────────────────────────────────────────────────────

    @Test
    void anEmptyTenantAnswersNoCollections() throws Exception {
        var body = stubs("");
        assertThat(body).containsEntry("limit", 1000);
        assertThat(collections(body)).isEmpty();
        assertThat(body.keySet()).containsExactlyInAnyOrder("collections", "limit");
    }

    @Test
    void aStubIsCountedAndItsDocumentNamed_besideAHealthyControlInTheSameCollection() throws Exception {
        stubPdf(KNOW_A, "broken");
        pdfWithChunk(TENANT, KNOW_A, "fine", HEALTHY);
        var c = entry(stubs(""), KNOW_A);
        assertThat(c).containsEntry("stub_chunks", 1).containsEntry("capped", false);
        assertThat(c.get("examples")).isEqualTo(List.of("file:///stubs/broken.pdf"));
        assertThat(c.keySet()).containsExactlyInAnyOrder("collection", "stub_chunks", "capped", "examples");
    }

    @Test
    void aCollectionWithNoStubIsListedWithZero_becauseItWasExamined() throws Exception {
        stubPdf(KNOW_A, "broken");
        pdfWithChunk(TENANT, KNOW_B, "fine", HEALTHY);
        var body = stubs("");
        assertThat(collections(body)).extracting(c -> c.get("collection")).containsExactly(KNOW_A, KNOW_B);
        assertThat(entry(body, KNOW_B)).containsEntry("stub_chunks", 0).containsEntry("capped", false)
            .containsEntry("examples", List.of());
    }

    @Test
    void onlyKnowledgeAndDocsCollectionsAreExamined_codeAndRdrAreNot() throws Exception {
        stubPdf(KNOW_A, "k");
        stubPdf(DOCS_A, "d");
        stubPdf(CODE_A, "c");
        stubPdf(RDR_A, "r");
        var body = stubs("");
        assertThat(collections(body)).extracting(c -> c.get("collection")).containsExactly(DOCS_A, KNOW_A);
        assertThat(entry(body, KNOW_A)).containsEntry("stub_chunks", 1);
        assertThat(entry(body, DOCS_A)).containsEntry("stub_chunks", 1);
    }

    @Test
    void aTombstonedDocumentIsNotCounted() throws Exception {
        stubPdf(KNOW_A, "live");
        String gone = stubPdf(KNOW_A, "gone");
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.parse("2026-11-01T00:00:00+00:00"))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(gone))).execute();
        }
        var c = entry(stubs(""), KNOW_A);
        assertThat(c).containsEntry("stub_chunks", 1);
        assertThat(c.get("examples")).isEqualTo(List.of("file:///stubs/live.pdf"));
    }

    @Test
    void anotherTenantsStubsAreInvisible_andSoAreOurs() throws Exception {
        stubPdf(KNOW_A, "ours");
        pdfWithChunk(TENANT_B, KNOW_A, "theirs", STUB);
        var ours = entry(stubs(""), KNOW_A);
        assertThat(ours).containsEntry("stub_chunks", 1);
        assertThat(ours.get("examples")).isEqualTo(List.of("file:///stubs/ours.pdf"));
        var resp = get(TOKEN_B, TENANT_B, "/v1/catalog/stub-metadata");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        var theirs = entry(mapper.readValue(resp.body(), MAP_T), KNOW_A);
        assertThat(theirs).containsEntry("stub_chunks", 1);
        assertThat(theirs.get("examples")).isEqualTo(List.of("file:///stubs/theirs.pdf"));
    }

    @Test
    void aManifestRowAtAnotherCollectionThanTheDocumentsOwnIsNotCountedUnderEither() throws Exception {
        // The client reads a collection's documents (physical_collection) and keeps only the manifest rows at that
        // collection. A document in A whose manifest row names B is in neither set.
        collection(TENANT, KNOW_A);
        collection(TENANT, KNOW_B);
        byte[] chash = nextChash();
        chunk(TENANT, KNOW_B, chash, STUB);
        document(TENANT, "7.900.1", KNOW_A, "pdf", "/stubs/split.pdf", "file:///stubs/split.pdf", "{}", false);
        manifest(TENANT, "7.900.1", 0, KNOW_B, chash);
        stubPdf(KNOW_A, "control");
        var body = stubs("");
        assertThat(entry(body, KNOW_A)).containsEntry("stub_chunks", 1);
        assertThat(entry(body, KNOW_A).get("examples")).isEqualTo(List.of("file:///stubs/control.pdf"));
        assertThat(entry(body, KNOW_B)).containsEntry("stub_chunks", 0);
    }

    @Test
    void aChunkAManifestListsAtTwoPositionsOfOneDocumentCountsOnce() throws Exception {
        String t = stubPdf(KNOW_A, "twice");
        byte[] same = nextChash();
        chunk(TENANT, KNOW_A, same, STUB);
        manifest(TENANT, t, 1, KNOW_A, same);
        manifest(TENANT, t, 2, KNOW_A, same);
        // the original chunk plus the one listed twice: two distinct chunks, three manifest rows
        assertThat(entry(stubs(""), KNOW_A)).containsEntry("stub_chunks", 2);
    }

    @Test
    void aChunkTwoDocumentsHoldCountsUnderEach() throws Exception {
        collection(TENANT, KNOW_A);
        byte[] shared = nextChash();
        chunk(TENANT, KNOW_A, shared, STUB);
        for (String n : List.of("one", "two")) {
            String t = "7.9" + n.length() + n.charAt(0) + ".1";
            document(TENANT, t, KNOW_A, "pdf", "/stubs/" + n + ".pdf", "file:///stubs/" + n + ".pdf", "{}", false);
            manifest(TENANT, t, 0, KNOW_A, shared);
        }
        var c = entry(stubs(""), KNOW_A);
        assertThat(c).containsEntry("stub_chunks", 2);
        assertThat(c.get("examples")).isEqualTo(List.of("file:///stubs/one.pdf", "file:///stubs/two.pdf"));
    }

    /**
     * Row-level security is the second line; the statement scopes by tenant itself (an explicit equality on the registry,
     * the documents and the chunk rows). Run as the superuser, which row-level security does not filter, with the SAME
     * collection name holding a stub in both tenants, it must still answer only the tenant it was asked about.
     */
    @Test
    void theStatementScopesByTenantItself_evenOnASessionRowLevelSecurityDoesNotFilter() throws Exception {
        stubPdf(KNOW_A, "ours");
        pdfWithChunk(TENANT_B, KNOW_A, "theirs", STUB);
        pdfWithChunk(TENANT_B, KNOW_B, "theirs-only-collection", STUB);
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var rows = PdfStubMetadataReads.query(ctx, TENANT, 11).fetch();
            assertThat(rows).extracting(r -> r.value1()).containsExactly(KNOW_A);
            assertThat(rows).extracting(r -> r.value2()).containsExactly("file:///stubs/ours.pdf");
            assertThat(rows).extracting(r -> r.value3()).containsExactly(1);
        }
    }

    // ── the bound ───────────────────────────────────────────────────────────

    @Test
    void theCountStopsAtTheLimit_andCappedSaysSo_perCollection() throws Exception {
        for (int i = 0; i < 5; i++) stubPdf(KNOW_A, "a" + i);
        stubPdf(KNOW_B, "b0");

        var two = stubs("?limit=2");
        assertThat(entry(two, KNOW_A)).containsEntry("stub_chunks", 2).containsEntry("capped", true);
        assertThat(entry(two, KNOW_B)).containsEntry("stub_chunks", 1).containsEntry("capped", false);
        assertThat(two).containsEntry("limit", 2);

        assertThat(entry(stubs("?limit=4"), KNOW_A)).containsEntry("stub_chunks", 4).containsEntry("capped", true);
        // exactly as many as the limit: not capped
        assertThat(entry(stubs("?limit=5"), KNOW_A)).containsEntry("stub_chunks", 5).containsEntry("capped", false);
        assertThat(entry(stubs("?limit=6"), KNOW_A)).containsEntry("stub_chunks", 5).containsEntry("capped", false);
    }

    @Test
    void aCappedCollectionStillNamesAFewOfItsDocuments() throws Exception {
        for (int i = 0; i < 5; i++) stubPdf(KNOW_A, "a" + i);
        var c = entry(stubs("?limit=2"), KNOW_A);
        @SuppressWarnings("unchecked") var examples = (List<String>) c.get("examples");
        assertThat(examples).isNotEmpty().hasSizeLessThanOrEqualTo(3).doesNotHaveDuplicates();
        assertThat(examples).allMatch(e -> e.startsWith("file:///stubs/a"));
    }

    @Test
    void examplesAreAtMostTen_documentsWithTheMostStubChunksFirst() throws Exception {
        for (int i = 0; i < 12; i++) {
            String t = stubPdf(KNOW_A, "d" + String.format("%02d", i));
            for (int extra = 0; extra < i; extra++) {      // document d(i) holds i + 1 stub chunks
                byte[] c = nextChash();
                chunk(TENANT, KNOW_A, c, STUB);
                manifest(TENANT, t, extra + 1, KNOW_A, c);
            }
        }
        var c = entry(stubs(""), KNOW_A);
        assertThat(c).containsEntry("stub_chunks", 78).containsEntry("capped", false);
        @SuppressWarnings("unchecked") var examples = (List<String>) c.get("examples");
        assertThat(examples).hasSize(PdfStubMetadataReads.MAX_EXAMPLES);
        assertThat(examples.get(0)).isEqualTo("file:///stubs/d11.pdf");
        assertThat(examples).doesNotContain("file:///stubs/d00.pdf", "file:///stubs/d01.pdf");
    }

    @Test
    void theDefaultLimitIsOneThousand_andTheMaxIsTenThousand() throws Exception {
        assertThat(stubs("")).containsEntry("limit", 1000);
        assertThat(stubs("?limit=10000")).containsEntry("limit", 10000);
    }

    @Test
    void aBadLimitIsA400_onlyGetIsAllowed_andNoCredentialIsA401() throws Exception {
        for (String bad : List.of("0", "-1", "10001", "abc", "1.5")) {
            assertThat(get(TOKEN, TENANT, "/v1/catalog/stub-metadata?limit=" + bad).statusCode()).as("limit=" + bad).isEqualTo(400);
        }
        var post = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/stub-metadata")
            .header("Authorization", "Bearer " + TOKEN).header("X-Nexus-Tenant", TENANT)
            .POST(HttpRequest.BodyPublishers.ofString("{}")).build();
        assertThat(http.send(post, java.net.http.HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(405);
        assertThat(get(null, TENANT, "/v1/catalog/stub-metadata").statusCode()).isEqualTo(401);
    }

    @Test
    void aReadThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        // Deterministic: another session holds ACCESS EXCLUSIVE on the documents table, so the statement waits on the
        // lock until its statement_timeout fires; nothing is answered in part.
        stubPdf(KNOW_A, "a");
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENTS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, TENANT, "/v1/catalog/stub-metadata");
            long ms = (System.nanoTime() - t0) / 1_000_000;
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
            var body = mapper.readValue(resp.body(), MAP_T);
            assertThat(body).containsEntry("reason", "statement_timeout");
            assertThat(body).doesNotContainKeys("collections", "limit");
            assertThat(ms).as("answered at the bound, not after the lock was released").isLessThan(5_000);
            holder.rollback();
        } finally {
            System.clearProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
        assertThat(entry(stubs(""), KNOW_A)).containsEntry("stub_chunks", 1);
    }

    /**
     * Most of a real collection is NOT a candidate (markdown, code, PDFs with a recorded method), and the statement must not
     * read their chunk rows. 300 markdown documents of 8 chunks each (2,400 chunk rows) beside 5 PDF documents of 8 stub
     * chunks: the plan may read the candidates' 40 chunks, not the collection's 2,440. Rows read are the {@code rows x
     * loops} of the plan nodes that scan the chunk table, from EXPLAIN ANALYZE of the exact statement the route runs.
     */
    @Test
    void chunkRowsAreReadForTheCandidateDocumentsOnly_notForTheWholeCollection() throws Exception {
        collection(TENANT, KNOW_A);
        var docs = new ArrayList<org.jooq.Query>();
        var chunks = new ArrayList<org.jooq.Query>();
        var manifest = new ArrayList<org.jooq.Query>();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, KNOW_A);
            for (int d = 0; d < 305; d++) {
                boolean pdf = d >= 300;
                String t = "7.700." + d;
                docs.add(dsl.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, t)
                    .set(CATALOG_DOCUMENTS.TITLE, "doc " + d)
                    .set(CATALOG_DOCUMENTS.CONTENT_TYPE, pdf ? "pdf" : "prose")
                    .set(CATALOG_DOCUMENTS.FILE_PATH, "/scale/" + d + (pdf ? ".pdf" : ".md"))
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///scale/" + d + (pdf ? ".pdf" : ".md"))
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, KNOW_A)
                    .set(CATALOG_DOCUMENTS.METADATA, org.jooq.JSONB.valueOf("{}")));
                for (int r = 0; r < 8; r++) {
                    byte[] chash = new byte[32];
                    chash[0] = (byte) (d >> 8); chash[1] = (byte) d; chash[2] = (byte) r; chash[31] = 1;
                    chunks.add(dsl.insertInto(dev.nexus.service.jooq.nexus.Tables.CHUNKS)
                        .set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID, TENANT)
                        .set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION, KNOW_A)
                        .set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.CHASH, chash)
                        .set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.EMBEDDING_MODEL, model)
                        .set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.CHUNK_TEXT, "t")
                        .set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.1f))
                        .set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.METADATA,
                            org.jooq.JSONB.valueOf(pdf ? STUB : HEALTHY)));
                    manifest.add(dsl.insertInto(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS)
                        .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
                        .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.DOC_ID, t)
                        .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.POSITION, r)
                        .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, r)
                        .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
                        .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.COLLECTION, KNOW_A)
                        .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model));
                }
            }
            dsl.batch(docs).execute();
            for (int i = 0; i < chunks.size(); i += 400) dsl.batch(chunks.subList(i, Math.min(i + 400, chunks.size()))).execute();
            dsl.batch(manifest).execute();
            for (var table : List.of(CATALOG_DOCUMENTS, dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS,
                    dev.nexus.service.jooq.nexus.Tables.CHUNKS)) {
                PgContainerHelper.analyzeTable(su, table);
            }
        }
        var scope = new TenantScope(svcDs);
        String plan = scope.withTenant(TENANT, ctx -> PgContainerHelper.explainPlanWith(ctx,
            PdfStubMetadataReads.query(ctx, TENANT, 1001), PgContainerHelper.ExplainOptions.ANALYZE_VERBOSE_COSTS_OFF));
        System.out.println("plan of the stub-metadata statement over 2,440 chunk rows, 40 of them candidates:\n" + plan);
        assertThat(chunkRowsRead(plan)).as("chunk rows read by the plan:\n" + plan).isPositive().isLessThan(400);
        assertThat(entry(stubs(""), KNOW_A)).containsEntry("stub_chunks", 40).containsEntry("capped", false);
    }

    /** The {@code rows x loops} of every plan node that scans the chunk table (a partition of it included). */
    private static long chunkRowsRead(String plan) {
        long read = 0;
        Matcher m = Pattern.compile("^.*(?:Scan|Only Scan) (?:using \\S+ )?on (?:\\S+\\.)?chunks\\S* .*\\(actual time=\\S+ rows=(\\d+) loops=(\\d+)\\)")
            .matcher("");
        for (String line : plan.split("\n")) {
            if (m.reset(line).find()) read += Long.parseLong(m.group(1)) * Long.parseLong(m.group(2));
        }
        return read;
    }

    // ── the plan ────────────────────────────────────────────────────────────

    /**
     * The route is ONE statement for every collection, and a collection with a backlog stops at {@code limit + 1} stub rows:
     * with 150 stub chunks in the collection and a limit of 3 the statement reads a handful of chunk rows, not 150. The
     * rows read are the {@code rows x loops} of the plan nodes that scan the chunk table, from EXPLAIN ANALYZE of the exact
     * statement the route runs, under the tenant's row-level security.
     */
    @Test
    void aBacklogCollectionStopsAtLimitPlusOne_itDoesNotReadEveryChunk() throws Exception {
        collection(TENANT, KNOW_A);
        String t = "7.800.1";
        document(TENANT, t, KNOW_A, "pdf", "/stubs/backlog.pdf", "file:///stubs/backlog.pdf", "{}", false);
        for (int i = 0; i < 150; i++) {
            byte[] c = nextChash();
            chunk(TENANT, KNOW_A, c, STUB);
            manifest(TENANT, t, i, KNOW_A, c);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (var table : List.of(CATALOG_DOCUMENTS, dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS,
                    dev.nexus.service.jooq.nexus.Tables.CHUNKS)) {
                PgContainerHelper.analyzeTable(su, table);
            }
        }
        var scope = new TenantScope(svcDs);
        String plan = scope.withTenant(TENANT, ctx -> PgContainerHelper.explainPlanWith(ctx,
            PdfStubMetadataReads.query(ctx, TENANT, 4), PgContainerHelper.ExplainOptions.ANALYZE_VERBOSE_COSTS_OFF));
        System.out.println("plan of the stub-metadata statement at limit 3, analysed:\n" + plan);
        long chunkRowsRead = chunkRowsRead(plan);
        assertThat(chunkRowsRead).as("chunk rows read by the plan:\n" + plan).isPositive().isLessThan(40);
        assertThat(plan).contains("Limit");
        // the same statement through the route answers the capped count
        assertThat(entry(stubs("?limit=3"), KNOW_A)).containsEntry("stub_chunks", 3).containsEntry("capped", true);
    }
}
