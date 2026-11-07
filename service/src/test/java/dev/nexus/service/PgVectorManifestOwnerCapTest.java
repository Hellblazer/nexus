// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.LexicalStat;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6cmam: a label-filtered hit expands to at most {@code ManifestLabelStage.MAX_OWNERS_PER_HIT} documents,
 * the first ones in {@code doc_id} order, and the lexical extras of {@code searchPerCollection} are cut at the
 * bound the stage's input implies, not at {@link Integer#MAX_VALUE}.
 *
 * <p>Fixture. {@code OWNERS} (the cap plus three) documents all hold two boilerplate chunks at position 0 and 1:
 * {@code S}, near the query, and {@code L}, far from it and the only text with the query's lexeme (so the
 * lexical arm of a per-collection search returns it and the vector arm does not). Document {@code 01} also holds
 * the distinct chunks {@code P1}, {@code P2} and {@code F1..F3}, all nearer the query than {@code L}. Every
 * manifest row is WRITTEN with {@code section_type = body}, so a {@code $ne references} filter passes them all.
 * The cap is mirrored in {@link #CAP}; {@code ManifestLabelStageTest} fails if the stage's constant moves.
 *
 * <p>Second fixture, in {@link #COLLECTION_B}: ten documents hold the chunk {@code T} at position 0, the first
 * {@link #FAILING_OWNERS} of them with {@code section_type = references} and the rest {@code body}, each with its
 * own title. It shows the cap counts the owners that PASS (a mutant that stops after {@code CAP} owners walked,
 * passing or not, would return only two of them), that a document-key filter naming one late owner returns it, and
 * that a capped hit still reads as shared (its rows carry no chunk-row span, where the unshared chunk {@code U}
 * keeps its own).
 *
 * <p>Scope of the cap: it lives in {@code ManifestLabelStage.apply}, which a search runs only when its {@code where}
 * names a label or a document key (route (a)). Route (b) (metadata-scoped, graph-hop, aspect-scoped and
 * {@code manifest_search_*} reads) returns one row per owner and is not capped; nothing here claims otherwise.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorManifestOwnerCapTest {

    private static final String SVC_ROLE = "svc_owner_cap";
    private static final String SVC_PASS = "svc_owner_cap_pass";

    private static final String TENANT = "owner-cap";
    private static final String COLLECTION = "knowledge__owner-cap__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static final String COLLECTION_B = "knowledge__owner-cap-b__minilm-l6-v2-384__v1";
    private static final int FAILING_OWNERS = 3;
    private static final int PASSING_OWNERS = 7;
    private static final String TEXT_T = "owner cap passing-owner chunk held by ten documents";
    private static final String TEXT_U = "owner cap unshared chunk held by one document";
    private static final String T = Chash.ofText("owner-cap-t").toHex();
    private static final String U = Chash.ofText("owner-cap-u").toHex();
    /** The chunk-row span the unshared chunk keeps and the shared one must lose. */
    private static final int ROW_LINE_START = 99;

    /** {@code ManifestLabelStage.MAX_OWNERS_PER_HIT}, which is package-private; ManifestLabelStageTest pins it equal. */
    private static final int CAP = 5;
    private static final int OWNERS = CAP + 3;

    private static final String QUERY = "capprobe";
    private static final String TEXT_S = "shared boilerplate chunk every document holds";
    private static final String TEXT_L = "capprobe shared lexical-only chunk every document holds";
    private static final String TEXT_P1 = "distinct chunk one of document one";
    private static final String TEXT_P2 = "distinct chunk two of document one";
    private static final String[] TEXT_F = {"filler chunk one", "filler chunk two", "filler chunk three"};

    private static final String S = Chash.ofText("owner-cap-s").toHex();
    private static final String L = Chash.ofText("owner-cap-l").toHex();
    private static final String P1 = Chash.ofText("owner-cap-p1").toHex();
    private static final String P2 = Chash.ofText("owner-cap-p2").toHex();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository vec;

    private static String doc(int n) {
        return String.format("cap-doc-%02d", n);
    }

    private static String docB(int n) {
        return String.format("pas-doc-%02d", n);
    }

    private static String titleB(int n) {
        return String.format("Pas Document %02d", n);
    }

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
        var tenantScope = new TenantScope(svcDs);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1f, 0f);
        embedder.register(TEXT_S, 1f, 0f);
        register(embedder, TEXT_P1, 0.01);
        register(embedder, TEXT_P2, 0.02);
        for (int i = 0; i < TEXT_F.length; i++) {
            register(embedder, TEXT_F[i], 0.03 + 0.01 * i);
        }
        register(embedder, TEXT_L, 1.2);
        embedder.register(TEXT_T, 1f, 0f);
        register(embedder, TEXT_U, 0.01);
        vec = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION);
            PgContainerHelper.insertCollection(seed, TENANT, COLLECTION_B);
            for (int d = 1; d <= FAILING_OWNERS + PASSING_OWNERS; d++) {
                seed.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                    .set(CATALOG_DOCUMENTS.TUMBLER, docB(d))
                    .set(CATALOG_DOCUMENTS.TITLE, titleB(d))
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION_B)
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + docB(d) + ".md")
                    .execute();
            }
            for (int d = 1; d <= OWNERS; d++) {
                seed.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                    .set(CATALOG_DOCUMENTS.TUMBLER, doc(d))
                    .set(CATALOG_DOCUMENTS.TITLE, "Document " + d)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + doc(d) + ".md")
                    .execute();
            }
        }

        List<String> ids = new ArrayList<>(List.of(S, L, P1, P2));
        List<String> texts = new ArrayList<>(List.of(TEXT_S, TEXT_L, TEXT_P1, TEXT_P2));
        for (int i = 0; i < TEXT_F.length; i++) {
            ids.add(Chash.ofText("owner-cap-f" + i).toHex());
            texts.add(TEXT_F[i]);
        }
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < ids.size(); i++) {
            metas.add(Map.of());
        }
        vec.upsertChunks(TENANT, COLLECTION, ids, texts, metas);
        vec.upsertChunks(TENANT, COLLECTION_B, List.of(T, U), List.of(TEXT_T, TEXT_U),
                         List.of(Map.of("line_start", ROW_LINE_START), Map.of("line_start", ROW_LINE_START)));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext seed = DSL.using(su, SQLDialect.POSTGRES);
            for (int d = 1; d <= OWNERS; d++) {
                manifest(seed, doc(d), 0, S);
                manifest(seed, doc(d), 1, L);
            }
            manifest(seed, doc(1), 2, P1);
            manifest(seed, doc(1), 3, P2);
            for (int i = 0; i < TEXT_F.length; i++) {
                manifest(seed, doc(1), 4 + i, Chash.ofText("owner-cap-f" + i).toHex());
            }
            for (int d = 1; d <= FAILING_OWNERS + PASSING_OWNERS; d++) {
                manifestIn(seed, COLLECTION_B, docB(d), 0, T, d <= FAILING_OWNERS ? "references" : "body");
            }
            manifestIn(seed, COLLECTION_B, docB(FAILING_OWNERS + 1), 1, U, "body");  // U has one holder
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void register(PgVectorRepositoryContractTest.FakeEmbedder embedder, String text, double theta) {
        embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
    }

    private static void manifest(DSLContext seed, String doc, int position, String chash) {
        manifestIn(seed, COLLECTION, doc, position, chash, "body");
    }

    private static void manifestIn(DSLContext seed, String collection, String doc, int position, String chash,
                                   String sectionType) {
        seed.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, "Intro")
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, sectionType)
            .set(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, 1)
            .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true)
            .execute();
    }

    private static Map<String, Object> notReferences() {
        Map<String, Object> w = new LinkedHashMap<>();
        Map<String, Object> op = new LinkedHashMap<>();
        op.put("$ne", "references");
        w.put("section_type", op);
        return w;
    }

    private static List<String> docsOf(List<Map<String, Object>> rows, String chash) {
        return rows.stream().filter(r -> chash.equals(r.get("id"))).map(r -> (String) r.get("doc_id")).toList();
    }

    private static List<String> firstOwners(int n) {
        List<String> out = new ArrayList<>();
        for (int d = 1; d <= n; d++) out.add(doc(d));
        return out;
    }

    @Test
    void premise_moreDocumentsHoldTheChunkThanTheCapKeeps() {
        assertThat(OWNERS).isGreaterThan(CAP);
        assertThat(CAP).as("the cap keeps the two-owner shared-chunk semantics").isGreaterThanOrEqualTo(2);
    }

    @Test
    void aChunkSharedByMoreDocumentsThanTheCap_returnsExactlyTheCapsOwners_andTheOtherHitsAreServed() {
        // A page of CAP + 2: without the cap the shared chunk would fill all of it.
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION), CAP + 2, notReferences());

        assertThat(docsOf(rows, S)).as("the first CAP owners in doc_id order, nothing past them")
            .containsExactlyElementsOf(firstOwners(CAP));
        assertThat(rows.stream().map(r -> (String) r.get("id")).toList())
            .as("the two distinct hits behind it are served, the page is full")
            .endsWith(P1, P2).hasSize(CAP + 2);
    }

    @Test
    void theCapCountsOwnersThatPass_notOwnersWalked() {
        // T is held by 10 documents; the first FAILING_OWNERS fail `$ne references`, the other 7 pass. The cap keeps
        // the first CAP PASSING owners, 4..8. A cap that counted occurrences walked would stop at 1..5 and return
        // only 4 and 5.
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION_B), 20, notReferences());

        assertThat(docsOf(rows, T)).containsExactly(
            docB(FAILING_OWNERS + 1), docB(FAILING_OWNERS + 2), docB(FAILING_OWNERS + 3),
            docB(FAILING_OWNERS + 4), docB(FAILING_OWNERS + 5));
        assertThat(PASSING_OWNERS).as("premise: more passing owners than the cap").isGreaterThan(CAP);
        assertThat(FAILING_OWNERS).as("premise: leading owners fail").isPositive();
    }

    @Test
    void aDocumentKeyFilterNamingOneLateOwnerReturnsThatOwner() {
        // The owner is the 7th of 10 in doc_id order, past the cap by position, and passes alone.
        int target = FAILING_OWNERS + 4;
        Map<String, Object> where = new LinkedHashMap<>();
        where.put("title", titleB(target));
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION_B), 20, where);

        assertThat(docsOf(rows, T)).containsExactly(docB(target));
        assertThat(rows).as("no other owner carries that title").allSatisfy(
            r -> assertThat(r.get("doc_id")).isEqualTo(docB(target)));
    }

    @Test
    void aCappedHitStillReadsAsShared_whereAnUnsharedChunkKeepsItsRowSpan() {
        var rows = vec.search(TENANT, QUERY, List.of(COLLECTION_B), 20, notReferences());

        var shared = rows.stream().filter(r -> T.equals(r.get("id"))).toList();
        assertThat(shared).as("the capped hit still returns its CAP owners").hasSize(CAP);
        assertThat(shared).as("T has 10 holders and no manifest span: the row's span is another writer's, so dropped")
            .allSatisfy(r -> assertThat(r).doesNotContainKey("line_start"));

        var unshared = rows.stream().filter(r -> U.equals(r.get("id"))).toList();
        assertThat(unshared).as("control: U has one holder, so its chunk-row span is its own and stays").hasSize(1);
        assertThat(((Number) unshared.get(0).get("line_start")).intValue()).isEqualTo(ROW_LINE_START);
    }

    @Test
    void searchPerCollection_vectorRows_areCappedTheSameWay() {
        var r = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 10, CAP + 2, null, notReferences(), false);

        assertThat(docsOf(r.rows(), S)).containsExactlyElementsOf(firstOwners(CAP));
        assertThat(r.rows()).hasSize(CAP + 2);
    }

    @Test
    void searchPerCollection_lexicalRows_haveEachHitExpandedToTheCapAtMost_andEveryStatIdIsServed() {
        // perCollectionK = 1 widens to 4: the vector arm holds S, P1, P2 and the nearest filler, so L, which is
        // far from the query and the only text with its lexeme, reaches the response through the lexical arm.
        var r = vec.searchPerCollection(TENANT, QUERY, List.of(COLLECTION), 1, 10, null, notReferences(),
                                        false, false, true);

        assertThat(r.rows().stream().map(x -> x.get("id"))).as("premise: the vector arm does not return L")
            .doesNotContain(L);
        assertThat(docsOf(r.lexicalRows(), L)).as("L is held by %s documents; the cap keeps its first owners", OWNERS)
            .containsExactlyElementsOf(firstOwners(CAP));
        assertThat(r.lexicalRows()).as("nothing else came back through the lexical arm").hasSize(CAP);

        List<Object> served = new ArrayList<>();
        r.rows().forEach(x -> served.add(x.get("id")));
        r.lexicalRows().forEach(x -> served.add(x.get("id")));
        assertThat(r.lexical()).isNotEmpty();
        for (LexicalStat st : r.lexical()) {
            assertThat(st.ids()).as("every id a lexical arm reports is still in the response").isNotEmpty()
                .allSatisfy(id -> assertThat(served).contains(id));
        }
    }
}
