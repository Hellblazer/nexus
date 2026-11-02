// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-pvkbv, write side. A chunk row's span fields belong to whichever document wrote the chunk last, and
 * the row records no writer. The search stage drops a NULL-span owner's field while another document still
 * holds the chunk in the manifest; a document that stops holding the chunk leaves no manifest trace, so the
 * sole holder left would show the departed writer's span. The engine clears the field on the chunk row, in
 * the transaction that removes the manifest rows, when exactly one holder remains and that holder's manifest
 * lacks the field.
 *
 * <p>Ways to stop holding: the manifest rewritten without the chunk (an edit or re-index), the manifest
 * purged, the manifest rewritten under another collection (a move, here through the real move path), the trash
 * purge of a tombstoned holder, and an append or import that puts another chunk at a held position. Controls:
 * the remaining holder has the span (row untouched), two holders remain (untouched), the same document
 * re-indexed keeping the chunk (untouched), another tenant's row of the same name (untouched), a chunk nobody
 * else holds (untouched), a departing document whose own span differs from the row's (it did not write the
 * row's: untouched, per field).
 *
 * <p>The manifest write runs through both entry points ({@link Via}): {@code writeManifest} (the departed rows
 * come back from the DELETE) and {@code writeManifestMany}, the indexer's path (they come from its guarded
 * before-read). A change that breaks one of the two must fail here.
 *
 * <p>Every scenario owns its documents and chunks, so the tests need no order. The chunk row's span is set
 * the way production sets it: the departing document wrote the chunk last, with its own span.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestDepartedWriterSpanTest {

    private static final String SVC_ROLE = "svc_departed_span";
    private static final String SVC_PASS = "svc_departed_span_pass";

    private static final String TENANT = "departed-span";
    private static final String OTHER_TENANT = "departed-span-other";
    private static final String COLL = "knowledge__departed-span-c__minilm-l6-v2-384__v1";
    private static final String COLL_D = "knowledge__departed-span-d__minilm-l6-v2-384__v1";
    /** The move scenario's sibling pair: same type and owner, two models. */
    private static final String COLL_M = "knowledge__departed-span-m__minilm-l6-v2-384__v1";
    private static final String COLL_M_TARGET = "knowledge__departed-span-m__bge-base-en-v15-768__v1";
    /** The fail-open scenario's collection: the only one its probe constraint concerns. */
    private static final String COLL_F = "knowledge__departed-span-f__minilm-l6-v2-384__v1";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String QUERY = "departedspan";

    /** The departing writer's span, the one every chunk row in a scenario carries. */
    private static final int[] P_SPAN = {500, 510, 5000, 5100};

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository vec;
    CatalogRepository catalog;
    CombinedWriteService combined;
    CombinedWriteService moveService;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
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
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1.0f, 0.0f);
        vec = new PgVectorRepository(tenantScope, embedder, embedder);
        catalog = new CatalogRepository(tenantScope);
        combined = new CombinedWriteService(tenantScope, catalog,
            new EmbedderRouter(new DocumentMoveTest.Fake(MODEL, 384), "document"));
        moveService = new CombinedWriteService(tenantScope, catalog,
            new EmbedderRouter(new DocumentMoveTest.Fake(BGE, 768), "document"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            for (String tenant : List.of(TENANT, OTHER_TENANT)) {
                PgContainerHelper.insertCollection(seed, tenant, COLL);
            }
            for (String coll : List.of(COLL_D, COLL_M, COLL_M_TARGET, COLL_F)) {
                PgContainerHelper.insertCollection(seed, TENANT, coll);
            }
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture helpers ─────────────────────────────────────────────────────

    /** The manifest write entry point a scenario goes through. */
    enum Via { WRITE_MANIFEST, WRITE_MANIFEST_MANY }

    /** A scenario's unique names, so tests share one database without touching each other's rows. */
    private final class Scn {
        final Via via;
        final String tag = "s" + seq.incrementAndGet();
        final String docA = tag + "-a";
        final String docB = tag + "-b";
        final String docP = tag + "-p";

        Scn(Via via) {
            this.via = via;
        }

        String text(String name) {
            return QUERY + " " + tag + " " + name;
        }

        String chash(String name) {
            return Chash.ofText(text(name)).toHex();
        }

        /** Registers the chunk's text and writes its chunk row carrying {@link #P_SPAN} (P wrote it last). */
        void chunk(String coll, String name) {
            chunk(TENANT, coll, name);
        }

        void chunk(String tenant, String coll, String name) {
            // Distinct unit vectors per chunk, near the query (1, 0).
            double angle = 0.01 * (1 + (Math.abs(text(name).hashCode()) % 100));
            embedder.register(text(name), (float) Math.cos(angle), (float) Math.sin(angle));
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("line_start", P_SPAN[0]);
            m.put("line_end", P_SPAN[1]);
            m.put("chunk_start_char", P_SPAN[2]);
            m.put("chunk_end_char", P_SPAN[3]);
            vec.upsertChunks(tenant, coll, List.of(chash(name)), List.of(text(name)), List.of(m));
        }

        void doc(String id, String coll) {
            doc(TENANT, id, coll);
        }

        void doc(String tenant, String id, String coll) {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
                    .set(CATALOG_DOCUMENTS.TUMBLER, id)
                    .set(CATALOG_DOCUMENTS.TITLE, id)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, coll)
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, uri(id))
                    .execute();
            } catch (java.sql.SQLException e) {
                throw new AssertionError(e);
            }
        }

        /** One chunk held by {@code doc} at {@code position}; spans {@code null} are the NULL-span shape. */
        Map<String, Object> row(int position, String name, int[] span) {
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("position", position);
            r.put("chash", chash(name));
            r.put("section_type", "body");
            if (span != null) {
                r.put("line_start", span[0]);
                r.put("line_end", span[1]);
                r.put("char_start", span[2]);
                r.put("char_end", span[3]);
            }
            return r;
        }

        @SafeVarargs
        final void write(String doc, String coll, Map<String, Object>... rows) {
            if (via == Via.WRITE_MANIFEST) {
                catalog.writeManifest(TENANT, doc, coll, List.of(rows));
                return;
            }
            Map<String, Object> r = catalog.writeManifestMany(TENANT,
                List.of(Map.of("doc_id", doc, "rows", List.of(rows))), coll);
            assertThat((List<?>) r.get("failed_doc_ids")).as("write_many of " + doc).isEmpty();
            assertThat(r.get("docs")).isEqualTo(1);
        }
    }

    private static String uri(String doc) {
        return "file:///" + doc + ".md";
    }

    /** The span keys the chunk row's metadata holds (a subset of the four), read under the tenant. */
    private List<String> rowSpanKeys(String tenant, String coll, String chash) {
        JSONB meta = tenantScope.withTenant(tenant, ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(coll))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash))))
            .fetchOne(CHUNKS.METADATA));
        assertThat(meta).as("chunk row " + chash + " in " + coll).isNotNull();
        List<String> keys = new ArrayList<>();
        for (String k : List.of("line_start", "line_end", "chunk_start_char", "chunk_end_char")) {
            if (meta.data().contains("\"" + k + "\"")) keys.add(k);
        }
        return keys;
    }

    private static final List<String> ALL_FOUR = List.of("line_start", "line_end", "chunk_start_char", "chunk_end_char");

    private Map<String, Object> hit(String coll, String chash) {
        var rows = vec.searchWithTokens(TENANT, QUERY, List.of(coll), 50, null, true).value();
        return rows.stream().filter(r -> chash.equals(r.get("id")) && coll.equals(r.get("collection")))
            .findFirst().orElseThrow(() -> new AssertionError("no search hit for " + chash + " in " + coll));
    }

    private static void assertNoSpan(Map<String, Object> row) {
        assertThat(row).doesNotContainKeys("line_start", "line_end", "chunk_start_char", "chunk_end_char");
    }

    private static void assertSpan(Map<String, Object> row, int ls, int le, int cs, int ce) {
        assertThat(((Number) row.get("line_start")).intValue()).as("line_start").isEqualTo(ls);
        assertThat(((Number) row.get("line_end")).intValue()).as("line_end").isEqualTo(le);
        assertThat(((Number) row.get("chunk_start_char")).intValue()).as("chunk_start_char").isEqualTo(cs);
        assertThat(((Number) row.get("chunk_end_char")).intValue()).as("chunk_end_char").isEqualTo(ce);
    }

    // ── (a) the document is edited so it dropped the chunk ──────────────────

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void editedToDropTheChunk_soleNullSpanHolderShowsNoDepartedSpan_andOnlyThatChunkIsCleared(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docB, COLL);
        s.doc(s.docP, COLL);
        for (String c : List.of("z", "two", "alone", "full", "keep")) s.chunk(COLL, c);
        // P wrote every chunk last. Z: A (no span) is the sole holder left. TWO: A and B (no span) both stay.
        // ALONE: nobody else holds it. FULL: A holds it with its own span. KEEP: P keeps it, A (no span) shares.
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN), s.row(1, "two", P_SPAN), s.row(2, "alone", P_SPAN),
            s.row(3, "full", P_SPAN), s.row(4, "keep", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null), s.row(1, "two", null), s.row(2, "full", new int[]{1, 2, 3, 4}),
            s.row(3, "keep", null));
        s.write(s.docB, COLL, s.row(0, "two", null));
        assertThat(hit(COLL, s.chash("z"))).as("two holders: the stage drops the field already").satisfies(
            ManifestDepartedWriterSpanTest::assertNoSpan);

        // P is edited and keeps only KEEP.
        s.write(s.docP, COLL, s.row(0, "keep", P_SPAN));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("sole holder lacks the span: cleared").isEmpty();
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("two"))).as("two holders remain").isEqualTo(ALL_FOUR);
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("alone"))).as("no holder remains").isEqualTo(ALL_FOUR);
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("full"))).as("the holder has the span").isEqualTo(ALL_FOUR);
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("keep"))).as("P keeps it").isEqualTo(ALL_FOUR);

        assertNoSpan(hit(COLL, s.chash("z")));
        assertNoSpan(hit(COLL, s.chash("two")));
        assertSpan(hit(COLL, s.chash("full")), 1, 2, 3, 4);
        assertNoSpan(hit(COLL, s.chash("keep")));
    }

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void singleRequestReindexKeepingTheChunk_leavesTheRowAlone(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));

        // The same document written again, same chunk, at another position.
        s.write(s.docP, COLL, s.row(7, "z", P_SPAN));
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEqualTo(ALL_FOUR);
        assertNoSpan(hit(COLL, s.chash("z")));
    }

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void singleRequestReindexKeepingTheChunk_soleHolderWithNoManifestSpan_keepsItsOwnRowSpan(Via via) {
        var s = new Scn(via);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        // P is the only holder and its manifest row has no span (a row written before the manifest had
        // spans): the chunk row's span is P's own, and a re-index that keeps the chunk must not erase it.
        s.write(s.docP, COLL, s.row(0, "z", null));

        s.write(s.docP, COLL, s.row(3, "z", null));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEqualTo(ALL_FOUR);
        assertSpan(hit(COLL, s.chash("z")), P_SPAN[0], P_SPAN[1], P_SPAN[2], P_SPAN[3]);
    }

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void partialSpan_clearsOnlyTheFieldsTheHolderLacks(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL, "p-keeps");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        // A holds lines 600-601 and no characters.
        Map<String, Object> aRow = s.row(0, "z", null);
        aRow.put("line_start", 600);
        aRow.put("line_end", 601);
        s.write(s.docA, COLL, aRow);

        s.write(s.docP, COLL, s.row(0, "p-keeps", P_SPAN));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).containsExactlyInAnyOrder("line_start", "line_end");
        var h = hit(COLL, s.chash("z"));
        assertThat(((Number) h.get("line_start")).intValue()).isEqualTo(600);
        assertThat(((Number) h.get("line_end")).intValue()).isEqualTo(601);
        assertThat(h).doesNotContainKeys("chunk_start_char", "chunk_end_char");
    }

    // ── (b) the document is purged ──────────────────────────────────────────

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void manifestPurged_soleNullSpanHolderShowsNoDepartedSpan(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));

        assertThat(catalog.purgeManifest(TENANT, s.docP)).isEqualTo(1);

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEmpty();
        assertNoSpan(hit(COLL, s.chash("z")));
    }

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void manifestPurged_holderWithSpans_leavesTheRowAlone(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", new int[]{1, 2, 3, 4}));

        catalog.purgeManifest(TENANT, s.docP);

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEqualTo(ALL_FOUR);
        assertSpan(hit(COLL, s.chash("z")), 1, 2, 3, 4);
    }

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void manifestPurged_twoHoldersRemain_leavesTheRowAlone(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docB, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));
        s.write(s.docB, COLL, s.row(0, "z", null));

        catalog.purgeManifest(TENANT, s.docP);

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEqualTo(ALL_FOUR);
        assertNoSpan(hit(COLL, s.chash("z")));
    }

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void trashPurged_soleNullSpanHolderShowsNoDepartedSpan(Via via) throws Exception {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docB, COLL);
        s.doc(s.docP, COLL);
        for (String c : List.of("z", "full", "two")) s.chunk(COLL, c);
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN), s.row(1, "full", P_SPAN), s.row(2, "two", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null), s.row(1, "full", new int[]{1, 2, 3, 4}), s.row(2, "two", null));
        s.write(s.docB, COLL, s.row(0, "two", null));
        // P is deleted into the trash: a tombstoned co-holder, which the stage already counts.
        assertThat(catalog.deleteDocument(TENANT, s.docP)).isEqualTo(1);
        assertNoSpan(hit(COLL, s.chash("z")));
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("still tombstoned, not yet purged").isEqualTo(ALL_FOUR);
        age(s.docP, 60);

        Map<String, Object> purged = catalog.purgeTrash(TENANT, 30);
        assertThat(((Number) purged.get("documents_purged")).intValue()).isGreaterThanOrEqualTo(1);

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("sole holder lacks the span").isEmpty();
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("full"))).as("the holder has the span").isEqualTo(ALL_FOUR);
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("two"))).as("two holders remain").isEqualTo(ALL_FOUR);
        assertNoSpan(hit(COLL, s.chash("z")));
        assertSpan(hit(COLL, s.chash("full")), 1, 2, 3, 4);
        assertNoSpan(hit(COLL, s.chash("two")));
    }

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void trashPurged_aTombstoneInsideTheGraceWindowStaysAHolder(Via via) throws Exception {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docB, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));
        s.write(s.docB, COLL, s.row(0, "z", null));
        // P is aged out of the grace window; B is tombstoned only now, so it stays and A + B still hold the chunk.
        catalog.deleteDocument(TENANT, s.docP);
        age(s.docP, 60);
        catalog.deleteDocument(TENANT, s.docB);

        catalog.purgeTrash(TENANT, 30);

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("A and the fresh tombstone B both remain").isEqualTo(ALL_FOUR);
    }

    // ── (c) the document moved to another collection ────────────────────────

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void rewrittenUnderAnotherCollection_soleNullSpanHolderInTheSourceShowsNoDepartedSpan(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL_D, "z");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));

        // P's manifest now names the other collection (the same rewrite a move performs; the real move path
        // is movedThroughTheMovePath_... below).
        s.write(s.docP, COLL_D, s.row(0, "z", P_SPAN));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("source collection: cleared").isEmpty();
        assertThat(rowSpanKeys(TENANT, COLL_D, s.chash("z"))).as("target collection: P holds it").isEqualTo(ALL_FOUR);
        assertNoSpan(hit(COLL, s.chash("z")));
    }

    // ── tenant scope ────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    @ParameterizedTest
    @EnumSource(Via.class)
    void anotherTenantsRowOfTheSameNameIsNotTouched(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.doc(OTHER_TENANT, s.docA, COLL);
        s.chunk(COLL, "z");
        s.chunk(OTHER_TENANT, COLL, "z");
        s.chunk(COLL, "p-keeps");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));
        catalog.writeManifest(OTHER_TENANT, s.docA, COLL, List.of(s.row(0, "z", null)));

        s.write(s.docP, COLL, s.row(0, "p-keeps", P_SPAN));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEmpty();
        assertThat(rowSpanKeys(OTHER_TENANT, COLL, s.chash("z"))).as("the other tenant's row").isEqualTo(ALL_FOUR);
    }


    @Test
    void movedThroughTheMovePath_soleNullSpanHolderInTheSourceShowsNoDepartedSpan() {
        var s = new Scn(Via.WRITE_MANIFEST_MANY);
        s.doc(s.docA, COLL_M);
        s.doc(s.docP, COLL_M);
        s.chunk(COLL_M, "z");
        s.write(s.docP, COLL_M, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL_M, s.row(0, "z", null));
        String z = s.chash("z");

        // The re-embed job's call: P leaves the source for its model sibling, A stays.
        var moved = moveService.moveDocsCombined(TENANT, COLL_M, COLL_M_TARGET,
            List.of(Map.<String, Object>of("chash", z, "text", s.text("z"), "metadata", Map.of())),
            List.of(Map.<String, Object>of("doc_id", s.docP,
                "rows", List.of(Map.<String, Object>of("position", 0, "chash", z, "chunk_index", 0)))),
            Map.of(s.docP, List.of(z))).response();

        assertThat((List<?>) moved.get("failed_doc_ids")).as("the move").isEmpty();
        assertThat(moved.get("docs")).isEqualTo(1);
        assertThat(rowSpanKeys(TENANT, COLL_M, z)).as("source collection: A is the sole holder left").isEmpty();
    }

    // ── (d) an append or import puts another chunk at a held position ───────

    @ParameterizedTest
    @EnumSource(Via.class)
    void appendOverAHeldPosition_theDisplacedChunksRowSpanIsCleared(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL, "y");
        s.chunk(COLL, "alone");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN), s.row(1, "alone", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));

        // P's position 0 now holds y (the append upserts BY POSITION; z is no longer P's).
        catalog.appendManifestChunks(TENANT, s.docP, COLL, List.of(s.row(0, "y", P_SPAN)));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("A is the sole holder of z now").isEmpty();
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("y"))).as("nobody else holds y").isEqualTo(ALL_FOUR);
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("alone"))).as("P still holds it").isEqualTo(ALL_FOUR);
        assertNoSpan(hit(COLL, s.chash("z")));
    }

    @ParameterizedTest
    @EnumSource(Via.class)
    void appendOverAHeldPosition_chunkStillHeldAtAnotherPosition_keepsTheRowSpan(Via via) {
        var s = new Scn(via);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL, "y");
        // P is the only holder; its manifest has no span for z (a row written before the manifest had spans),
        // so the chunk row's span is its own. z stands at two positions; one of them is overwritten.
        s.write(s.docP, COLL, s.row(0, "z", null), s.row(1, "z", null));

        catalog.appendManifestChunks(TENANT, s.docP, COLL, List.of(s.row(0, "y", null)));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("P still holds z at position 1").isEqualTo(ALL_FOUR);
    }

    @ParameterizedTest
    @EnumSource(Via.class)
    void importOverAHeldPosition_aChangedContentReimportClearsTheDisplacedChunksRowSpan(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL, "y");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));

        // RDR-176 ETL: the same document imported again with changed content at position 0.
        catalog.importChunksBatch(TENANT, s.docP, COLL, List.of(s.row(0, "y", P_SPAN)));
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("batch import").isEmpty();

        // and the single-row leg
        s.chunk(COLL, "z2");
        s.write(s.docP, COLL, s.row(0, "z2", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z2", null));
        catalog.importChunk(TENANT, s.docP, COLL, s.row(0, "y", P_SPAN));
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z2"))).as("single-row import").isEmpty();
    }

    // ── (e) the writer test: a departing document with a span of its own that is not the row's ───────

    @ParameterizedTest
    @EnumSource(Via.class)
    void departingDocumentWithADifferentSpan_isNotTheWriter_soleLegacyHolderKeepsItsRowSpan(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL, "p-keeps");
        // The chunk row holds {500, 510, 5000, 5100}: A's own, written before the manifest had spans (A's
        // manifest row has none). P holds the same chunk with a manifest span that is NOT the row's, so P did
        // not write the row's span, and its departure must leave it.
        s.write(s.docA, COLL, s.row(0, "z", null));
        s.write(s.docP, COLL, s.row(0, "z", new int[]{1, 2, 3, 4}), s.row(1, "p-keeps", P_SPAN));

        s.write(s.docP, COLL, s.row(0, "p-keeps", P_SPAN));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEqualTo(ALL_FOUR);
        assertSpan(hit(COLL, s.chash("z")), P_SPAN[0], P_SPAN[1], P_SPAN[2], P_SPAN[3]);
    }

    @ParameterizedTest
    @EnumSource(Via.class)
    void departingDocumentSpan_isComparedPerField(Via via) {
        var s = new Scn(via);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL, "p-keeps");
        s.write(s.docA, COLL, s.row(0, "z", null));
        // P agrees with the row on three fields and differs on line_end
        s.write(s.docP, COLL, s.row(0, "z", new int[]{P_SPAN[0], 999, P_SPAN[2], P_SPAN[3]}),
            s.row(1, "p-keeps", P_SPAN));

        s.write(s.docP, COLL, s.row(0, "p-keeps", P_SPAN));

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("only the field P could not have written stays")
            .containsExactly("line_end");
    }

    @Test
    void trashPurged_departingTombstoneWithADifferentSpan_isNotTheWriter() throws Exception {
        var s = new Scn(Via.WRITE_MANIFEST);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.write(s.docA, COLL, s.row(0, "z", null));
        s.write(s.docP, COLL, s.row(0, "z", new int[]{1, 2, 3, 4}));
        catalog.deleteDocument(TENANT, s.docP);
        age(s.docP, 60);

        catalog.purgeTrash(TENANT, 30);

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEqualTo(ALL_FOUR);
    }

    // ── (f) a re-index of more than one request: the chunk a later request adds back ─────────────────

    @Test
    void multiRequestReindex_aChunkALaterRequestAddsBackGetsItsSpanBack() {
        var s = new Scn(Via.WRITE_MANIFEST_MANY);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL, "x");
        s.write(s.docP, COLL, s.row(0, "x", P_SPAN), s.row(1, "z", P_SPAN));
        s.write(s.docA, COLL, s.row(1, "z", null));
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).isEqualTo(ALL_FOUR);

        // Request 1 of P's re-index (replace, sweep off) does not list z: until request 2 it is P's dropped
        // chunk, and A is its sole holder.
        var first = combined.writeManyCombined(TENANT, COLL, List.of(chunkPayload(s, "x")),
            List.of(Map.<String, Object>of("doc_id", s.docP, "rows", List.of(s.row(0, "x", P_SPAN)))),
            null, false, false);
        assertThat((List<?>) first.response().get("failed_doc_ids")).isEmpty();
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("between the two requests").isEmpty();

        // Request 2 appends z back with the chunk payload (metadata, spans included) the writer always sends:
        // the engine refreshes the stored chunk's metadata before the append's transaction.
        combined.appendCombined(TENANT, COLL, s.docP, List.of(s.row(1, "z", P_SPAN)),
            List.of(chunkPayload(s, "z")), false);

        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("after the later request").isEqualTo(ALL_FOUR);
        assertNoSpan(hit(COLL, s.chash("z")));
    }

    private Map<String, Object> chunkPayload(Scn s, String name) {
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("line_start", P_SPAN[0]);
        meta.put("line_end", P_SPAN[1]);
        meta.put("chunk_start_char", P_SPAN[2]);
        meta.put("chunk_end_char", P_SPAN[3]);
        return Map.of("chash", s.chash(name), "text", s.text(name), "metadata", meta);
    }

    // ── (g) the clear is maintenance: it never takes the manifest write down with it ─────────────────

    @ParameterizedTest
    @EnumSource(Via.class)
    void theClearFailing_leavesTheManifestWriteCommittedAndTheRowUntouched(Via via) throws Exception {
        var s = new Scn(via);
        s.doc(s.docA, COLL_F);
        s.doc(s.docP, COLL_F);
        s.chunk(COLL_F, "z");
        s.chunk(COLL_F, "p-keeps");
        s.write(s.docP, COLL_F, s.row(0, "z", P_SPAN), s.row(1, "p-keeps", P_SPAN));
        s.write(s.docA, COLL_F, s.row(0, "z", null));
        // A probe constraint, for this collection only: a chunk row of it must keep its line_start. The clear
        // would delete it, so the clear's UPDATE fails with a check violation (not a deadlock).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS).add(DSL.constraint("pvkbv_failopen_probe")
                .check(CHUNKS.COLLECTION.ne(COLL_F).or(DSL.condition(
                    DSL.function("jsonb_exists", Boolean.class, CHUNKS.METADATA, DSL.inline("line_start")))))).execute();
        }
        try {
            s.write(s.docP, COLL_F, s.row(0, "p-keeps", P_SPAN));
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS).dropConstraint("pvkbv_failopen_probe").execute();
            }
        }

        assertThat(catalog.getManifest(TENANT, s.docP)).as("the manifest write committed").singleElement()
            .satisfies(r -> assertThat(r.get("chash")).isEqualTo(s.chash("p-keeps")));
        assertThat(rowSpanKeys(TENANT, COLL_F, s.chash("z"))).as("the clear failed open: row untouched")
            .isEqualTo(ALL_FOUR);
    }

    // ── (h) the batch writer's before-read fails ────────────────────────────────────────────────────

    @Test
    void theBatchWritersBeforeReadFailing_theWriteLands_andTheClearIsSkipped() throws Exception {
        var s = new Scn(Via.WRITE_MANIFEST_MANY);
        s.doc(s.docA, COLL);
        s.doc(s.docP, COLL);
        s.chunk(COLL, "z");
        s.chunk(COLL, "p-keeps");
        s.write(s.docP, COLL, s.row(0, "z", P_SPAN), s.row(1, "p-keeps", P_SPAN));
        s.write(s.docA, COLL, s.row(0, "z", null));

        // Only the chash column read fails (the grant leaves every other column): the before-read selects it,
        // the write's own DELETE and INSERT do not.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES)
                .revoke(DSL.privilege("SELECT")).on(CATALOG_DOCUMENT_CHUNKS).from(DSL.role(SVC_ROLE)).execute();
            PgContainerHelper.runSuperuserTestChangelog(su,
                "db/changelog-test/db.changelog-test-manifest-chunks-column-select.xml",
                "databasechangelog_test_manifest_chunks_column_select", Map.of("role", SVC_ROLE));
        }
        Map<String, Object> result;
        try {
            result = catalog.writeManifestMany(TENANT, List.of(Map.of("doc_id", s.docP,
                "rows", List.of(s.row(0, "p-keeps", P_SPAN)))), COLL);
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES)
                    .grant(DSL.privilege("SELECT")).on(CATALOG_DOCUMENT_CHUNKS).to(DSL.role(SVC_ROLE)).execute();
            }
        }

        assertThat(result.get("docs")).as("the manifest write is not failed by the unreadable before-set").isEqualTo(1);
        assertThat((List<?>) result.get("failed_doc_ids")).isEmpty();
        assertThat(catalog.getManifest(TENANT, s.docP)).singleElement()
            .satisfies(r -> assertThat(r.get("chash")).isEqualTo(s.chash("p-keeps")));
        assertThat(rowSpanKeys(TENANT, COLL, s.chash("z"))).as("no departed set, no clear").isEqualTo(ALL_FOUR);
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private void age(String doc, int days) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now().minusDays(days))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc)))
                .execute();
        }
    }
}
