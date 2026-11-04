// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-l4hds: {@code GET /v1/catalog/manifest/section_label_fillable} counts the legacy manifest rows the
 * manifest-section-labels rung could still fill, in one bounded tenant-scoped query. Each test builds the one
 * situation a guard of the rung's scan (src/nexus/upgrade_ladder/rungs/manifest_section_labels.py) exists for,
 * beside a control row that IS fillable in the same fixture, so a count of zero cannot come from a fixture that
 * fills nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestSectionLabelFillableTest {

    private static final String TOKEN    = "section-label-fillable-test-token-l4hds";
    private static final String TOKEN_B  = "section-label-fillable-test-token-l4hds-b";
    private static final String SVC_ROLE = "svc_section_label_fillable_test";
    private static final String SVC_PASS = "svc_section_label_fillable_test_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    private static final String TENANT_B = "tenant-b-l4hds";
    private static final String COLL     = "knowledge__l4hds-owner__v1";
    private static final String DOC_AT   = "2026-10-01T12:00:00+00:00";
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;
    int seq;
    /** The indexed_at each document should carry: the engine stamps "now" on every manifest write, so it is put back after one. */
    final Map<String, String> wantedStamp = new java.util.HashMap<>();

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, TOKEN, TENANT, "test-bound");
            PgContainerHelper.seedServiceToken(dsl, TOKEN_B, TENANT_B, "test-bound-b");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = TestHttp.client();
        // Burn the per-tenant ghost sweep (nexus-ft04v.3) on a request that registers nothing, as the sibling manifest tests do.
        get(TOKEN, TENANT, "/v1/catalog/collections/list");
        get(TOKEN_B, TENANT_B, "/v1/catalog/collections/list");
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** Every test counts its own rows: the route is tenant-wide, so earlier tests' rows are removed. */
    @BeforeEach
    void wipe() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
            dsl.deleteFrom(CHUNKS).execute();
            dsl.deleteFrom(CATALOG_DOCUMENTS).execute();
        }
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    /** A registered document: its tumbler. */
    private String doc(String tenantToken, String tenant, String title, String filePath, String indexedAt, String meta) throws Exception {
        String owner = "l4hds.owner" + (seq++);
        var resp = post(tenantToken, tenant, "/v1/catalog/doc/register", mapper.writeValueAsString(Map.of(
            "owner_prefix", owner, "title", title, "content_type", "prose",
            "source_uri", "file:///l4hds/" + owner + ".md", "physical_collection", COLL)));
        assertThat(resp.statusCode()).as("register: " + resp.body()).isEqualTo(200);
        String tumbler = (String) mapper.readValue(resp.body(), MAP_T).get("tumbler");
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.FILE_PATH, filePath)
                .set(CATALOG_DOCUMENTS.INDEXED_AT, stampOf(indexedAt))
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(meta))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
        wantedStamp.put(tumbler, indexedAt);
        return tumbler;
    }

    private static OffsetDateTime stampOf(String iso) {
        return iso == null ? null : OffsetDateTime.parse(iso);
    }

    private void restamp(String tenant, String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.INDEXED_AT, stampOf(wantedStamp.get(docId)))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
    }

    private String doc(String title) throws Exception {
        return doc(TOKEN, TENANT, title, "", DOC_AT, "{}");
    }

    /** One chunk row with {@code metadata} (a JSON object text) and a manifest row for each (doc, position). */
    private void chunk(String tenant, String chash, String metadataJson) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, tenant, COLL);
            String model = PgContainerHelper.collectionModel(dsl, tenant, COLL);
            dsl.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, tenant)
                .set(CHUNKS.COLLECTION, COLL)
                .set(CHUNKS.CHASH, HexFormat.of().parseHex(chash))
                .set(CHUNKS.EMBEDDING_MODEL, model)
                .set(CHUNKS.CHUNK_TEXT, "stub")
                .set(CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.1f))
                .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL).doNothing()
                .execute();
            int n = dsl.update(CHUNKS).set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(COLL))
                    .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash))))
                .execute();
            assertThat(n).isEqualTo(1);
        }
    }

    /** The manifest rows of a document, one per chash (positions 0..n-1), with no label key: legacy rows. */
    private void manifestAs(String token, String tenant, String docId, String... chashes) throws Exception {
        var rows = new java.util.ArrayList<Map<String, Object>>();
        for (int i = 0; i < chashes.length; i++) rows.add(Map.of("position", i, "chash", chashes[i]));
        var resp = post(token, tenant, "/v1/catalog/manifest/write",
            mapper.writeValueAsString(Map.of("doc_id", docId, "collection", COLL, "rows", rows)));
        assertThat(resp.statusCode()).as("manifest write: " + resp.body()).isEqualTo(200);
        restamp(tenant, docId);
    }

    private void manifest(String docId, String... chashes) throws Exception {
        manifestAs(TOKEN, TENANT, docId, chashes);
    }

    private static String h(int n) {
        return String.format("%02x", n).repeat(32);
    }

    private static String labelled(String stamp) {
        return "{\"section_title\":\"Intro\",\"section_type\":\"heading\",\"indexed_at\":\"" + stamp + "\"}";
    }

    private static String labelled(String stamp, String title) {
        return "{\"section_title\":\"Intro\",\"indexed_at\":\"" + stamp + "\",\"title\":\"" + title + "\"}";
    }

    private Map<String, Object> fillable(int limit) throws Exception {
        var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/section_label_fillable?limit=" + limit);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    private int fillable() throws Exception {
        return ((Number) fillable(1000).get("fillable")).intValue();
    }

    /**
     * Adds the control: a document whose single row IS fillable, so a zero from the situation under test cannot
     * come from a fixture that fills nothing. Returns the count the situation alone adds (the total minus the control's 1).
     */
    private int fillableBesideAControl() throws Exception {
        String d = doc("control doc");
        chunk(TENANT, h(0xc0), labelled("2026-10-01T11:59:00+00:00"));
        manifest(d, h(0xc0));
        return fillable() - 1;
    }

    // ── the predicate ───────────────────────────────────────────────────────

    @Test
    void aLegacyRowWhoseChunkCarriesALabelIsCounted() throws Exception {
        String d = doc("plain doc");
        chunk(TENANT, h(1), labelled("2026-10-01T11:59:00+00:00"));
        manifest(d, h(1));
        assertThat(fillable(1000)).containsEntry("fillable", 1).containsEntry("limit", 1000).containsEntry("capped", false);
    }

    @Test
    void anEmptyTenantCountsZero() throws Exception {
        assertThat(fillable(1000)).containsEntry("fillable", 0).containsEntry("capped", false);
    }

    @Test
    void aRowThatAlreadyHasItsLabelsIsNotCounted() throws Exception {
        String d = doc("written doc");
        chunk(TENANT, h(2), labelled("2026-10-01T11:59:00+00:00"));
        var resp = post(TOKEN, TENANT, "/v1/catalog/manifest/write", mapper.writeValueAsString(Map.of(
            "doc_id", d, "collection", COLL,
            "rows", List.of(Map.of("position", 0, "chash", h(2), "section_title", "Mine", "section_type", "")))));
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aChunkWithNoLabelKeyIsNotCounted() throws Exception {
        String d = doc("unlabelled chunk doc");
        chunk(TENANT, h(3), "{\"indexed_at\":\"2026-10-01T11:59:00+00:00\"}");
        manifest(d, h(3));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aChunkWithNoMetadataAtAllIsNotCounted() throws Exception {
        String d = doc("no metadata doc");
        chunk(TENANT, h(4), "{}");
        manifest(d, h(4));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aChashHeldByTwoLiveDocumentsIsNotCounted() throws Exception {
        String a = doc("shared a"), b = doc("shared b");
        chunk(TENANT, h(5), labelled("2026-10-01T11:59:00+00:00"));
        manifest(a, h(5));
        manifest(b, h(5));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aChashRepeatedInsideOneDocumentIsNotCounted() throws Exception {
        String a = doc("repeated");
        chunk(TENANT, h(6), labelled("2026-10-01T11:59:00+00:00"));
        manifest(a, h(6), h(6));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aChashAlsoHeldByATombstonedDocumentIsNotCounted() throws Exception {
        String live = doc("co-owner live"), gone = doc("co-owner gone");
        chunk(TENANT, h(7), labelled("2026-10-01T11:59:00+00:00"));
        manifest(live, h(7));
        manifest(gone, h(7));
        var del = post(TOKEN, TENANT, "/v1/catalog/delete", mapper.writeValueAsString(Map.of("tumbler", gone)));
        assertThat(del.statusCode()).as(del.body()).isEqualTo(200);
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aTombstonedDocumentsOwnRowIsNotCounted() throws Exception {
        String gone = doc("only gone");
        chunk(TENANT, h(8), labelled("2026-10-01T11:59:00+00:00"));
        manifest(gone, h(8));
        assertThat(post(TOKEN, TENANT, "/v1/catalog/delete", mapper.writeValueAsString(Map.of("tumbler", gone))).statusCode()).isEqualTo(200);
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aDocumentTheOrphanBackfillMintedIsNotCounted() throws Exception {
        String d = doc(TOKEN, TENANT, "minted", "", DOC_AT, "{\"backfill_from\":\"t3_orphan\"}");
        chunk(TENANT, h(9), labelled("2026-10-01T11:59:00+00:00"));
        manifest(d, h(9));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void anotherBackfillMarkerIsNotTheOrphanMarker() throws Exception {
        String d = doc(TOKEN, TENANT, "other marker", "", DOC_AT, "{\"backfill_from\":\"something_else\"}");
        chunk(TENANT, h(10), labelled("2026-10-01T11:59:00+00:00"));
        manifest(d, h(10));
        assertThat(fillable()).isEqualTo(1);
    }

    @Test
    void anAliasDocumentIsNotCounted() throws Exception {
        // The rung's scan skips documents that name another (alias_of): the target's rows are the ones filled.
        String d = doc("alias doc");
        chunk(TENANT, h(27), labelled("2026-10-01T11:59:00+00:00"));
        manifest(d, h(27));
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.ALIAS_OF, "1.1.1")
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(d)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    // ── the attribution guards ──────────────────────────────────────────────

    @Test
    void aChunkWrittenMoreThanFiveMinutesAfterTheDocumentIsNotCounted() throws Exception {
        String d = doc("late chunk");
        chunk(TENANT, h(11), labelled("2026-10-01T12:05:01+00:00"));
        manifest(d, h(11));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aChunkWrittenInsideTheFiveMinuteSkewIsCounted() throws Exception {
        String d = doc("skewed chunk");
        chunk(TENANT, h(12), labelled("2026-10-01T12:04:59+00:00"));
        manifest(d, h(12));
        assertThat(fillable()).isEqualTo(1);
    }

    @Test
    void theStampIsComparedAsAnInstantWhateverItsOffset() throws Exception {
        // 13:30+05:00 is 08:30 UTC, before the document's 12:00 UTC; read as 13:30 UTC it would be late.
        String d = doc("offset chunk");
        chunk(TENANT, h(13), labelled("2026-10-01T13:30:00+05:00"));
        manifest(d, h(13));
        assertThat(fillable()).isEqualTo(1);
    }

    @Test
    void aStampWithNoOffsetIsReadAsUtc() throws Exception {
        String early = doc("naive early"), late = doc("naive late");
        chunk(TENANT, h(14), labelled("2026-10-01T11:00:00.123456"));
        chunk(TENANT, h(15), labelled("2026-10-01T12:10:00"));
        manifest(early, h(14));
        manifest(late, h(15));
        assertThat(fillable()).as("only the early one: 12:10 UTC is past the skew").isEqualTo(1);
    }

    @Test
    void aChunkWithNoIndexedAtIsNotCounted() throws Exception {
        String d = doc("unstamped chunk");
        chunk(TENANT, h(16), "{\"section_title\":\"Intro\"}");
        manifest(d, h(16));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aDocumentWithNoIndexedAtIsNotCounted() throws Exception {
        String d = doc(TOKEN, TENANT, "unstamped doc", "", null, "{}");
        chunk(TENANT, h(17), labelled("2026-10-01T11:59:00+00:00"));
        manifest(d, h(17));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aStampTheDateGuardCannotReadIsCounted_neverHidden() throws Exception {
        // The one place the engine and Python may differ (module docstring of the method): the engine counts it.
        String d = doc("odd stamp");
        chunk(TENANT, h(18), labelled("2026-10-01"));
        manifest(d, h(18));
        assertThat(fillable()).isEqualTo(1);
    }

    @Test
    void aStampThatIsNotARealDateIsCounted_andDoesNotFailTheRouteForTheTenant() throws Exception {
        // The regexes admit month 13 and hour 99; PostgreSQL cannot read them as a timestamp. pg_input_is_valid keeps
        // the cast from being reached, so the stamp is 'cannot read' (counted), and the route answers for the tenant.
        String[] stamps = {"2026-13-01T00:00:00+00:00", "2026-10-01T99:00:00", "2026-02-30 12:00:00Z"};
        for (int i = 0; i < stamps.length; i++) {
            String d = doc("impossible date " + i);
            chunk(TENANT, h(60 + i), labelled(stamps[i]));
            manifest(d, h(60 + i));
        }
        String control = doc("control beside impossible dates");
        chunk(TENANT, h(0xc1), labelled("2026-10-01T11:59:00+00:00"));
        manifest(control, h(0xc1));
        assertThat(fillable()).isEqualTo(4);
    }

    @Test
    void aStampThatIsNotARealDateDoesNotHideALateRowBesideIt() throws Exception {
        // The impossible stamp must not poison the rows around it: a late (not fillable) neighbour is still excluded.
        String bad = doc("impossible date");
        String late = doc("late neighbour");
        chunk(TENANT, h(63), labelled("2026-13-45T00:00:00+00:00"));
        chunk(TENANT, h(64), labelled("2026-10-01T12:05:01+00:00"));
        manifest(bad, h(63));
        manifest(late, h(64));
        assertThat(fillable()).isEqualTo(1);
    }

    // ── the label keys ──────────────────────────────────────────────────────

    @Test
    void aLabelKeyHoldingJsonNullIsStillAKey_andIsCounted() throws Exception {
        // The rung's scan asks whether the key is IN the chunk row's metadata; a stored JSON null is a key. jsonb
        // '->' gives the jsonb value null (not SQL NULL), where '->>' gives SQL NULL and would undercount.
        String d = doc("null label");
        chunk(TENANT, h(65), "{\"section_title\":null,\"indexed_at\":\"2026-10-01T11:59:00+00:00\"}");
        manifest(d, h(65));
        assertThat(fillable()).isEqualTo(1);
    }

    @Test
    void aLabelKeyHoldingAnEmptyStringIsStillAKey_andIsCounted() throws Exception {
        String d = doc("empty label");
        chunk(TENANT, h(66), "{\"section_title\":\"\",\"indexed_at\":\"2026-10-01T11:59:00+00:00\"}");
        manifest(d, h(66));
        assertThat(fillable()).isEqualTo(1);
    }

    // ── the title guard ─────────────────────────────────────────────────────

    @Test
    void aChunkTitledForAnotherFileIsNotCounted() throws Exception {
        String d = doc(TOKEN, TENANT, "mine", "docs/mine.md", DOC_AT, "{}");
        chunk(TENANT, h(19), labelled("2026-10-01T11:59:00+00:00", "docs/theirs.md:chunk-3"));
        manifest(d, h(19));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    @Test
    void aChunkTitleNamingTheDocumentIsCounted_whicheverNameAndSuffix() throws Exception {
        String byTitle = doc(TOKEN, TENANT, "My Title", "docs/a.md", DOC_AT, "{}");
        String byPath = doc(TOKEN, TENANT, "Other Title", "docs/b.md", DOC_AT, "{}");
        String byName = doc(TOKEN, TENANT, "Third Title", "docs/c.md", DOC_AT, "{}");
        String bySuffix = doc(TOKEN, TENANT, "Fourth Title", "docs/d.py", DOC_AT, "{}");
        String byPage = doc(TOKEN, TENANT, "Fifth Title", "docs/e.pdf", DOC_AT, "{}");
        chunk(TENANT, h(20), labelled("2026-10-01T11:59:00+00:00", "My Title"));
        chunk(TENANT, h(21), labelled("2026-10-01T11:59:00+00:00", "docs/b.md"));
        chunk(TENANT, h(22), labelled("2026-10-01T11:59:00+00:00", "c.md"));
        chunk(TENANT, h(23), labelled("2026-10-01T11:59:00+00:00", "docs/d.py:12-30"));
        chunk(TENANT, h(24), labelled("2026-10-01T11:59:00+00:00", "Fifth Title:page-2"));
        manifest(byTitle, h(20));
        manifest(byPath, h(21));
        manifest(byName, h(22));
        manifest(bySuffix, h(23));
        manifest(byPage, h(24));
        assertThat(fillable()).isEqualTo(5);
    }

    @Test
    void aChunkWithNoTitleIsNotContradicted() throws Exception {
        String d = doc(TOKEN, TENANT, "any", "docs/any.md", DOC_AT, "{}");
        chunk(TENANT, h(25), labelled("2026-10-01T11:59:00+00:00"));
        manifest(d, h(25));
        assertThat(fillable()).isEqualTo(1);
    }

    @Test
    void anEmptyDocumentNameDoesNotMatchAnEmptiedChunkTitle() throws Exception {
        // ":chunk-3" with its suffix stripped is "", which must not equal a document whose file path is "" (NULLIF).
        String d = doc(TOKEN, TENANT, "t", "", DOC_AT, "{}");
        chunk(TENANT, h(26), labelled("2026-10-01T11:59:00+00:00", ":chunk-3"));
        manifest(d, h(26));
        assertThat(fillableBesideAControl()).isEqualTo(0);
    }

    // ── the bound, the tenant, the request ──────────────────────────────────

    @Test
    void theCountStopsAtTheLimitAndSaysSo() throws Exception {
        for (int i = 0; i < 3; i++) {
            String d = doc("bounded " + i);
            chunk(TENANT, h(40 + i), labelled("2026-10-01T11:59:00+00:00"));
            manifest(d, h(40 + i));
        }
        assertThat(fillable(2)).containsEntry("fillable", 2).containsEntry("limit", 2).containsEntry("capped", true);
        assertThat(fillable(3)).containsEntry("fillable", 3).containsEntry("capped", true);
        assertThat(fillable(4)).containsEntry("fillable", 3).containsEntry("capped", false);
    }

    @Test
    void anotherTenantsRowsAreNotCounted() throws Exception {
        String theirs = doc(TOKEN_B, TENANT_B, "their doc", "", DOC_AT, "{}");
        chunk(TENANT_B, h(50), labelled("2026-10-01T11:59:00+00:00"));
        manifestAs(TOKEN_B, TENANT_B, theirs, h(50));
        assertThat(fillableBesideAControl()).as("the default tenant sees only its own control").isEqualTo(0);
        var theirCount = mapperRead(get(TOKEN_B, TENANT_B, "/v1/catalog/manifest/section_label_fillable"));
        assertThat(theirCount).containsEntry("fillable", 1).containsEntry("limit", 1000);
    }

    @Test
    void aBadLimitIsA400_andOnlyGetIsAllowed() throws Exception {
        for (String bad : List.of("0", "-1", "10001", "abc", "1.5")) {
            var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/section_label_fillable?limit=" + bad);
            assertThat(resp.statusCode()).as("limit=" + bad).isEqualTo(400);
        }
        assertThat(post(TOKEN, TENANT, "/v1/catalog/manifest/section_label_fillable", "{}").statusCode()).isEqualTo(405);
    }

    @Test
    void theDefaultLimitIsOneThousand() throws Exception {
        var body = mapperRead(get(TOKEN, TENANT, "/v1/catalog/manifest/section_label_fillable"));
        assertThat(body).containsEntry("limit", 1000);
    }

    @Test
    void aCountThatOutlastsItsStatementBoundAnswers503_andTheNextCountAnswers() throws Exception {
        // Deterministic: another session holds ACCESS EXCLUSIVE on the manifest table, so the count waits on the lock
        // until its statement_timeout fires (a lock wait is bounded by statement_timeout too).
        String d = doc("bounded by time");
        chunk(TENANT, h(70), labelled("2026-10-01T11:59:00+00:00"));
        manifest(d, h(70));
        System.setProperty(dev.nexus.service.db.CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENT_CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/section_label_fillable");
            long ms = (System.nanoTime() - t0) / 1_000_000;
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
            assertThat(mapper.readValue(resp.body(), MAP_T)).containsEntry("reason", "statement_timeout");
            assertThat(ms).as("answered at the bound, not after the lock was released").isLessThan(5_000);
            holder.rollback();
        } finally {
            System.clearProperty(dev.nexus.service.db.CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
        assertThat(fillable()).as("the cancelled count left the pool usable").isEqualTo(1);
    }

    // ── http ────────────────────────────────────────────────────────────────

    private Map<String, Object> mapperRead(HttpResponse<String> resp) throws Exception {
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    private HttpResponse<String> post(String token, String tenant, String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String token, String tenant, String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
