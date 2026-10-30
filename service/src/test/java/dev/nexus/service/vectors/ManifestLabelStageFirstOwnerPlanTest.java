// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
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
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.26 fix round 1: the first-owner lookup is its own statement,
 * {@code SELECT DISTINCT ON (chash, collection) ... ORDER BY chash, collection, doc_id, position}, so a
 * boilerplate chunk held by many documents costs a search window one row per collection, where the
 * all-occurrences statement returns one per holder. One statement answers every collection of a window.
 *
 * <p>The statement under test is {@link ManifestLabelStage#occurrenceQuery}, the one production runs, planned
 * and EXECUTED (EXPLAIN ANALYZE) through the RLS-subject service role with the tenant GUC stamped. It pins:
 * the plan has a {@code Unique} node and the top node returns exactly one row per chash (the all-occurrences
 * statement over the same keys returns every holder), and {@code firstOwners} answers, per key, the head of
 * the full lookup's list.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestLabelStageFirstOwnerPlanTest {

    private static final String SVC_ROLE = "svc_first_owner_plan";
    private static final String SVC_PASS = "svc_first_owner_plan_pass";
    private static final String TENANT = "first-owner-plan";
    private static final String COL = "knowledge__first-owner-plan__minilm-l6-v2-384__v1";
    private static final String COL2 = "docs__first-owner-plan__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    /** Three boilerplate chunks held by HOLDERS documents each, and one held by a single document. */
    private static final int HOLDERS = 40;
    private static final int BOILERPLATE = 3;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private final List<String> chashes = new ArrayList<>();

    /** COL2-only chunks; with the four COL keys and chunk 0 a window is 305 keys, past one 300-key batch. */
    private static final int BIG = 300;
    private final List<String> big = new ArrayList<>();

    @BeforeAll
    void seed() throws Exception {
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
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, COL);
            Vector zero = Vector.of(new float[384]);
            for (int c = 0; c <= BOILERPLATE; c++) {
                String hex = Chash.ofText("first-owner-plan-" + c).toHex();
                chashes.add(hex);
                ctx.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, TENANT)
                    .set(CHUNKS.COLLECTION, COL)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex(hex))
                    .set(CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CHUNKS.CHUNK_TEXT, "text " + c)
                    .set(CHUNKS.EMBEDDING_384, zero)
                    .execute();
            }
            for (int d = 0; d < HOLDERS; d++) {
                String doc = String.format("doc-%03d", d);
                ctx.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                    .set(CATALOG_DOCUMENTS.TUMBLER, doc)
                    .set(CATALOG_DOCUMENTS.TITLE, doc)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COL)
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + doc + ".md")
                    .execute();
            }
            // Written in descending doc_id order so the first owner is not the first row on disk.
            for (int d = HOLDERS - 1; d >= 0; d--) {
                for (int c = 0; c < BOILERPLATE; c++) {
                    manifest(ctx, COL, String.format("doc-%03d", d), c, chashes.get(c));
                }
            }
            // The last chunk is held by one document only.
            manifest(ctx, COL, "doc-007", 9, chashes.get(BOILERPLATE));
            // A second collection holds boilerplate chunk 0 and the lone chunk, in one document whose doc_id
            // sorts after every COL holder, so a COL2 answer can only come from COL2's own row.
            PgContainerHelper.insertCollection(ctx, TENANT, COL2);
            for (int c : new int[] {0, BOILERPLATE}) {
                ctx.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, TENANT)
                    .set(CHUNKS.COLLECTION, COL2)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex(chashes.get(c)))
                    .set(CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CHUNKS.CHUNK_TEXT, "text " + c)
                    .set(CHUNKS.EMBEDDING_384, zero)
                    .execute();
            }
            ctx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, "doc-900")
                .set(CATALOG_DOCUMENTS.TITLE, "doc-900")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COL2)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///doc-900.md")
                .execute();
            manifest(ctx, COL2, "doc-900", 4, chashes.get(0));
            manifest(ctx, COL2, "doc-900", 5, chashes.get(BOILERPLATE));
            // Enough further COL2 chunks that a window over both collections crosses one lookup batch.
            for (int c = 0; c < BIG; c++) {
                String hex = Chash.ofText("first-owner-plan-big-" + c).toHex();
                big.add(hex);
                ctx.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, TENANT)
                    .set(CHUNKS.COLLECTION, COL2)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex(hex))
                    .set(CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CHUNKS.CHUNK_TEXT, "big " + c)
                    .set(CHUNKS.EMBEDDING_384, zero)
                    .execute();
                manifest(ctx, COL2, "doc-900", 100 + c, hex);
            }
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
        }
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void manifest(DSLContext ctx, String collection, String doc, int position, String hex) {
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(hex))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .execute();
    }

    private Map<String, Set<String>> window() {
        Map<String, Set<String>> w = new LinkedHashMap<>();
        w.put(COL, new LinkedHashSet<>(chashes));
        return w;
    }

    /** The collection half of {@code n} (collection, chash) pairs, every pair in {@code collection}. */
    private static String[] cols(String collection, int n) {
        String[] out = new String[n];
        java.util.Arrays.fill(out, collection);
        return out;
    }

    private byte[][] keys() {
        return chashes.stream().map(h -> Chash.fromHex(h).toBytes()).toArray(byte[][]::new);
    }

    private static final Pattern ACTUAL_ROWS = Pattern.compile("actual time=[0-9.]+\\.\\.[0-9.]+ rows=(\\d+)");

    /** EXPLAIN ANALYZE of the production statement under RLS; the plan text, top node first. */
    private List<String> plan(boolean firstOnly) {
        Field<String> chashHex = ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH);
        return tenantScope.withTenant(TENANT, ctx -> {
            var select = new ArrayList<Field<?>>(List.of(chashHex, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENTS.SOURCE_URI));
            return List.of(PgContainerHelper.explainPlanWith(ctx,
                ManifestLabelStage.occurrenceQuery(ctx, TENANT, cols(COL, keys().length), keys(), select, firstOnly),
                new PgContainerHelper.ExplainOptions(true, false, false, false)).split("\n"));
        });
    }

    private static int topRows(List<String> plan) {
        Matcher m = ACTUAL_ROWS.matcher(plan.get(0));
        assertThat(m.find()).as("top node reports its rows: " + plan.get(0)).isTrue();
        return Integer.parseInt(m.group(1));
    }

    @Test
    void theFirstOwnerStatement_isDistinctOn_andReturnsOneRowPerChash() {
        List<String> plan = plan(true);

        assertThat(String.join("\n", plan)).as("plan").contains("Unique");
        assertThat(topRows(plan)).as("one row per chash").isEqualTo(chashes.size());
    }

    @Test
    void theAllOccurrencesStatement_returnsEveryHolder_soTheFirstOwnerStatementIsTheCheaperOne() {
        // The control: the same keys, the same joins, without DISTINCT ON.
        List<String> plan = plan(false);

        assertThat(String.join("\n", plan)).doesNotContain("Unique");
        assertThat(topRows(plan)).isEqualTo(BOILERPLATE * HOLDERS + 1);
    }

    @Test
    void firstOwners_isTheHeadOfTheFullLookup_forEveryKey() {
        var stage = new ManifestLabelStage(tenantScope);
        var first = stage.firstOwners(TENANT, window());
        var all = stage.allOccurrences(TENANT, window());

        assertThat(first).hasSize(chashes.size());
        assertThat(all.keySet()).isEqualTo(first.keySet());
        all.forEach((key, list) -> assertThat(first.get(key)).as(key).isEqualTo(list.get(0)));
        // And that head is the lowest doc_id, then position: doc-000 holds each boilerplate chunk at its own
        // position; the lone chunk is doc-007's.
        for (int c = 0; c < BOILERPLATE; c++) {
            var o = first.get(ManifestLabelStage.ownerKey(COL, chashes.get(c)));
            assertThat(o.docId()).isEqualTo("doc-000");
            assertThat(o.position()).isEqualTo(c);
        }
        assertThat(first.get(ManifestLabelStage.ownerKey(COL, chashes.get(BOILERPLATE))).docId())
            .isEqualTo("doc-007");
    }

    @Test
    void aTwoCollectionWindow_isOneStatement_andAnswersEachCollectionFromItsOwnRows() {
        var stage = new ManifestLabelStage(tenantScope);
        Map<String, Set<String>> w = window();
        // COL2 is asked for chunk 0 only; it also holds the lone chunk, which the statement's ANY(...) pair
        // can match and the stage must drop.
        w.put(COL2, new LinkedHashSet<>(List.of(chashes.get(0))));

        int before = stage.lookupStatementCount();
        var first = stage.firstOwners(TENANT, w);
        assertThat(stage.lookupStatementCount() - before).as("one statement for the whole window").isEqualTo(1);

        assertThat(first).hasSize(chashes.size() + 1);
        var col2 = first.get(ManifestLabelStage.ownerKey(COL2, chashes.get(0)));
        assertThat(col2.docId()).isEqualTo("doc-900");
        assertThat(col2.position()).isEqualTo(4);
        assertThat(first.get(ManifestLabelStage.ownerKey(COL, chashes.get(0))).docId()).isEqualTo("doc-000");
        assertThat(first).as("a pair the window did not ask for")
            .doesNotContainKey(ManifestLabelStage.ownerKey(COL2, chashes.get(BOILERPLATE)));

        var all = stage.allOccurrences(TENANT, w);
        assertThat(all.get(ManifestLabelStage.ownerKey(COL2, chashes.get(0))))
            .extracting(ManifestLabelStage.Occurrence::docId).containsExactly("doc-900");
        assertThat(all).doesNotContainKey(ManifestLabelStage.ownerKey(COL2, chashes.get(BOILERPLATE)));
    }

    @Test
    void aWindowPastOneBatch_isTwoStatements_andEveryKeyIsAnsweredFromItsOwnCollection() {
        var stage = new ManifestLabelStage(tenantScope);
        Map<String, Set<String>> w = window();
        Set<String> col2 = new LinkedHashSet<>(List.of(chashes.get(0)));
        col2.addAll(big);
        w.put(COL2, col2);

        int before = stage.lookupStatementCount();
        var first = stage.firstOwners(TENANT, w);
        var all = stage.allOccurrences(TENANT, w);
        assertThat(stage.lookupStatementCount() - before).as("305 keys: two statements per lookup").isEqualTo(4);

        assertThat(first).hasSize(chashes.size() + 1 + BIG);
        for (int c = 0; c < BIG; c++) {
            var o = first.get(ManifestLabelStage.ownerKey(COL2, big.get(c)));
            assertThat(o.docId()).as(big.get(c)).isEqualTo("doc-900");
            assertThat(o.position()).isEqualTo(100 + c);
        }
        assertThat(first.get(ManifestLabelStage.ownerKey(COL2, chashes.get(0))).docId()).isEqualTo("doc-900");
        assertThat(first.get(ManifestLabelStage.ownerKey(COL, chashes.get(0))).docId()).isEqualTo("doc-000");
        assertThat(first).doesNotContainKey(ManifestLabelStage.ownerKey(COL2, chashes.get(BOILERPLATE)));
        assertThat(all.get(ManifestLabelStage.ownerKey(COL, chashes.get(0)))).hasSize(HOLDERS);
        assertThat(all).doesNotContainKey(ManifestLabelStage.ownerKey(COL2, chashes.get(BOILERPLATE)));
        all.forEach((key, list) -> assertThat(first.get(key)).as(key).isEqualTo(list.get(0)));
    }

    @Test
    void theStatementText_isTheSame_whateverTheBatchSize() {
        Field<String> chashHex = ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH);
        String one = tenantScope.withTenant(TENANT, ctx -> ManifestLabelStage.occurrenceQuery(ctx, TENANT,
            cols(COL, 1), new byte[][] {keys()[0]}, new ArrayList<>(List.of(chashHex)), true).getSQL());
        String many = tenantScope.withTenant(TENANT, ctx -> ManifestLabelStage.occurrenceQuery(ctx, TENANT,
            cols(COL2, keys().length), keys(), new ArrayList<>(List.of(chashHex)), true).getSQL());
        assertThat(many).isEqualTo(one);
    }
}
