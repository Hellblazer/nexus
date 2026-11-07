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
 * nexus-6cmam (statement budget): the lookup behind {@link ManifestLabelStage#apply} reads at most {@link
 * ManifestLabelStage#OCCURRENCES_PER_CHASH} live occurrences of one chash, the first in {@code (doc_id,
 * position)} order, however many documents hold it. The owner cap (five passing owners) is applied in Java after
 * the filter, so the lookup has to hand it more than five candidates; this bound is how many more.
 *
 * <p>Fixture: one boilerplate chunk {@code X} held by {@link #HOLDERS} documents ({@code HOLDERS} well past the
 * bound), a chunk {@code Y} held by three, and a tombstoned document that sorts first and holds {@code Y}. In
 * {@code X}'s holders the first {@link #FAILING} (by doc_id) carry {@code section_type = references} and the rest
 * {@code body}, so {@code $ne references} leaves the owners from {@code FAILING} on. {@code Z} is held by the
 * same documents, but its first {@code BOUND} holders all fail, so only holders past the bound would pass.
 *
 * <p>What the bound drops (declared, not accidental): an owner past the first {@code OCCURRENCES_PER_CHASH}
 * candidates is never seen, so a filter that fails every one of those candidates returns that hit with no rows
 * even though a later holder would pass. {@link #aPassingOwnerPastTheBound_isNotSeen} pins it as the stated
 * behaviour.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestLabelStageOccurrenceBoundTest {

    private static final String SVC_ROLE = "svc_occ_bound";
    private static final String SVC_PASS = "svc_occ_bound_pass";
    private static final String TENANT = "occ-bound";
    private static final String COL = "knowledge__occ-bound__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static final int BOUND = ManifestLabelStage.OCCURRENCES_PER_CHASH;
    private static final int HOLDERS = BOUND * 3;
    /** Leading holders (doc_id order) that fail {@code $ne references}; inside the bound, so owners remain. */
    private static final int FAILING = BOUND - 4;

    private static final String X = Chash.ofText("occ-bound-x").toHex();
    private static final String Y = Chash.ofText("occ-bound-y").toHex();
    /** Held by every document like X, but its first BOUND holders all fail {@code $ne references}. */
    private static final String Z = Chash.ofText("occ-bound-z").toHex();

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;

    private static String doc(int n) {
        return String.format("bnd-doc-%03d", n);
    }

    private static List<String> docs(int fromInclusive, int toExclusive) {
        return java.util.stream.IntStream.range(fromInclusive, toExclusive).mapToObj(
            ManifestLabelStageOccurrenceBoundTest::doc).toList();
    }

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
            for (String hex : List.of(X, Y, Z)) {
                ctx.insertInto(CHUNKS)
                    .set(CHUNKS.TENANT_ID, TENANT)
                    .set(CHUNKS.COLLECTION, COL)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex(hex))
                    .set(CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CHUNKS.CHUNK_TEXT, "text " + hex)
                    .set(CHUNKS.EMBEDDING_384, zero)
                    .execute();
            }
            for (int d = 0; d < HOLDERS; d++) {
                document(ctx, doc(d), false);
                manifest(ctx, doc(d), 0, X, d < FAILING ? "references" : "body");
                manifest(ctx, doc(d), 2, Z, d < BOUND ? "references" : "body");
            }
            // Y: three live holders after a tombstoned one that would sort first.
            document(ctx, "aaa-tombstoned", true);
            manifest(ctx, "aaa-tombstoned", 0, Y, "body");
            for (int d = 0; d < 3; d++) {
                manifest(ctx, doc(d), 1, Y, "body");
            }
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
        }
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void document(DSLContext ctx, String doc, boolean tombstoned) {
        var insert = ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENTS.TUMBLER, doc)
            .set(CATALOG_DOCUMENTS.TITLE, doc)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COL)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + doc + ".md");
        if (tombstoned) {
            insert.set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now());
        }
        insert.execute();
    }

    private static void manifest(DSLContext ctx, String doc, int position, String hex, String sectionType) {
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(hex))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COL)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, sectionType)
            .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, true)
            .execute();
    }

    private static Map<String, Object> hit(String chash) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("id", chash);
        row.put("collection", COL);
        return row;
    }

    private static List<ManifestLabelStage.Predicate> notReferences() {
        return ManifestLabelStage.split(Map.of("section_type", Map.of("$ne", "references"))).labels();
    }

    private Map<String, Set<String>> window(String... hexes) {
        Map<String, Set<String>> w = new LinkedHashMap<>();
        w.put(COL, new LinkedHashSet<>(List.of(hexes)));
        return w;
    }

    private static final Pattern ACTUAL_ROWS = Pattern.compile("actual time=[0-9.]+\\.\\.[0-9.]+ rows=(\\d+)");

    @Test
    void premise_theBoundIsPastTheOwnerCap_andTheFixtureIsPastTheBound() {
        assertThat(BOUND).as("the filter runs in Java after the lookup, so it needs more than the cap")
            .isGreaterThan(ManifestLabelStage.MAX_OWNERS_PER_HIT);
        assertThat(HOLDERS).isGreaterThan(BOUND);
        assertThat(FAILING).as("some leading holders fail, but fewer than the bound").isBetween(1, BOUND - 1);
    }

    @Test
    void aChunkHeldByManyDocuments_isReadAtMostTheBound_inDocIdOrder() {
        var stage = new ManifestLabelStage(tenantScope);

        var occ = stage.occurrences(TENANT, List.of(hit(X), hit(Y)), Set.of());

        var x = occ.get(ManifestLabelStage.ownerKey(COL, X));
        assertThat(x).as("%d documents hold X; the lookup reads %d".formatted(HOLDERS, BOUND)).hasSize(BOUND);
        assertThat(x.stream().map(ManifestLabelStage.Occurrence::docId).toList())
            .as("the first BOUND holders in doc_id order")
            .containsExactlyElementsOf(docs(0, BOUND));
    }

    @Test
    void aChunkBelowTheBound_isReadWhole_andATombstonedHolderOwnsNothing() {
        var stage = new ManifestLabelStage(tenantScope);

        var y = stage.occurrences(TENANT, List.of(hit(Y)), Set.of()).get(ManifestLabelStage.ownerKey(COL, Y));

        assertThat(y.stream().map(ManifestLabelStage.Occurrence::docId).toList())
            .containsExactly(doc(0), doc(1), doc(2));
    }

    @Test
    void theStatement_returnsAtMostTheBoundPerChash_asSeenByThePlan() {
        Field<String> chashHex = ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH);
        List<byte[]> keys = List.of(Chash.fromHex(X).toBytes(), Chash.fromHex(Y).toBytes());

        String plan = tenantScope.withTenant(TENANT, ctx -> {
            var select = new ArrayList<Field<?>>(List.of(chashHex, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENTS.SOURCE_URI));
            return PgContainerHelper.explainPlanWith(ctx,
                ManifestLabelStage.occurrenceQuery(ctx, TENANT, COL, keys, select, false, BOUND),
                new PgContainerHelper.ExplainOptions(true, false, false, false));
        });

        Matcher m = ACTUAL_ROWS.matcher(plan.split("\n")[0]);
        assertThat(m.find()).as("top node reports its rows: " + plan).isTrue();
        assertThat(Integer.parseInt(m.group(1))).as("X is cut at the bound, Y returns its three")
            .isEqualTo(BOUND + 3);
    }

    @Test
    void aLatePassingOwnerWithinTheBound_stillReturns() {
        var stage = new ManifestLabelStage(tenantScope);

        var out = stage.apply(TENANT, List.of(hit(X)), notReferences(), 50);

        // Holders FAILING..BOUND-1 pass and sit inside the bound; the owner cap (5) would also take BOUND, which
        // the lookup never reads (the declared drop, pinned below).
        assertThat(out.stream().map(r -> (String) r.get("doc_id")).toList())
            .as("the last candidate inside the bound is returned, the leading failures skipped")
            .containsExactlyElementsOf(docs(FAILING, BOUND));
        assertThat(out.get(out.size() - 1).get("doc_id")).isEqualTo(doc(BOUND - 1));
    }

    @Test
    void anOwnerPastTheBound_isNotSeen_theDeclaredDrop() {
        // X: five passing holders exist (FAILING..BOUND), the bound sees four. Z: every candidate inside the bound
        // fails, so the hit returns nothing although later holders pass. Pinned so a change to the bound is a
        // decision (class javadoc of ManifestLabelStage says what it drops).
        var stage = new ManifestLabelStage(tenantScope);

        var x = stage.apply(TENANT, List.of(hit(X)), notReferences(), 50);
        var z = stage.apply(TENANT, List.of(hit(Z)), notReferences(), 50);

        assertThat(x).as("the cap alone would give %d".formatted(ManifestLabelStage.MAX_OWNERS_PER_HIT))
            .hasSize(BOUND - FAILING);
        assertThat(BOUND - FAILING).isLessThan(ManifestLabelStage.MAX_OWNERS_PER_HIT);
        assertThat(z).isEmpty();
    }
}
