// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 2 round-3 fix (bead nexus-wbfpw.4, code-review Significant):
 * corrected plan-shape investigation for {@link PgVectorRepository#MANIFEST_LESS_CENSUS_SQL}'s
 * reverse-owner resolution.
 *
 * <p><strong>Why catalog-038's {@code metadata ->> 'doc_id'} index was dropped
 * (round 3; mechanism corrected in round 4).</strong> Structurally: after the
 * round-3 rewrite, the reverse predicate is not a per-row WHERE-clause equality
 * against {@code catalog_documents} anywhere in the statement. {@code
 * metadata ->> 'doc_id'} is computed once per live note in {@code live_notes}'
 * SELECT list and joined to {@code rev_candidates} on that derived column, so no
 * expression index on it can be chosen, under any role.
 *
 * <p>Why the index was not used even before the rewrite: round 2 blamed
 * bind-parameter selectivity after measuring through {@code
 * pg.createConnection("")}, a Testcontainers SUPERUSER connection that bypasses
 * row-level security. That was wrong. Under a NOSUPERUSER NOBYPASSRLS role such
 * as nexus_svc ({@code svcDs}/{@code tenantScope} below, created via {@link
 * PgContainerHelper#bootstrapServiceRole}), FORCE ROW LEVEL SECURITY applies the
 * policy qual as a security barrier, and a user qual is evaluated below it (so
 * can become an index condition) only when every function it calls is
 * LEAKPROOF. {@code texteq} is leakproof, so {@code live_notes}' tenant_id and
 * physical_collection equalities still use {@code
 * idx_catalog_documents_collection_live}; {@code jsonb_object_field_text} (the
 * {@code ->>} operator) is not, so a {@code metadata ->> 'doc_id'} qual stays
 * above the barrier whether its value is a literal or a bind. {@link
 * #leakproofFlags_explainWhichQualsCanReachAnIndexUnderRls} pins both flags.
 *
 * <p>{@link #liveNotesCte_isMaterializedOnce_regardlessOfHowManyOuterChunksProbeIt}
 * measures the rewrite through the RLS-subject pool (never a superuser
 * connection) with {@code EXPLAIN (ANALYZE)}: {@code live_notes} and {@code
 * rev_candidates} each run once per statement, and {@code live_notes} is served
 * by an index scan on {@code idx_catalog_documents_collection_live}, not a
 * sequential scan of {@code catalog_documents}.
 *
 * <p>The index-scan outcome depends on selectivity, and the fixture manufactures
 * it: {@code OTHER_COLLECTION_ROWS} makes the census collection about 9% of the
 * tenant's catalog rows. No production collections-per-tenant cardinality has
 * been measured. For a tenant whose notes are most of {@code catalog_documents},
 * a sequential scan is the correct plan, and it is still one scan per statement.
 * The regression this class guards is the per-row re-scan (loops above 1), which
 * holds at any selectivity; the index-scan assertion only pins the planner's
 * choice at the selectivity this fixture sets up.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS lifecycle.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestLessCensusNotesGuardIndexPlanShapeTest {

    private static final String SVC_ROLE = "svc_wbfpw4_idx_planshape";
    private static final String SVC_PASS = "svc_wbfpw4_idx_planshape_pass";
    private static final String TENANT = "wbfpw4-idx-planshape";
    private static final String COLLECTION = "knowledge__wbfpw4-idx-planshape__minilm-l6-v2-384__v1";

    // Modest but non-trivial cardinality: large enough that a per-row re-scan of
    // catalog_documents would be visibly expensive if the rewrite regressed back
    // to one, small enough to seed fast under Testcontainers. Matches this repo's
    // established "a few thousand rows" precedent (PgVectorRepositoryRawSqlPlanShapeTest
    // .CHUNKS_PER_DIM = 4_000).
    private static final int NOISE_NOTE_ROWS = 5_000;

    // Live documents in OTHER collections of the same tenant. Production holds
    // many collections per tenant, so live_notes' (tenant_id, physical_collection)
    // predicate selects a small fraction of catalog_documents; without these rows
    // it would select every row and a sequential scan would be the right plan.
    private static final int OTHER_COLLECTION_ROWS = 50_000;
    private static final String OTHER_COLLECTION = "knowledge__wbfpw4-idx-planshape-other__minilm-l6-v2-384__v1";

    // Multiple manifest-less outer chunks, each resolved only in reverse, so a
    // regression back to a per-row correlated LATERAL would show up as
    // catalog_documents being re-scanned NUM_TARGETS times (loops=NUM_TARGETS)
    // instead of once (loops=1).
    private static final int NUM_TARGETS = 5;

    private static final List<String> TARGET_CHASHES = IntStream.range(0, NUM_TARGETS)
        .mapToObj(i -> Chash.ofText("wbfpw4-idx-planshape-target-" + i).toHex())
        .collect(Collectors.toList());
    private static final List<String> TARGET_NOTE_DOCS = IntStream.range(0, NUM_TARGETS)
        .mapToObj(i -> "wbfpw4-idx-planshape-target-note-" + i)
        .collect(Collectors.toList());

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    PgVectorRepository vecRepo;
    HikariDataSource svcDs;

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
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        var embedder = new ConstantEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }

        seedNoiseAndTargets();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * {@code NOISE_NOTE_ROWS} live, note-shaped {@code catalog_documents} rows in
     * {@code COLLECTION}, each with a DISTINCT {@code metadata.doc_id} (server-side
     * {@code md5} concatenation — the SAME "no pgcrypto needed" pattern {@link
     * dev.nexus.service.ChashProbePlanShapeTest} uses for bulk chash-shaped fixture
     * values), none of which match any target chash. {@code NUM_TARGETS} additional
     * note rows each carry {@code metadata.doc_id} = one target chash exactly — the
     * rows the reverse lookup must actually find. {@code NUM_TARGETS} manifest-less
     * T3 chunks (empty chunk metadata — no forward key at all) force the reverse
     * path to resolve every one of them.
     */
    private void seedNoiseAndTargets() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            Statement st = su.createStatement();
            st.execute(
                "INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, content_type, corpus, physical_collection, metadata) "
                + "SELECT '" + TENANT + "', 'wbfpw4-idx-planshape-noise-' || i, "
                + "       'noise note ' || i, 'prose', 'knowledge', '" + COLLECTION + "', "
                + "       jsonb_build_object('doc_id', md5('noise-a-' || i) || md5('noise-b-' || i)) "
                + "FROM generate_series(1, " + NOISE_NOTE_ROWS + ") i");
            st.execute(
                "INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, content_type, corpus, physical_collection, metadata) "
                + "SELECT '" + TENANT + "', 'wbfpw4-idx-planshape-other-' || i, "
                + "       'other collection doc ' || i, 'prose', 'knowledge', '" + OTHER_COLLECTION + "', "
                + "       jsonb_build_object('doc_id', md5('other-a-' || i) || md5('other-b-' || i)) "
                + "FROM generate_series(1, " + OTHER_COLLECTION_ROWS + ") i");
            for (int i = 0; i < NUM_TARGETS; i++) {
                st.execute(
                    "INSERT INTO nexus.catalog_documents "
                    + "(tenant_id, tumbler, title, content_type, corpus, physical_collection, metadata) "
                    + "VALUES ('" + TENANT + "', '" + TARGET_NOTE_DOCS.get(i) + "', 'target note', 'prose', "
                    + "        'knowledge', '" + COLLECTION + "', "
                    + "        jsonb_build_object('doc_id', '" + TARGET_CHASHES.get(i) + "'))");
            }
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }

        vecRepo.upsertChunks(TENANT, COLLECTION, TARGET_CHASHES,
            TARGET_CHASHES.stream().map(c -> "reverse-lookup plan-shape text " + c).toList(),
            IntStream.range(0, NUM_TARGETS).mapToObj(i -> Map.<String, Object>of()).toList());
    }

    // Matches each CTE's own MATERIALIZATION node -- "CTE <name>" followed by its
    // first child plan node's loops= count -- never a later "CTE Scan on <name>"
    // READ of the already-materialized tuplestore, which legitimately shows
    // loops=NUM_TARGETS (one cheap re-read per outer probe) regardless of how many
    // times the underlying computation itself ran.
    private static final Pattern LIVE_NOTES_MATERIALIZATION_LOOPS =
        Pattern.compile("CTE live_notes\\s*\\n\\s*->.*?loops=(\\d+)", Pattern.DOTALL);
    private static final Pattern REV_CANDIDATES_MATERIALIZATION_LOOPS =
        Pattern.compile("CTE rev_candidates\\s*\\n\\s*->.*?loops=(\\d+)", Pattern.DOTALL);

    private String explainFullStatement() {
        return tenantScope.withTenant(TENANT, ctx -> {
            Result<Record> rows = ctx.fetch(
                "EXPLAIN (ANALYZE, VERBOSE, COSTS OFF) " + PgVectorRepository.MANIFEST_LESS_CENSUS_SQL,
                TENANT, COLLECTION, TENANT, COLLECTION, 300, 0);
            List<String> lines = new ArrayList<>();
            for (var rec : rows) {
                lines.add(rec.get(0, String.class));
            }
            return String.join("\n", lines);
        });
    }

    /**
     * The whole point of round 3's rewrite: {@code live_notes} — the CTE that
     * scans {@code nexus.catalog_documents} for this collection's live,
     * note-shaped rows — is materialized ONCE per statement execution, regardless
     * of {@code NUM_TARGETS} manifest-less outer chunks each needing reverse
     * resolution. A regression back to a per-row correlated LATERAL would show
     * {@code loops=NUM_TARGETS} (or more) on that scan instead of {@code loops=1}.
     * Run through {@code tenantScope} (the RLS-subject {@code SVC_ROLE} pool,
     * NEVER a superuser connection) — the shape that actually matters, per round
     * 3's corrected diagnosis above.
     */
    @Test
    void liveNotesCte_isMaterializedOnce_regardlessOfHowManyOuterChunksProbeIt() {
        String plan = explainFullStatement();

        Matcher liveNotes = LIVE_NOTES_MATERIALIZATION_LOOPS.matcher(plan);
        assertThat(liveNotes.find())
            .as("plan must contain a live_notes CTE materialization node with a loops= count."
                + " Plan was:%n%s", plan)
            .isTrue();
        assertThat(Integer.parseInt(liveNotes.group(1)))
            .as("live_notes must be materialized exactly ONCE per statement execution -- a"
                + " regression back to a per-row correlated LATERAL would show loops=%d (one per"
                + " outer manifest-less chunk) instead. Plan was:%n%s", NUM_TARGETS, plan)
            .isEqualTo(1);

        // rev_candidates' own DISTINCT ON reduction must ALSO run once, not once per
        // outer probe -- without MATERIALIZED here, Postgres re-sorted/re-deduped the
        // whole live_notes population on every outer nested-loop iteration (measured:
        // its own Unique/Sort node showed loops=NUM_TARGETS before MATERIALIZED was
        // added here), even though the underlying live_notes scan itself was already
        // correctly materialized. A LATER "CTE Scan on rev_candidates" node legitimately
        // shows loops=NUM_TARGETS too -- that is a cheap re-read of the already-computed
        // tuplestore, not a re-computation, and is not what this assertion checks.
        Matcher revCandidates = REV_CANDIDATES_MATERIALIZATION_LOOPS.matcher(plan);
        assertThat(revCandidates.find())
            .as("plan must contain a rev_candidates CTE materialization node with a loops= count."
                + " Plan was:%n%s", plan)
            .isTrue();
        assertThat(Integer.parseInt(revCandidates.group(1)))
            .as("rev_candidates' DISTINCT ON reduction must be materialized exactly ONCE -- without"
                + " it, this same Sort/Unique work re-runs once per outer manifest-less chunk"
                + " (loops=%d) instead. Plan was:%n%s", NUM_TARGETS, plan)
            .isEqualTo(1);

        assertThat(plan)
            .as("catalog-038's index was dropped at round 3 (see class javadoc) -- it must not"
                + " appear in this statement's plan at all. Plan was:%n%s", plan)
            .doesNotContain("idx_catalog_documents_live_note_doc_id");

        // Round 4 (critique observation): the cost claim is that live_notes' single
        // scan is an index scan on idx_catalog_documents_collection_live, even under
        // the RLS security barrier, because its quals are leakproof text equalities.
        String liveNotesSubtree = plan.substring(
            plan.indexOf("CTE live_notes"), plan.indexOf("CTE rev_candidates"));
        assertThat(liveNotesSubtree)
            .as("live_notes must reach catalog_documents through idx_catalog_documents_collection_live"
                + " under the RLS-subject role. live_notes subtree was:%n%s", liveNotesSubtree)
            .contains("idx_catalog_documents_collection_live")
            .containsPattern("(Index|Bitmap Index) Scan")
            .doesNotContain("Seq Scan on nexus.catalog_documents");
    }

    /**
     * The leakproof flags the SQL header's mechanism rests on (round 4, critique
     * Significant 1). Under FORCE RLS for a non-bypass role, only quals built from
     * leakproof functions are evaluated below the policy's security barrier and
     * can drive an index scan. {@code texteq} (text {@code =}) is leakproof;
     * {@code jsonb_object_field_text} (jsonb {@code ->>} text) is not. If a future
     * PostgreSQL release flips either flag, the header's explanation is stale and
     * this fails.
     */
    @Test
    void leakproofFlags_explainWhichQualsCanReachAnIndexUnderRls() {
        Map<String, Boolean> flags = tenantScope.withTenant(TENANT, ctx -> {
            Map<String, Boolean> out = new java.util.HashMap<>();
            ctx.fetch("SELECT p.proname, p.proleakproof FROM pg_operator o "
                    + "JOIN pg_proc p ON p.oid = o.oprcode "
                    + "WHERE (o.oprname = '=' AND o.oprleft = 'text'::regtype AND o.oprright = 'text'::regtype) "
                    + "   OR (o.oprname = '->>' AND o.oprleft = 'jsonb'::regtype AND o.oprright = 'text'::regtype)")
               .forEach(r -> out.put(r.get(0, String.class), r.get(1, Boolean.class)));
            return out;
        });
        assertThat(flags)
            .containsEntry("texteq", true)
            .containsEntry("jsonb_object_field_text", false);
    }

    /** Minimal {@link Embedder}: content of the vector is irrelevant — only chash/collection/metadata movement is under test. */
    private static final class ConstantEmbedder implements Embedder {
        private final int dim;
        ConstantEmbedder(int dim) { this.dim = dim; }

        @Override
        public List<float[]> embed(List<String> texts) {
            float[] v = new float[dim];
            v[0] = 1.0f;
            return texts.stream().map(t -> v.clone()).toList();
        }

        @Override
        public void close() { }
    }
}
