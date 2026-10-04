// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Query;
import org.jooq.ResultQuery;
import org.jooq.SQLDialect;
import org.jooq.impl.SQLDataType;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_VECTOR_STATS;
import static dev.nexus.service.jooq.nexus.Tables.HYBRID_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_HNSW_FIRST_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_768;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Phase 2 gate finding (bead nexus-wbfpw.37, T2 nexus/critique-rdr-192-phase2 row 11):
 * plan evidence for the live(c) read paths vectors-019 and vectors-023 moved, beyond the two the
 * earlier work measured (plain search and {@code text_gated_search_hnsw_first}, in
 * {@code PlainSearchTextGatedSearchExplainTest} and {@code ChunkLiveOwnersMsz9iScaleIntegrationTest}).
 * Covered here: topic-scoped search, the SQL hybrid function, the hybrid dispatch's gate probe,
 * the by-chash rank, and the {@code collection_vector_stats} inventory view.
 *
 * <p><b>Role.</b> Every plan is taken as the production service role {@code nexus_svc}
 * (NOSUPERUSER NOBYPASSRLS, asserted below), inside {@link TenantScope#withTenant}, so the
 * row-level-security filter is part of the plan rather than bypassed by a superuser. The plans
 * are taken at the planner's own choice (no {@code enable_*} switch), because the question is
 * what the planner does with the inlined predicate, and each function is measured as a plain
 * {@code SELECT .. FROM fn(..)} so the plan is the one the repository's own call produces.
 *
 * <p><b>Ownership (nexus-wbfpw.48).</b> The container is DEDICATED and migrated as a non-superuser owner
 * ({@code PgContainerHelper#bootstrapNonSuperuserOwner}, then {@code SchemaMigrator.migrate}), which is
 * production's shape: {@code nexus_admin} owns every relation, is NOSUPERUSER and not BYPASSRLS, and FORCE
 * RLS applies to it. That is required since vectors-029 made the gate probe SECURITY DEFINER: a definer
 * function runs as its owner, so a superuser-migrated container (the shared template) would run the body
 * past RLS and make the probe's plan say nothing about production. The probe is no longer inlined, so
 * EXPLAIN of a call is one Function Scan; its body's plan, as {@code nexus_svc}, is read from auto_explain's
 * nested-statement log ({@code db.changelog-test-auto-explain.xml}). The class also carries the probe's
 * tenant-isolation tests (a second tenant is seeded for them) and the pin of vectors-029's owner policy.
 *
 * <p><b>Fixture.</b> {@value #NUM_CHUNKS} 384-dim chunks (override {@code -Dnx.rdr192Explain.chunks}),
 * 90% manifested across {@value #NUM_DOCS} documents of which every third is tombstoned, 10%
 * manifest-less; so 63% of the chunks are live(c) and 37% are hidden by one of the two reasons live(c)
 * hides a chunk. Chunk text carries a rare token (selective gate, 1%) and a common one (dense gate,
 * 70%). One topic is assigned to the first {@value #TOPIC_CHUNKS} chunks.
 *
 * <p><b>Which tests guard vectors-023, and which do not.</b> None of the inlining pins below guards
 * {@code vectors-023} (the move of {@code text_gate_probe_<dim>} onto live(c), nexus-wbfpw.35): all of them
 * also pass on the OLD probe, with vectors-023 removed from the master include (checked: 13 of 13 green),
 * because the old probe's tenant-wide dead-set anti-join inlines too. They pin only that
 * {@code chunk_live_owners} stays inlinable. What fails without vectors-023 is
 * {@code Rdr192EngineLivenessMatrix#p1p_textGateProbeVisibility} (the probe must not count a hidden
 * chunk), and, for the fixture shape, {@code HnswScanBudgetOnEverySearchPathIntegrationTest}'s gate chunks,
 * which need a live owner for the probe to see them. (That paragraph is about vectors-023 and the probe's
 * old, inlined form. Since vectors-029 the probe's body plan is read from auto_explain, and the pins on it
 * are real: the selective gate must reach idx_chunks_tsv or idx_chunks_trgm. Checked by dropping the
 * changeset's owner policy in this fixture (recorded on the bead): the body then plans a Bitmap Heap Scan
 * over chunks_pk with the text predicates as a Filter, "Rows Removed by Filter: 23856", and this class's pin
 * fails.)
 *
 * <p><b>What is pinned and why.</b> The one regression these queries share is the liveness
 * predicate stopping inlining: {@code nexus.chunk_live_owners} is a set-returning SQL function that
 * the planner folds into an indexed per-row probe of {@code catalog_document_chunks} and
 * {@code catalog_documents} (a SubPlan; PG 17 does not pull the EXISTS up into a semi-join, see T2
 * nexus/rdr-192-reapable-plans-and-decisions-2026-10-01) only while it stays a plain inlinable
 * {@code LANGUAGE sql} function (vectors-018's header; SECURITY DEFINER, a {@code SET} clause
 * or a volatile marking each break it). If it stopped inlining, every chunk read would call it as an
 * opaque function per row, a cost no result-set test sees. Each plan below asserts the function name is
 * absent and the probe's target table is present. The stats view carries one further pin: a single
 * shared probe per chunk. The remaining plan facts (index choices, row estimates, costs) are
 * written to {@code target/rdr192-explain-evidence.txt} for the record, not pinned: they move with
 * fixture size and PG minor version, and a pin on them would fail for reasons that are not regressions.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr192LiveCExplainEvidenceIntegrationTest {

    private static final String TENANT = "rdr192-explain";
    private static final String OTHER_TENANT = "rdr192-other";
    private static final String ADMIN_ROLE = "nexus_admin_rdr192";
    private static final String ADMIN_PASS = "nexus_admin_rdr192_pass";
    private static final String OTHER_COLL = "knowledge__rdr192-other__minilm-l6-v2-384__v1";
    static final int OTHER_CHUNKS = 50;
    /** Tenant A's second collection: live chunks carrying numeric metadata, for the where_path tests. */
    private static final String META_COLL = "knowledge__rdr192-meta__minilm-l6-v2-384__v1";
    /** Chunks of META_COLL whose "v" is > 1 (the jsonpath below matches them) and chunks whose "v" is 0. */
    static final int META_MATCH = 5;
    static final int META_NOMATCH = 2;
    /** A tenant whose id is the EMPTY string: only a superuser can write one (TenantScope refuses a blank id). */
    private static final String EMPTY_TENANT = "";
    private static final String EMPTY_COLL = "knowledge__rdr192-empty__minilm-l6-v2-384__v1";
    /**
     * Tenant B's hostile metadata: values under which {@code $.v.double() > 1} ERRORS (a non-numeric string,
     * an out-of-range number string, an object, a null) next to ones it matches (a large number, an array).
     * They sit in tenant B's collection, live, carrying the rare token, so a probe that ever evaluated a
     * where_path on B's rows from tenant A's session would be handed every one of them.
     */
    private static final List<Map<String, Object>> HOSTILE_METADATA = List.of(
        Map.of("v", "not-a-number"),
        Map.of("v", "1e999"),
        Map.of("v", Map.of("x", 1)),
        Collections.singletonMap("v", null),
        Map.of("v", 9999),
        Map.of("v", List.of(1, 2, 3)));
    private static final String V_GT_1 = "$.v.double() > 1";
    private static final String COLL = "knowledge__rdr192-explain__minilm-l6-v2-384__v1";
    private static final int DIM = 384;
    static final int NUM_CHUNKS = Integer.getInteger("nx.rdr192Explain.chunks", 24_000);
    static final int NUM_DOCS = 600;
    static final int TOPIC_CHUNKS = 2_000;
    static final int TIMED_RUNS = 5;
    private static final String TOPIC_LABEL = "rdr192-explain-topic";
    // Tokens share no trigram with the filler text ("rdr192 explain fixture chunk N alpha bravo"): a
    // token that did (the first cut used "rdr192rare") puts its trigrams in every row, the GIN trigram
    // index then cannot narrow the candidate set, and the probe's selective-gate plan is a sequential
    // scan that says nothing about the indexed case.
    private static final String RARE_TOKEN = "qzvkwx";
    private static final String COMMON_TOKEN = "jmpthy";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    /** nexus_svc sessions opened after auto_explain was switched on for the role; see {@link #probeBodyPlan}. */
    HikariDataSource explainDs;
    TenantScope explainScope;
    final List<String> chashHex = new ArrayList<>();
    final List<String> otherChashHex = new ArrayList<>();
    final List<String> metaMatchChashHex = new ArrayList<>();
    final List<float[]> vectors = new ArrayList<>();
    final Map<String, String> evidence = new LinkedHashMap<>();
    /** The median wall-clock of each {@link #explain}ed statement, by label, for the few tests that bound one. */
    final Map<String, Long> p50Ms = new LinkedHashMap<>();

    @BeforeAll
    void startAll() throws Exception {
        // A DEDICATED container migrated as a NON-SUPERUSER owner, which is production's shape
        // (nexus_admin owns every relation, NOSUPERUSER NOBYPASSRLS), not the shared superuser-migrated
        // template. It matters since vectors-029: the gate probe is SECURITY DEFINER, a definer function
        // runs as its owner, and a superuser owner would read past RLS and make every plan below say nothing
        // about production. See db.changelog-test-nonsuper-owner.xml.
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        try (var adminDs = new HikariDataSource(adminCfg)) {
            SchemaMigrator.migrate(adminDs);
            // Through the migrating role's own connection (PgContainerHelper#installTestObjects' ownership
            // contract): the nexus_test helpers the fixture uses (analyze_table).
            try (Connection c = adminDs.getConnection()) {
                PgContainerHelper.installTestObjects(c);
            }
        }
        svcDs = svcPool("rdr192-svc");
        tenantScope = new TenantScope(svcDs);
        seed();
        // After the seed: from here nexus_svc's NEW sessions log every plan, nested ones included.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.enableAutoExplainForService(su);
        }
        explainDs = svcPool("rdr192-explain");
        explainScope = new TenantScope(explainDs);
    }

    private HikariDataSource svcPool(String name) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setPoolName(name);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() throws IOException {
        writeEvidence();
        if (explainDs != null) explainDs.close();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLL);
        }
        Random rnd = new Random(20261001037L);
        var repo = new PgVectorRepository(tenantScope, (Embedder) null, (Embedder) null);
        int batch = 1000;
        for (int start = 0; start < NUM_CHUNKS; start += batch) {
            int end = Math.min(start + batch, NUM_CHUNKS);
            List<String> ids = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            List<float[]> vecs = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int i = start; i < end; i++) {
                String id = Chash.ofText("rdr192-explain-chunk-" + i).toHex();
                chashHex.add(id);
                float[] v = unitVector(rnd);
                vectors.add(v);
                ids.add(id);
                vecs.add(v);
                String text = "rdr192 explain fixture chunk " + i + " alpha bravo";
                if (i % 100 == 0) text = RARE_TOKEN + " " + text;          // 1%: selective gate
                if (i % 10 < 7) text = COMMON_TOKEN + " " + text;          // 70%: dense gate
                texts.add(text);
                metas.add(Map.of());
            }
            repo.upsertChunksWithVectors(TENANT, COLL, ids, texts, vecs, metas);
        }

        int manifested = NUM_CHUNKS * 9 / 10;
        int perDoc = Math.max(1, manifested / NUM_DOCS);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<Query> docs = new ArrayList<>();
            for (int d = 0; d < NUM_DOCS; d++) {
                docs.add(ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID,
                        CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE,
                        CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                    .values(TENANT, doc(d), "Doc " + d, "prose", COLL));
            }
            ctx.batch(docs).execute();
            su.commit();
            for (int start = 0; start < manifested; start += 5000) {
                int end = Math.min(start + 5000, manifested);
                List<Query> rows = new ArrayList<>();
                for (int i = start; i < end; i++) {
                    int d = Math.min(NUM_DOCS - 1, i / perDoc);
                    rows.add(ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                            CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                            CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                        .values(TENANT, doc(d), i - d * perDoc, HexFormat.of().parseHex(chashHex.get(i)), COLL));
                }
                ctx.batch(rows).execute();
                su.commit();
            }
            // Tombstone every third document (scattered), the msz9i methodology.
            for (int d = 0; d < NUM_DOCS; d += 3) {
                ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                   .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc(d))))
                   .execute();
            }
            su.commit();

            long topicId = 9_100_000_001L;
            ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                           TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
               .values(topicId, TENANT, TOPIC_LABEL, COLL, TOPIC_CHUNKS, OffsetDateTime.now(), "pending")
               .execute();
            for (int start = 0; start < TOPIC_CHUNKS; start += 1000) {
                List<Query> rows = new ArrayList<>();
                for (int i = start; i < Math.min(start + 1000, TOPIC_CHUNKS); i++) {
                    rows.add(ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID,
                            TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                            TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                        .values(TENANT, HexFormat.of().parseHex(chashHex.get(i)), topicId, "projection", COLL,
                                OffsetDateTime.now()));
                }
                ctx.batch(rows).execute();
                su.commit();
            }
        }
        seedOtherTenant(repo);
        seedHostileAndMetadataRows();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, TOPIC_ASSIGNMENTS);
            PgContainerHelper.analyzeTable(su, TOPICS);
        }
    }

    /**
     * A second tenant, for the isolation tests: {@value #OTHER_CHUNKS} LIVE chunks (manifested under a live
     * document) in a collection of its own, every one carrying the SAME rare token the first tenant's
     * selective gate matches. A probe that leaked across tenants would return them.
     */
    private void seedOtherTenant(PgVectorRepository repo) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), OTHER_TENANT, OTHER_COLL);
        }
        Random rnd = new Random(20261004048L);
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < OTHER_CHUNKS; i++) {
            ids.add(Chash.ofText("rdr192-other-chunk-" + i).toHex());
            texts.add(RARE_TOKEN + " rdr192 other tenant chunk " + i);
            vecs.add(unitVector(rnd));
            metas.add(Map.of());
        }
        repo.upsertChunksWithVectors(OTHER_TENANT, OTHER_COLL, ids, texts, vecs, metas);
        otherChashHex.addAll(ids);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(OTHER_TENANT, "rdr192-other-doc-0000", "Other doc", "prose", OTHER_COLL).execute();
            List<Query> rows = new ArrayList<>();
            for (int i = 0; i < OTHER_CHUNKS; i++) {
                rows.add(ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                        CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                        CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(OTHER_TENANT, "rdr192-other-doc-0000", i, HexFormat.of().parseHex(ids.get(i)), OTHER_COLL));
            }
            ctx.batch(rows).execute();
        }
    }

    /**
     * Rows written by the superuser (RLS bypassed, so any tenant id is writable): tenant B's hostile-metadata
     * chunks (see {@link #HOSTILE_METADATA}), tenant A's metadata collection (live, numeric {@code v}), and one
     * chunk of the tenant whose id is the empty string. Every one carries the rare token and a live owner, so
     * a probe that reached it would return it.
     */
    private void seedHostileAndMetadataRows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Random rnd = new Random(20261004049L);

            List<String> hostile = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            List<float[]> vecs = new ArrayList<>();
            for (int i = 0; i < HOSTILE_METADATA.size(); i++) {
                hostile.add(Chash.ofText("rdr192-other-hostile-" + i).toHex());
                texts.add(RARE_TOKEN + " rdr192 other tenant hostile metadata " + i);
                vecs.add(unitVector(rnd));
            }
            PgContainerHelper.insertChunks(ctx, OTHER_TENANT, OTHER_COLL, hostile, texts, vecs, HOSTILE_METADATA);
            PgContainerHelper.ownChunks(ctx, OTHER_TENANT, OTHER_COLL, hostile.toArray(String[]::new));
            otherChashHex.addAll(hostile);

            PgContainerHelper.insertCollection(ctx, TENANT, META_COLL);
            List<String> ids = new ArrayList<>();
            List<String> mtexts = new ArrayList<>();
            List<float[]> mvecs = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int i = 0; i < META_MATCH + META_NOMATCH; i++) {
                ids.add(Chash.ofText("rdr192-meta-chunk-" + i).toHex());
                mtexts.add(RARE_TOKEN + " rdr192 metadata chunk " + i);
                mvecs.add(unitVector(rnd));
                metas.add(Map.of("v", i < META_MATCH ? i + 2 : 0));
                if (i < META_MATCH) metaMatchChashHex.add(ids.get(i));
            }
            PgContainerHelper.insertChunks(ctx, TENANT, META_COLL, ids, mtexts, mvecs, metas);
            PgContainerHelper.ownChunks(ctx, TENANT, META_COLL, ids.toArray(String[]::new));

            PgContainerHelper.insertCollection(ctx, EMPTY_TENANT, EMPTY_COLL);
            String emptyId = Chash.ofText("rdr192-empty-tenant-chunk").toHex();
            PgContainerHelper.insertChunks(ctx, EMPTY_TENANT, EMPTY_COLL, List.of(emptyId),
                List.of(RARE_TOKEN + " rdr192 empty tenant chunk"), List.of(unitVector(rnd)),
                List.of(Map.of()));
            PgContainerHelper.ownChunks(ctx, EMPTY_TENANT, EMPTY_COLL, emptyId);
        }
    }

    private static String doc(int d) {
        return String.format("rdr192-explain-doc-%04d", d);
    }

    private static float[] unitVector(Random rnd) {
        float[] v = new float[DIM];
        double norm = 0;
        for (int i = 0; i < DIM; i++) {
            v[i] = (float) rnd.nextGaussian();
            norm += v[i] * v[i];
        }
        float inv = (float) (1.0 / Math.sqrt(norm));
        for (int i = 0; i < DIM; i++) v[i] *= inv;
        return v;
    }

    // ── the role the plans are taken as ─────────────────────────────────────

    /**
     * Non-vacuity for every plan below: it was taken as {@code nexus_svc}, which is neither a
     * superuser nor BYPASSRLS. A plan taken as the container superuser would carry no RLS qual
     * and prove nothing about production.
     */
    @Test
    void plansAreTakenAsTheProductionRoleUnderRls() {
        Table<?> roles = DSL.table(DSL.name("pg_catalog", "pg_roles"));
        Field<String> name = DSL.field(DSL.name("rolname"), String.class);
        Field<Boolean> superuser = DSL.field(DSL.name("rolsuper"), Boolean.class);
        Field<Boolean> bypass = DSL.field(DSL.name("rolbypassrls"), Boolean.class);
        tenantScope.withTenant(TENANT, ctx -> {
            var row = ctx.select(name, superuser, bypass).from(roles).where(name.eq(DSL.currentUser())).fetchOne();
            assertThat(row).as("current role must be visible in pg_roles").isNotNull();
            assertThat(row.value1()).isEqualTo(PgContainerHelper.SVC_USERNAME);
            assertThat(row.value2()).as("nexus_svc must not be a superuser").isFalse();
            assertThat(row.value3()).as("nexus_svc must not bypass RLS").isFalse();
            return null;
        });
        String plan = explain("rls_probe", ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(COLL)));
        // The tenant GUC comparison, not just the column name: the query names no tenant of its own, so
        // this qual can only come from the row-level-security policy.
        assertThat(plan).as("the plan must carry the row-level-security qual").contains("current_setting('nexus.tenant'");
    }

    /**
     * Control for the test above: the same query as the container superuser carries NO such qual, so
     * the assertion there tells a plan taken under RLS from one taken without it.
     */
    @Test
    void thePlanWithoutRlsCarriesNoTenantQual() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String plan = ctx.explain(ctx.selectCount().from(CHUNKS).where(CHUNKS.COLLECTION.eq(COLL))).plan();
            assertThat(plan).doesNotContain("current_setting('nexus.tenant'");
        }
    }

    /**
     * The premise of nexus-wbfpw.48 (the gate probe cannot use the GIN text indexes under RLS): the
     * operator functions the text gate evaluates are not leakproof, and PostgreSQL refuses to make a
     * non-leakproof operator an index condition on a relation with a security qual
     * ({@code restriction_is_securely_promotable}). Recorded and pinned: a PostgreSQL that marked them
     * leakproof would change what the fix has to be.
     */
    @Test
    void textGateOperatorFunctions_areNotLeakproof() throws Exception {
        Table<?> procs = DSL.table(DSL.name("pg_catalog", "pg_proc"));
        Field<String> name = DSL.field(DSL.name("proname"), String.class);
        Field<Boolean> leakproof = DSL.field(DSL.name("proleakproof"), Boolean.class);
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES).select(name, leakproof).from(procs)
                .where(name.in("ts_match_vq", "word_similarity_op", "word_similarity_commutator_op"))
                .orderBy(name).fetch();
            synchronized (evidence) {
                evidence.put("proleakproof of the text gate's operator functions", rows.toString());
            }
            assertThat(rows.getValues(name)).as("every operator function the gate uses must be found")
                .contains("ts_match_vq", "word_similarity_op", "word_similarity_commutator_op");
            assertThat(rows.getValues(leakproof)).as("none of them is leakproof").doesNotContain(true);
        }
    }

    // ── topic-scoped ────────────────────────────────────────────────────────

    @Test
    void topicScopedSearch_inlinesLiveC() {
        Table<?> fn = SEARCH_TOPIC_SCOPED_384.call(queryVec(), TOPIC_LABEL, COLL, 10);
        String plan = explain("search_topic_scoped_384", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "search_topic_scoped_384");
    }

    // ── hybrid ──────────────────────────────────────────────────────────────

    @Test
    void hybridSearchFunction_inlinesLiveC() {
        Table<?> fn = HYBRID_SEARCH_384.call(queryVec(), COMMON_TOKEN, new String[] {COLL}, null, 10);
        String plan = explain("hybrid_search_384", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "hybrid_search_384");
    }

    // ── the gate probe (vectors-029, nexus-wbfpw.48) ────────────────────────
    //
    // nexus.text_gate_probe_<dim> is SECURITY DEFINER since vectors-029, so it is never inlined: EXPLAIN of a
    // call is one "Function Scan" line and says nothing about the body. The body's own plan, as nexus_svc,
    // is read from auto_explain's nested-statement log (probeBodyPlan). The container was migrated as a
    // non-superuser owner (startAll), which is what makes those plans production's: a superuser owner would
    // run the definer body past RLS.

    private static Table<?> probe384(String token, String... collections) {
        return TEXT_GATE_PROBE_384.call(token, collections, null, null, PgVectorRepository.SELECTIVE_GATE_MAX + 1);
    }

    /** Chunks the selective token matches that live(c) shows, from the fixture's own arithmetic. */
    private static int expectedSelectiveLive() {
        int manifested = NUM_CHUNKS * 9 / 10;
        int perDoc = Math.max(1, manifested / NUM_DOCS);
        int live = 0;
        for (int i = 0; i < NUM_CHUNKS; i += 100) {
            if (i < manifested && Math.min(NUM_DOCS - 1, i / perDoc) % 3 != 0) live++;
        }
        return live;
    }

    /**
     * Timing, as nexus_svc, of the selective-gate probe: the number the bead measured (77 ms before
     * vectors-029, 24,000 chunks). Evidence only, never asserted; the row count is.
     */
    @Test
    void gateProbe_selective_asNexusSvc_returnsTheLiveMatchesAndIsTimed() {
        Table<?> fn = probe384(RARE_TOKEN, COLL);
        explain("text_gate_probe_384 (selective gate), call as nexus_svc", ctx -> ctx.selectFrom(fn));
        int rows = tenantScope.withTenant(TENANT, ctx -> ctx.selectFrom(fn).fetch().size());
        assertThat(rows).as("the selective gate returns exactly the live rare chunks").isEqualTo(expectedSelectiveLive());
    }

    /**
     * THE PIN (acceptance of nexus-wbfpw.48): the probe's body, run as nexus_svc under FORCE RLS in a
     * container whose owner is a non-superuser, reaches a GIN text index and does not scan the table.
     * Without vectors-029's owner policy (checked, recorded on the bead) the same function, still SECURITY
     * DEFINER, does not reach the text indexes: the definer is the table owner and FORCE applies to it, so the
     * body carries the row-level-security qual again and the text predicates stay a Filter over chunks_pk.
     */
    @Test
    void gateProbe_selective_underNexusSvcRls_reachesAGinTextIndex() throws Exception {
        String plan = probeBodyPlan("text_gate_probe_384 (selective gate), body plan as nexus_svc", RARE_TOKEN);
        assertThat(plan)
            .as("the selective gate must be driven by idx_chunks_tsv or idx_chunks_trgm. Plan was:%n%s", plan)
            .containsPattern("Bitmap Index Scan on idx_chunks_(tsv|trgm)")
            .doesNotContain("Seq Scan on chunks");
        assertThat(plan).as("live(c) stays inlined in the body. Plan was:%n%s", plan)
            .doesNotContain("chunk_live_owners")
            .contains("catalog_document_chunks");
    }

    /**
     * The upper bound on the DENSE gate's median wall-clock as nexus_svc at the default fixture size, a
     * catastrophe guard and not a benchmark (it is the one place a timing is asserted). Why it exists: the
     * definer body is planned without the call's parameter values, so it cannot choose a sequential scan with
     * an early LIMIT for a common token the way the inlined invoker form could (nexus-6nkn3 documents the
     * generic-plan failure class). The recorded numbers, 24,000 chunks, 70% of rows match the token: see the
     * bead (nexus-wbfpw.48) for this probe's median against the pre-change inlined plan on the same fixture.
     */
    private static final long DENSE_GATE_P50_BOUND_MS = 500;

    /**
     * The dense gate (70% of rows carry the token, far above the selective-gate cap): the body's plan under
     * nexus_svc, and its measured wall-clock. Pins that live(c) stays inlined in the body, that the body's
     * plan is the indexed one rather than a table scan, and that the median stays under a generous bound.
     * The body is planned parameter-blind, so this plan is the same one the rare token gets, by construction.
     */
    @Test
    void gateProbe_dense_underNexusSvc_inlinesLiveC_usesTheIndexedPlan_andStaysUnderTheBound() throws Exception {
        String plan = probeBodyPlan("text_gate_probe_384 (dense gate), body plan as nexus_svc", COMMON_TOKEN);
        assertThat(plan).as("live(c) stays inlined in the body. Plan was:%n%s", plan)
            .doesNotContain("chunk_live_owners")
            .contains("catalog_document_chunks");
        assertThat(plan).as("the dense gate's body is planned as the indexed plan. Plan was:%n%s", plan)
            .containsPattern("Bitmap Index Scan on idx_chunks_(tsv|trgm)")
            .doesNotContain("Seq Scan on chunks");
        String label = "text_gate_probe_384 (dense gate), call as nexus_svc";
        Table<?> fn = probe384(COMMON_TOKEN, COLL);
        explain(label, ctx -> ctx.selectFrom(fn));
        long p50 = p50Ms.get(label);
        assertThat(p50).as("the dense gate's median wall-clock as nexus_svc, %d chunks, 70%% matching",
            NUM_CHUNKS).isLessThan(DENSE_GATE_P50_BOUND_MS);
    }

    /**
     * The three functions vectors-029 changed are definer functions with a pinned search_path and no
     * EXECUTE for PUBLIC; nexus_svc keeps EXECUTE (the calls above would fail otherwise).
     */
    @Test
    void gateProbes_areDefinerFunctionsWithAPinnedSearchPath_andNotCallableByPublic() throws Exception {
        Table<?> procs = DSL.table(DSL.name("pg_catalog", "pg_proc"));
        Field<String> name = DSL.field(DSL.name("proname"), String.class);
        Field<Boolean> definer = DSL.field(DSL.name("prosecdef"), Boolean.class);
        Field<String> config = DSL.field(DSL.name("proconfig")).cast(SQLDataType.VARCHAR);
        Field<String> acl = DSL.field(DSL.name("proacl")).cast(SQLDataType.VARCHAR);
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES).select(name, definer, config, acl).from(procs)
                .where(name.in("text_gate_probe_384", "text_gate_probe_768", "text_gate_probe_1024")).fetch();
            assertThat(rows).hasSize(3);
            for (var row : rows) {
                String fn = row.value1();
                assertThat(row.value2()).as("%s is SECURITY DEFINER", fn).isTrue();
                assertThat(row.value3()).as("%s pins its search_path", fn).contains("search_path=pg_catalog, pg_temp");
                assertThat(row.value4()).as("%s grants EXECUTE to nexus_svc", fn).contains("nexus_svc=X/");
                assertThat(row.value4()).as("%s grants EXECUTE to nobody else but its owner", fn)
                    .doesNotContain("{=X/").doesNotContain(",=X/");
            }
        }
    }

    private List<String> probeAs(String tenant, Table<?> fn) {
        return tenantScope.withTenant(tenant, ctx -> ctx.selectFrom(fn).fetch().getValues(0, byte[].class).stream()
            .map(b -> HexFormat.of().formatHex(b)).toList());
    }

    /**
     * Tenant isolation through every changed function. The definer function bypasses RLS, so isolation
     * rests on its own predicate; this is the test of that predicate. A tenant that names ANOTHER tenant's
     * collection gets nothing, a tenant that names both gets only its own rows, and each tenant sees its own
     * rows (so the empty results are not vacuous).
     */
    @Test
    void gateProbes_neverReturnAnotherTenantsChunks_throughAnyDimension() {
        List<java.util.function.Function<Object[], Table<?>>> dims = List.of(
            a -> TEXT_GATE_PROBE_384.call(RARE_TOKEN, (String[]) a[0], null, null, 10_000),
            a -> TEXT_GATE_PROBE_768.call(RARE_TOKEN, (String[]) a[0], null, null, 10_000),
            a -> TEXT_GATE_PROBE_1024.call(RARE_TOKEN, (String[]) a[0], null, null, 10_000));
        for (var dim : dims) {
            String[] mine = {COLL}, theirs = {OTHER_COLL}, both = {COLL, OTHER_COLL};
            assertThat(probeAs(TENANT, dim.apply(new Object[] {theirs})))
                .as("tenant A naming tenant B's collection").isEmpty();
            List<String> aBoth = probeAs(TENANT, dim.apply(new Object[] {both}));
            assertThat(aBoth).as("tenant A naming both collections: its own live rows only")
                .hasSize(expectedSelectiveLive()).doesNotContainAnyElementsOf(otherChashHex);
            assertThat(probeAs(TENANT, dim.apply(new Object[] {mine})))
                .as("tenant A's own collection").hasSize(expectedSelectiveLive());
            assertThat(probeAs(OTHER_TENANT, dim.apply(new Object[] {mine})))
                .as("tenant B naming tenant A's collection").isEmpty();
            assertThat(probeAs(OTHER_TENANT, dim.apply(new Object[] {both})))
                .as("tenant B naming both collections: its own rows only")
                .containsExactlyInAnyOrderElementsOf(otherChashHex);
        }
    }

    /**
     * Fail closed: with no tenant stamped (a fresh session, where the setting reads NULL) and with the
     * setting empty (what a pooled session reads once a transaction-local stamp has ended), every changed
     * function returns nothing, while the same call with a tenant stamped returns rows.
     */
    @Test
    void gateProbes_returnNothing_whenNoTenantIsStamped_orTheStampIsEmpty() throws Exception {
        List<Table<?>> fns = List.of(
            probe384(RARE_TOKEN, COLL, OTHER_COLL),
            TEXT_GATE_PROBE_768.call(RARE_TOKEN, new String[] {COLL, OTHER_COLL}, null, null, 10_000),
            TEXT_GATE_PROBE_1024.call(RARE_TOKEN, new String[] {COLL, OTHER_COLL}, null, null, 10_000));
        Field<String> setting = DSL.function("current_setting", SQLDataType.VARCHAR,
            DSL.inline("nexus.tenant"), DSL.inline(true));
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME,
                PgContainerHelper.SVC_PASSWORD)) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            assertThat(ctx.select(setting).fetchOne(0)).as("a fresh session has no tenant stamped").isNull();
            for (Table<?> fn : fns) assertThat(ctx.selectFrom(fn).fetch()).as("NULL tenant: %s", fn).isEmpty();
        }
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME,
                PgContainerHelper.SVC_PASSWORD)) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.inline("nexus.tenant"), DSL.inline(""), DSL.inline(true))).fetch();
            assertThat(ctx.select(setting).fetchOne(0)).as("the stamp is the empty string").isEqualTo("");
            for (Table<?> fn : fns) assertThat(ctx.selectFrom(fn).fetch()).as("empty tenant: %s", fn).isEmpty();
            c.rollback();
        }
        assertThat(probeAs(TENANT, fns.get(0))).as("the same call with a tenant stamped returns rows").isNotEmpty();
    }

    /**
     * The empty-string tenant (what a pooled session reads once a transaction-local stamp has ended) must
     * fail closed even when a tenant whose id IS the empty string owns live matching chunks. Only a superuser
     * can write such a chunk (TenantScope refuses a blank id), and the row-level-security policy, which
     * compares tenant_id to the bare setting, WOULD show it to a session stamped ''; the probe's
     * {@code NULLIF(.., '')} is what keeps it from returning it. The control asserts the policy does show
     * it, so this test fails if the NULLIF is removed (checked by hand: with it removed the probes return the
     * empty tenant's chunk).
     */
    @Test
    void gateProbes_failClosedOnAnEmptyStamp_evenWhenAnEmptyIdTenantOwnsLiveMatches() throws Exception {
        List<Table<?>> fns = List.of(
            probe384(RARE_TOKEN, EMPTY_COLL, COLL),
            TEXT_GATE_PROBE_768.call(RARE_TOKEN, new String[] {EMPTY_COLL, COLL}, null, null, 10_000),
            TEXT_GATE_PROBE_1024.call(RARE_TOKEN, new String[] {EMPTY_COLL, COLL}, null, null, 10_000));
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME,
                PgContainerHelper.SVC_PASSWORD)) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.inline("nexus.tenant"), DSL.inline(""), DSL.inline(true))).fetch();
            assertThat(ctx.fetchCount(CHUNKS))
                .as("CONTROL: the row-level-security policy shows the empty-id tenant's chunk to a session stamped ''")
                .isEqualTo(1);
            for (Table<?> fn : fns) {
                assertThat(ctx.selectFrom(fn).fetch())
                    .as("a probe stamped '' must not return the empty-id tenant's chunk: %s", fn).isEmpty();
            }
            c.rollback();
        }
    }


    private static final String[] DIM_NAMES = {"384", "768", "1024"};

    private static Table<?> probeByDim(int dimIndex, String token, String[] collections, String wherePath) {
        return switch (dimIndex) {
            case 0 -> TEXT_GATE_PROBE_384.call(token, collections, null, wherePath, 10_000);
            case 1 -> TEXT_GATE_PROBE_768.call(token, collections, null, wherePath, 10_000);
            default -> TEXT_GATE_PROBE_1024.call(token, collections, null, wherePath, 10_000);
        };
    }

    /**
     * Since vectors-029 the probe is no longer behind a row-level-security barrier, so the planner may
     * evaluate its other quals (the where_path jsonpath, the text operators) on rows before the tenant
     * predicate rejects them. This is the test of the visible consequences: tenant B holds metadata that makes
     * {@code .double()} ERROR where an error surfaces (the control below), next to metadata the probe's path
     * {@code $.v.double() > 1} matches, all live and carrying the rare token. (The probe's {@code @@} is
     * silent, so on this evidence an error cannot escape it; the test pins that and the isolation together.) Tenant A's probe over A's collection alone and over
     * A's plus B's must return the same rows, with no error, and B's rows must never appear.
     */
    @Test
    void gateProbes_whereJsonpath_isUnaffectedByAnotherTenantsHostileMetadata() throws Exception {
        // Non-vacuity: the hostile metadata really makes .double() ERROR when the item method is evaluated where
        // an error surfaces (a STRICT, non-predicate path through jsonb_path_query). The probe's @@ is silent and
        // a comparison predicate turns an error into "unknown", so the probe itself never raises on these rows
        // (observed: V_GT_1 through the non-silent jsonb_path_match does not raise on them either); this proves
        // the values are the kind that would raise if the evaluation were not silent.
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String hostile : List.of("rdr192-other-hostile-0", "rdr192-other-hostile-1", "rdr192-other-hostile-2")) {
                byte[] chash = Chash.ofText(hostile).toBytes();
                var evaluate = DSL.function("jsonb_path_query", org.jooq.JSONB.class, CHUNKS.METADATA,
                    DSL.inline("strict $.v.double()"));
                org.assertj.core.api.Assertions.assertThatThrownBy(() -> ctx.select(evaluate).from(CHUNKS)
                        .where(CHUNKS.TENANT_ID.eq(OTHER_TENANT)).and(CHUNKS.CHASH.eq(chash)).fetch())
                    .as("%s: the path must ERROR on this metadata when evaluated non-silently", hostile)
                    .hasMessageContaining("double");
            }
        }
        for (int dim = 0; dim < DIM_NAMES.length; dim++) {
            String label = "dim " + DIM_NAMES[dim];
            List<String> alone = probeAs(TENANT, probeByDim(dim, RARE_TOKEN, new String[] {META_COLL}, V_GT_1));
            assertThat(alone).as("%s: tenant A's own matches (non-vacuity)", label)
                .containsExactlyInAnyOrderElementsOf(metaMatchChashHex);
            List<String> withB = probeAs(TENANT,
                probeByDim(dim, RARE_TOKEN, new String[] {META_COLL, OTHER_COLL}, V_GT_1));
            assertThat(withB).as("%s: naming tenant B's hostile collection changes nothing for tenant A", label)
                .containsExactlyInAnyOrderElementsOf(alone);
            List<String> b = probeAs(OTHER_TENANT,
                probeByDim(dim, RARE_TOKEN, new String[] {META_COLL, OTHER_COLL}, V_GT_1));
            assertThat(b).as("%s: tenant B's own session evaluates its hostile metadata without error", label)
                .isNotEmpty().isSubsetOf(otherChashHex).doesNotContainAnyElementsOf(metaMatchChashHex);
        }
    }

    /**
     * The policies on nexus.chunks, exactly: the tenant policy, and vectors-029's owner SELECT policy bound to
     * the migrating role alone, never to PUBLIC and never to nexus_svc. (The single-role posture, where the
     * changeset must not create the policy at all, is TextGateProbeSingleRoleGuardIntegrationTest.)
     */
    @Test
    void chunksPolicies_areExactlyTheTenantPolicyAndTheOwnerSelectPolicy() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var policies = PgCatalogProbes.policyRoles(DSL.using(su, SQLDialect.POSTGRES), "nexus", "chunks");
            assertThat(policies.stream().map(PgCatalogProbes.PolicyRoles::toString).toList())
                .containsExactly(
                    "chunks_gate_probe_owner_read roles={" + ADMIN_ROLE + "} cmd=SELECT",
                    "tenant_isolation roles={public} cmd=ALL");
        }
    }

    /**
     * Every SECURITY DEFINER function in schema nexus, pinned as a list. A definer function in this schema
     * runs as the migration role, which owns the tables and, since vectors-029, holds a read-everything policy
     * on nexus.chunks, so a new one is an access path that needs a reviewer. The two ensure_vector_extensions_*
     * helpers are the DBA-side relocation helpers (installed by the test's owner bootstrap exactly as
     * nexus.db.pg_provision installs them); the others are the three gate probes.
     */
    @Test
    void securityDefinerFunctionsInSchemaNexus_areExactlyTheAllowlist() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var definers = PgCatalogProbes.functionShapesIn(DSL.using(su, SQLDialect.POSTGRES), List.of("nexus")).stream()
                .filter(PgCatalogProbes.FunctionShape::securityDefiner)
                .map(f -> f.identity().substring(0, f.identity().indexOf('(')))
                .sorted().toList();
            assertThat(definers).containsExactly(
                "ensure_vector_extensions_relocated", "ensure_vector_extensions_unrelocated",
                "text_gate_probe_1024", "text_gate_probe_384", "text_gate_probe_768");
        }
    }

    /**
     * What vectors-029's owner policy does and does not do, pinned. It applies to the owner only: nexus_svc
     * still sees exactly its own tenant's chunks through the table (and none with no tenant stamped). The
     * owner can now READ every tenant's chunks, the stated cost, and gains no write access: UPDATE and DELETE
     * as the owner without a tenant stamped still affect nothing, the silent no-op that
     * test_changelog_rls_lint documents for migration DML.
     */
    @Test
    void ownerReadPolicy_doesNotWidenTheServiceRole_andDoesNotWidenTheOwnersWrites() throws Exception {
        int seenByB = tenantScope.withTenant(OTHER_TENANT, ctx -> ctx.fetchCount(CHUNKS));
        int seenByA = tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(CHUNKS));
        assertThat(seenByB).as("nexus_svc, tenant B: only B's chunks").isEqualTo(OTHER_CHUNKS + HOSTILE_METADATA.size());
        assertThat(seenByA).as("nexus_svc, tenant A: only A's chunks").isEqualTo(NUM_CHUNKS + META_MATCH + META_NOMATCH);
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME,
                PgContainerHelper.SVC_PASSWORD)) {
            assertThat(DSL.using(c, SQLDialect.POSTGRES).fetchCount(CHUNKS))
                .as("nexus_svc with no tenant stamped sees nothing").isZero();
        }
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            assertThat(ctx.fetchCount(CHUNKS)).as("the owner reads every tenant's chunks (the policy's stated cost)")
                .isEqualTo(NUM_CHUNKS + META_MATCH + META_NOMATCH + OTHER_CHUNKS + HOSTILE_METADATA.size() + 1);
            assertThat(ctx.update(CHUNKS).set(CHUNKS.CHUNK_TEXT, CHUNKS.CHUNK_TEXT).execute())
                .as("the owner's UPDATE with no tenant stamped still affects nothing").isZero();
            assertThat(ctx.deleteFrom(CHUNKS).execute())
                .as("the owner's DELETE with no tenant stamped still affects nothing").isZero();
            c.rollback();
        }
    }

    @Test
    void byChashRank_inlinesLiveC() {
        List<String> some = new ArrayList<>();
        for (int i = 0; i < NUM_CHUNKS; i += 100) some.add(chashHex.get(i));
        byte[][] chashes = some.stream().map(h -> HexFormat.of().parseHex(h)).toArray(byte[][]::new);
        Table<?> fn = TEXT_GATED_SEARCH_BY_CHASH_384.call(queryVec(), chashes, new String[] {COLL}, null, null, 10);
        String plan = explain("text_gated_search_by_chash_384", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "text_gated_search_by_chash_384");
    }

    @Test
    void hnswFirstRank_inlinesLiveC() {
        Table<?> fn = TEXT_GATED_SEARCH_HNSW_FIRST_384.call(queryVec(), COMMON_TOKEN, new String[] {COLL},
            null, null, 10);
        String plan = explain("text_gated_search_hnsw_first_384", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "text_gated_search_hnsw_first_384");
    }

    @Test
    void plainSearch_inlinesLiveC_reference() {
        Table<?> fn = PLAIN_SEARCH_384.call(queryVec(), new String[] {COLL}, null, null, 10);
        String plan = explain("plain_search_384 (reference)", ctx -> ctx.select(fn.field("id")).from(fn));
        assertInlinedLiveC(plan, "plain_search_384");
    }

    // ── collection_vector_stats ─────────────────────────────────────────────

    @Test
    void collectionVectorStats_oneCollection_inlinesLiveC() {
        String plan = explain("collection_vector_stats (one collection)", ctx -> ctx
            .select(COLLECTION_VECTOR_STATS.COLLECTION, COLLECTION_VECTOR_STATS.CHUNK_COUNT,
                    COLLECTION_VECTOR_STATS.STORED_COUNT)
            .from(COLLECTION_VECTOR_STATS)
            .where(COLLECTION_VECTOR_STATS.COLLECTION.eq(COLL)));
        assertInlinedLiveC(plan, "collection_vector_stats");
        assertOneSharedLiveProbe(plan, "collection_vector_stats");
    }

    @Test
    void collectionVectorStats_wholeTenant_inlinesLiveC() {
        String plan = explain("collection_vector_stats (whole tenant)", ctx -> ctx
            .select(COLLECTION_VECTOR_STATS.COLLECTION, COLLECTION_VECTOR_STATS.CHUNK_COUNT,
                    COLLECTION_VECTOR_STATS.STORED_COUNT)
            .from(COLLECTION_VECTOR_STATS));
        assertInlinedLiveC(plan, "collection_vector_stats");
        assertOneSharedLiveProbe(plan, "collection_vector_stats");
    }

    /** The view must also report what the fixture constructed, so the plans above were taken
     *  over a population that has both kinds of hidden chunk. */
    @Test
    void fixtureHasLiveAndHiddenChunks() {
        tenantScope.withTenant(TENANT, ctx -> {
            var row = ctx.select(COLLECTION_VECTOR_STATS.CHUNK_COUNT, COLLECTION_VECTOR_STATS.STORED_COUNT)
                .from(COLLECTION_VECTOR_STATS)
                .where(COLLECTION_VECTOR_STATS.COLLECTION.eq(COLL)).fetchOne();
            assertThat(row.value2()).isEqualTo((long) NUM_CHUNKS);
            long live = row.value1();
            assertThat(live).as("live(c) hides the manifest-less 10% and the tombstoned third of the rest")
                .isBetween((long) (NUM_CHUNKS * 0.55), (long) (NUM_CHUNKS * 0.70));
            return null;
        });
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /**
     * The liveness predicate must have inlined: no call to the function by name (a per-row
     * Function Scan or SubPlan over {@code chunk_live_owners} would show it), and the semi-join
     * target its body reads, {@code catalog_document_chunks}, must be in the plan.
     */
    private static void assertInlinedLiveC(String plan, String what) {
        assertThat(plan)
            .as("%s: chunk_live_owners must inline into a probe of catalog_document_chunks; its name "
                + "in the plan means it stayed an opaque per-row call. Plan was:%n%s", what, plan)
            .doesNotContain("chunk_live_owners")
            .contains("catalog_document_chunks");
        assertThat(plan)
            .as("%s: no Function Scan over the search function itself either. Plan was:%n%s", what, plan)
            .doesNotContain("Function Scan");
    }

    /**
     * The view reads {@code chunk_count} and {@code last_write} through the same live(c) test, and the
     * planner evaluates it ONCE per chunk and feeds both aggregates (one SubPlan). The view is O(chunks)
     * by construction, so a second probe per chunk would double a cost that every {@code list_collections}
     * pays; this fails if a change makes the two aggregates probe separately.
     */
    private static void assertOneSharedLiveProbe(String plan, String what) {
        assertThat(plan)
            .as("%s: live(c) must be one per-chunk probe shared by both aggregates. Plan was:%n%s", what, plan)
            .contains("SubPlan 1")
            .doesNotContain("SubPlan 2");
    }

    private static final String BODY_MARKER = "NULLIF(pg_catalog.current_setting('nexus.tenant', true)";
    private static final Pattern NEXT_LOG_LINE = Pattern.compile("\n\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:");

    /**
     * Run the probe as a fresh nexus_svc session that has auto_explain on (explainDs, opened after the role
     * was configured) and return the plan auto_explain logged for the probe's BODY, the nested statement.
     * Taken from the container's server log; the log line is written asynchronously, so this polls for it.
     * The whole entry lands in the evidence file; the return value is the plan alone.
     */
    private String probeBodyPlan(String label, String token) throws Exception {
        int from = pg.getLogs().length();
        Table<?> fn = probe384(token, COLL);
        int rows = explainScope.withTenant(TENANT, ctx -> ctx.selectFrom(fn).fetch().size());
        String block = null;
        for (int attempt = 0; attempt < 100 && block == null; attempt++) {
            String logs = pg.getLogs();
            int mark = logs.indexOf(BODY_MARKER, from);
            if (mark >= 0) {
                int begin = logs.lastIndexOf("LOG:  duration:", mark);
                Matcher next = NEXT_LOG_LINE.matcher(logs);
                int end = next.find(mark) ? next.start() : logs.length();
                block = logs.substring(Math.max(begin, from), end);
            } else {
                Thread.sleep(100);
            }
        }
        assertThat(block).as("auto_explain logged no plan for the probe's body (token %s)", token).isNotNull();
        synchronized (evidence) {
            evidence.put(label, "rows=" + rows + "\n" + block);
        }
        // The log entry leads with the statement's own text, which names chunk_live_owners as written; the
        // plan is what follows the parameters line, and that is what a caller may assert on.
        int params = block.indexOf("Query Parameters:");
        assertThat(params).as("auto_explain's entry carries the parameters line. Entry was:%n%s", block).isNotNegative();
        return block.substring(block.indexOf('\n', params) + 1);
    }

    private Vector queryVec() {
        return Vector.of(vectors.get(7));
    }

    /**
     * EXPLAIN as nexus_svc inside the tenant scope, at the planner's own choice, recorded for the
     * evidence file together with the statement's measured wall-clock (one warm-up run, then the
     * median of {@value #TIMED_RUNS}) and the row count it returned. Timing is evidence only and is
     * never asserted: it moves with the box.
     */
    private String explain(String label, Function<DSLContext, ? extends ResultQuery<?>> queryBuilder) {
        return explainWith(label, List.of(), queryBuilder);
    }

    private String explainWith(String label, List<String> offGucs,
                               Function<DSLContext, ? extends ResultQuery<?>> queryBuilder) {
        return tenantScope.withTenant(TENANT, ctx -> {
            // The same serving GUCs the repository sets before an ordered vector fetch, so the plan is
            // the one production gets and not the one a default session gets.
            PgSession.setHnswEfSearch(ctx, 10);
            for (String guc : offGucs) PgSession.setLocal(ctx, guc, "off");
            ResultQuery<?> q = queryBuilder.apply(ctx);
            String plan = ctx.explain(q).plan();
            int rows = q.fetch().size();                       // warm-up
            long[] ms = new long[TIMED_RUNS];
            for (int i = 0; i < TIMED_RUNS; i++) {
                long t0 = System.nanoTime();
                q.fetch();
                ms[i] = (System.nanoTime() - t0) / 1_000_000;
            }
            java.util.Arrays.sort(ms);
            synchronized (evidence) {
                p50Ms.put(label, ms[TIMED_RUNS / 2]);
                evidence.put(label, "rows=" + rows + "  p50=" + ms[TIMED_RUNS / 2] + " ms  (runs "
                    + java.util.Arrays.toString(ms) + ")\n" + plan);
            }
            return plan;
        });
    }

    private void writeEvidence() throws IOException {
        Path out = Paths.get(System.getProperty("user.dir"), "target", "rdr192-explain-evidence.txt");
        Files.createDirectories(out.getParent());
        StringBuilder sb = new StringBuilder();
        sb.append("RDR-192 live(c) plan evidence (nexus-wbfpw.37), role nexus_svc, ")
          .append(NUM_CHUNKS).append(" chunks, ").append(NUM_DOCS).append(" documents\n");
        synchronized (evidence) {
            evidence.forEach((label, plan) ->
                sb.append("\n=== ").append(label).append(" ===\n").append(plan).append('\n'));
        }
        Files.writeString(out, sb.toString());
    }
}
