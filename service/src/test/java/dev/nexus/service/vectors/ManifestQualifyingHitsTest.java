// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_QUALIFYING_HITS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-8ptg4: {@code nexus.manifest_qualifying_hits} (vectors-047) is the bounded count that lets the engine skip the
 * exact re-run of a short page that is genuinely complete. Its one hard contract is that it NEVER UNDER-COUNTS: a count
 * below the chunks {@code manifest_search_<dim>} would return skips a re-run a starved page needs, a silent recall loss.
 * It may over-count (the cost is a re-run).
 *
 * <p>The class compares the count with the rows {@code manifest_search_384} returns over the same predicate, with index
 * scans' ordering irrelevant (the table is small, every page is complete): for every predicate of the matrix the count
 * is at least the row count, equal where the fixture holds no second qualifying owner of a chunk, and larger by exactly
 * the over-count where it does. The matrix reaches the written and the legacy label home, a document key, a numeric
 * range, an {@code $in}, a chunk-row where, a soft-deleted owner, a second collection, another tenant and another
 * model, and a non-vacuity floor keeps it from shrinking.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestQualifyingHitsTest {

    private static final String SVC_ROLE = "svc_qual_hits";
    private static final String SVC_PASS = "svc_qual_hits_pass";

    private static final String TENANT = "qual-hits";
    private static final String OTHER_TENANT = "qual-hits-other";
    private static final String C1 = "docs__qual-hits__minilm-l6-v2-384__v1";
    private static final String C2 = "docs__qual-hits-2__minilm-l6-v2-384__v1";
    private static final String OTHER_C = "docs__qual-hits-other__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static String chash(String collection, int i) {
        return Chash.ofText("qh-" + collection + "-" + i).toHex();
    }

    private static String text(String collection, int i) {
        return "qh " + collection + " chunk " + i;
    }

    /** Embedder with one unit vector, (1, 0, ...): the ranking is irrelevant to the count. */
    private static final class Flat implements Embedder {
        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>();
            for (String t : texts) {
                float[] v = new float[384];
                v[0] = 1.0f;
                out.add(v);
            }
            return out;
        }
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    private void chunks(PgVectorRepository vec, String tenant, String collection, int from, int to,
                        Map<Integer, Map<String, Object>> metas) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> ms = new ArrayList<>();
        for (int i = from; i < to; i++) {
            ids.add(chash(collection, i));
            texts.add(text(collection, i));
            ms.add(metas.getOrDefault(i, new LinkedHashMap<>()));
        }
        vec.upsertChunks(tenant, collection, ids, texts, ms);
    }

    private static void doc(DSLContext seed, String tenant, String tumbler, String title, String collection,
                            boolean deleted) {
        var step = seed.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, title)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + tumbler + ".md");
        if (deleted) {
            step = step.set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now().minusDays(1));
        }
        step.execute();
    }

    /** One manifest row: label columns written ({@code written}) or left to the chunk row's value (legacy). */
    private static void occurrence(DSLContext seed, String tenant, String doc, int position, String collection,
                                   int chunk, String sectionType, Integer page, boolean written) {
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS,
                CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE,
                CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN)
            .values(tenant, doc, position, HexFormat.of().parseHex(chash(collection, chunk)), collection, MODEL,
                written ? sectionType : null, written ? page : null, written)
            .execute();
    }

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        var embedder = new Flat();
        var vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, C1);
            PgContainerHelper.insertCollection(seed, TENANT, C2);
            PgContainerHelper.insertCollection(seed, OTHER_TENANT, OTHER_C);

            // C1 chunks 0..16. Chunks 1 and 2 also say lang=fr on the chunk row; legacy chunks 10 and 11 carry a
            // section_type on the chunk row only (their manifest rows are not written).
            Map<Integer, Map<String, Object>> metas = new LinkedHashMap<>();
            metas.put(1, new LinkedHashMap<>(Map.of("lang", "fr")));
            metas.put(2, new LinkedHashMap<>(Map.of("lang", "fr")));
            metas.put(10, new LinkedHashMap<>(Map.of("section_type", "legacy-x")));
            metas.put(11, new LinkedHashMap<>(Map.of("section_type", "legacy-x")));
            chunks(vec, TENANT, C1, 0, 17, metas);
            chunks(vec, TENANT, C2, 0, 3, Map.of());
            chunks(vec, OTHER_TENANT, OTHER_C, 0, 3, Map.of());

            // D1 "Alpha": chunks 0-4 intro, 5-9 body, pages = chunk number.
            doc(seed, TENANT, "d1", "Alpha", C1, false);
            for (int i = 0; i < 10; i++) {
                occurrence(seed, TENANT, "d1", i, C1, i, i < 5 ? "intro" : "body", i, true);
            }
            // D2 "Beta": legacy rows 10-14, labels not written; the chunk row answers (10 and 11 say legacy-x).
            doc(seed, TENANT, "d2", "Beta", C1, false);
            for (int i = 10; i < 15; i++) {
                occurrence(seed, TENANT, "d2", i - 10, C1, i, null, null, false);
            }
            // D3 "Gamma" is soft-deleted: its intro chunks 15 and 16 qualify for nobody.
            doc(seed, TENANT, "d3", "Gamma", C1, true);
            occurrence(seed, TENANT, "d3", 0, C1, 15, "intro", 0, true);
            occurrence(seed, TENANT, "d3", 1, C1, 16, "intro", 1, true);
            // D5 "Delta" holds chunks 0 and 5 as intro: chunk 0 now has TWO qualifying owners (the over-count), and
            // chunk 5, body for D1, qualifies through D5.
            doc(seed, TENANT, "d5", "Delta", C1, false);
            occurrence(seed, TENANT, "d5", 0, C1, 0, "intro", 0, true);
            occurrence(seed, TENANT, "d5", 1, C1, 5, "intro", 1, true);
            // C2: three intro chunks of D4 "Alpha" too (the same title in another collection).
            doc(seed, TENANT, "d4", "Alpha", C2, false);
            for (int i = 0; i < 3; i++) {
                occurrence(seed, TENANT, "d4", i, C2, i, "intro", i, true);
            }
            // Another tenant: three intro chunks that must never count for TENANT.
            doc(seed, OTHER_TENANT, "x1", "Alpha", OTHER_C, false);
            for (int i = 0; i < 3; i++) {
                occurrence(seed, OTHER_TENANT, "x1", i, OTHER_C, i, "intro", i, true);
            }
        }
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static Map<String, Object> where(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static Map<String, Object> op(String operator, Object operand) {
        return where(operator, operand);
    }

    /** The compiled predicate of a manifest-answered {@code where}. */
    private static ManifestPushdown.Plan plan(Map<String, Object> where) {
        ManifestPushdown.Plan plan = ManifestPushdown.of(ManifestLabelStage.split(where).labels());
        assertThat(plan).as("a pushable predicate: %s", where).isNotNull();
        return plan;
    }

    private static String[] colls(String... names) {
        return names;
    }

    /** The function's hit count under the tenant scope, at most {@code cap}. */
    private int hits(String tenant, String[] collections, ManifestPushdown.Plan plan, JSONB chunkWhere, int cap,
                     String model) {
        return tenantScope.withTenant(tenant, ctx -> ctx.selectFrom(MANIFEST_QUALIFYING_HITS.call(
            collections, chunkWhere, null, plan.containment(), plan.jsonPath(), cap, model, tenant)).fetch().size());
    }

    /** The rows {@code manifest_search_384} returns over the same predicate: every qualifying, live chunk. */
    private int searchRows(String tenant, String[] collections, ManifestPushdown.Plan plan, JSONB chunkWhere) {
        Vector q = Vector.of(unit());
        return tenantScope.withTenant(tenant, ctx -> {
            // The table is small and the statement is exact: nothing here depends on the HNSW walk.
            PgSession.disableIndexScanForExactFallback(ctx);
            return ctx.selectFrom(MANIFEST_SEARCH_384.call(q, null, null, collections, chunkWhere, null,
                plan.containment(), plan.jsonPath(), 1000, MODEL, tenant)).fetch().size();
        });
    }

    private static float[] unit() {
        float[] v = new float[384];
        v[0] = 1.0f;
        return v;
    }

    /** One matrix row: a predicate, its collections, an optional chunk-row where, and the expected over-count. */
    private record Case(String name, Map<String, Object> where, String[] collections, JSONB chunkWhere,
                        int overCount) {}

    private static List<Case> matrix() {
        JSONB fr = JSONB.valueOf("{\"lang\":\"fr\"}");
        return List.of(
            // chunk 0 has two qualifying owners (D1 and D5), so the count is one above the chunk count
            new Case("intro in C1", where("section_type", "intro"), colls(C1), null, 1),
            new Case("intro in C1 and C2", where("section_type", "intro"), colls(C1, C2), null, 1),
            new Case("body in C1", where("section_type", "body"), colls(C1), null, 0),
            new Case("legacy label read from the chunk row", where("section_type", "legacy-x"), colls(C1), null, 0),
            new Case("an in-list across both homes", where("section_type", op("$in", List.of("body", "legacy-x"))),
                colls(C1), null, 0),
            new Case("a document key", where("title", "Beta"), colls(C1), null, 0),
            new Case("the same title in two collections", where("title", "Alpha"), colls(C2, C1), null, 0),
            new Case("a numeric range", where("page_number", op("$gte", 7)), colls(C1), null, 0),
            new Case("a label and a document key", where("section_type", "intro", "title", "Delta"), colls(C1), null, 0),
            new Case("nothing matches", where("section_type", "no-such-label"), colls(C1), null, 0),
            new Case("a soft-deleted owner is not counted", where("title", "Gamma"), colls(C1), null, 0),
            new Case("a chunk-row where narrows the count", where("section_type", "intro"), colls(C1), fr, 0));
    }

    @Test
    void theCountNeverUnderCountsManifestSearch_andEqualsItWhereNoChunkHasTwoOwners() {
        int examined = 0;
        int nonEmpty = 0;
        for (Case c : matrix()) {
            ManifestPushdown.Plan plan = plan(c.where());
            int rows = searchRows(TENANT, c.collections(), plan, c.chunkWhere());
            int count = hits(TENANT, c.collections(), plan, c.chunkWhere(), 1000, MODEL);
            examined++;
            if (rows > 0) nonEmpty++;
            assertThat(count).as("%s: never fewer than the chunks manifest_search returns (%d)", c.name(), rows)
                .isGreaterThanOrEqualTo(rows);
            assertThat(count - rows).as("%s: the over-count", c.name()).isEqualTo(c.overCount());
        }
        // Non-vacuity: the matrix ran and most of it found rows to compare.
        assertThat(examined).isEqualTo(12);
        assertThat(nonEmpty).as("predicates that matched at least one chunk").isGreaterThanOrEqualTo(9);
    }

    @Test
    void theCountIsBounded_byTheCap() {
        ManifestPushdown.Plan intro = plan(where("section_type", "intro"));
        assertThat(hits(TENANT, colls(C1), intro, null, 3, MODEL)).isEqualTo(3);
        assertThat(hits(TENANT, colls(C1), intro, null, 1, MODEL)).isEqualTo(1);
        assertThat(hits(TENANT, colls(C1), intro, null, 0, MODEL)).isZero();
        // Below the cap the count is whole: 7 owners of intro chunks in C1 (D1 5, D5 2).
        assertThat(hits(TENANT, colls(C1), intro, null, 8, MODEL)).isEqualTo(7);
    }

    @Test
    void anotherTenantAndAnotherModelAreNotCounted() {
        ManifestPushdown.Plan intro = plan(where("section_type", "intro"));
        int mine = hits(TENANT, colls(C1, C2), intro, null, 1000, MODEL);
        // The other tenant has 3 intro chunks under the same label; asking for ITS collection as TENANT counts none.
        assertThat(hits(TENANT, colls(OTHER_C), intro, null, 1000, MODEL)).isZero();
        assertThat(hits(OTHER_TENANT, colls(OTHER_C), intro, null, 1000, MODEL)).isEqualTo(3);
        assertThat(mine).isEqualTo(7 + 3);
        assertThat(hits(TENANT, colls(C1, C2), intro, null, 1000, "voyage-3-1024")).isZero();
    }
}
