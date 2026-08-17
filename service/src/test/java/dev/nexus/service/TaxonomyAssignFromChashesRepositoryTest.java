// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.List;
import java.util.Map;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

/**
 * nexus-lns3o (engine half) — {@code TaxonomyRepository#assignFromChashes} contract
 * suite. T2 [21580] flush-tail-attribution-2026-08-07 NEW-A: server-side
 * compute-and-persist taxonomy assignment from just-upserted chashes.
 *
 * <p>PARITY DERIVATION (the "python-side contract test" the design gate asked for
 * — encoded here as fixture expectations rather than a cross-language harness,
 * per the worktree constraint): every scenario below is hand-derived from
 * {@code HttpTaxonomyStore.compute_assignments} (src/nexus/db/t2/http_taxonomy_store.py
 * ~line 695) and {@code TaxonomyRepository.assignOne}'s upsert semantics (this file,
 * ~line 493), NOT reverse-engineered from this implementation:
 * <ul>
 *   <li>{@code compute_assignments} computes {@code sim = _cosine_matrix(q, cent)}
 *       (normalized dot product == 1 - cosine distance) then
 *       {@code nearest_idx = sim.argmax(axis=1)} — a PURE argmax, no threshold. A
 *       chash is assigned to its single nearest centroid regardless of how low the
 *       similarity is. {@code PROJECTION_THRESHOLD} gates a DIFFERENT function
 *       ({@code compute_cross_links}) and never appears in {@code compute_assignments}.</li>
 *   <li>{@code if not c_embs or not embeddings: return []} — an empty centroid set
 *       (or empty query batch) short-circuits to NO assignments, silently. This is
 *       a normal empty-taxonomy state, not an error and not an "unmatched" input.</li>
 *   <li>own pass ({@code cross_collection=False}): the Python-side dict this test's
 *       fixtures were originally derived from carries {@code "similarity": None,
 *       "source_collection": None}. {@code assignFromChashesOnePass}'s non-projection
 *       (centroid) branch persists {@code assigned_by='centroid'} with similarity/
 *       assigned_at NULL and {@code ON CONFLICT DO NOTHING} (first-write-wins,
 *       idempotent) exactly as derived. CORRECTED (RDR-194 D1, nexus-tk070.p3a):
 *       {@code source_collection} is NO LONGER NULL on this branch. It now persists
 *       {@code p_collection} (the collection the chash came from, the same value
 *       already used three lines above as this branch's own WHERE filter), a
 *       prerequisite for D1's composite FK from {@code topic_assignments} to
 *       {@code chunks}: a NULL {@code source_collection} escapes that FK through
 *       MATCH SIMPLE, which is exactly the vacuous-VALIDATE failure mode D1 exists
 *       to close. This is a deliberate divergence from the pre-P3a Python dict this
 *       parity derivation otherwise still holds for.</li>
 *   <li>cross pass ({@code cross_collection=True}): the dict carries the REAL
 *       computed similarity and {@code source_collection=collection_name} (the
 *       collection the chashes came FROM, not the foreign collection the matched
 *       topic belongs to) — {@code assignOne}'s projection branch persists
 *       {@code assigned_by='projection'}, {@code ON CONFLICT DO UPDATE} with
 *       {@code GREATEST(existing, new)} on similarity, assigned_at/source_collection
 *       following the winning value.</li>
 * </ul>
 *
 * <p>Unit vectors ({@code (x, y, 0, ..., 0)}) throughout so cosine similarity is
 * exactly hand-computable (same fixture idiom as
 * {@link TaxonomyCentroidRepositoryTest}) — this IS the parity pin: the expected
 * float values in each assertion are computed independently of the SQL, from the
 * geometry of the vectors chosen.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyAssignFromChashesRepositoryTest {

    private static final String SVC_ROLE = "svc_afc_test";
    private static final String SVC_PASS = "svc_afc_test_pass";

    private static final String TENANT_A = "afc-tenant-a";
    private static final String TENANT_B = "afc-tenant-b";

    // Four-segment conformant (RDR-103) — dimForCollection requires this shape.
    // voyage-code-3 -> 1024. Used only where no centroid is ever seeded (see the
    // per-test dedicated-collection note on ownPass_assignsNearestCentroid...).
    private static final String COL_A = "code__afc_a__voyage-code-3__v1";
    private static final int DIM = 1024;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TaxonomyRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            // RDR-191 unify (vectors-004/taxonomy-007): chunks_<dim> and
            // taxonomy_centroids_<dim> collapse into ONE unified table each
            // (nexus.chunks / nexus.taxonomy_centroids, three nullable typed
            // embedding_<dim> columns) -- one grant apiece, not a dim loop.
            // assign_from_chashes_<dim> stays THREE separate typed functions
            // (DECISION 2, T2 [22445]), so its EXECUTE grant keeps the loop.
            su.createStatement().execute(
                "GRANT SELECT ON nexus.chunks TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.taxonomy_centroids TO " + SVC_ROLE);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT EXECUTE ON FUNCTION nexus.assign_from_chashes_" + dim
                    + "(text, text[], boolean) TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.topics TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT, UPDATE ON SEQUENCE nexus.topics_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.topic_assignments TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    // ── own pass ("centroid") ────────────────────────────────────────────────────

    @Test
    void ownPass_assignsNearestCentroid_noThreshold_similarityAlwaysNull() throws Exception {
        // Dedicated collection (nexus-lns3o review S1): COL_A-style sharing across test
        // methods in this PER_CLASS suite accumulates centroids cross-test, and the
        // unit-vector fixtures deliberately reuse the same (1,0)/(0,1) points across
        // methods — a shared collection risks an EARLIER test's centroid winning this
        // test's argmax (or a tie broken by the smaller topic_id). Every test that seeds
        // an OWN centroid gets its own collection.
        String col = "code__afc_own1__voyage-code-3__v1";
        String c1 = hexChash("afc-chash-own-1");
        String c2 = hexChash("afc-chash-own-2");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        seedChunk(TENANT_A, col, c2, unit(0.0f, 1.0f));
        // t2 is FAR from t1's chunk (orthogonal) — proves no minimum-similarity
        // threshold: compute_assignments' sim.argmax has none, so even a near-zero
        // similarity still wins when it is the nearest available centroid.
        long t1 = seedTopic(TENANT_A, col, "own-topic-1");
        long t2 = seedTopic(TENANT_A, col, "own-topic-2");
        seedCentroid(TENANT_A, col, t1, unit(1.0f, 0.0f));
        seedCentroid(TENANT_A, col, t2, unit(0.0f, 1.0f));

        Map<String, Object> out = repo.assignFromChashes(TENANT_A, col, List.of(c1, c2), false);
        assertThat(out.get("assigned")).isEqualTo(2);
        assertThat(out.get("cross_assigned")).isEqualTo(0);
        assertThat((List<?>) out.get("unmatched_chashes")).isEmpty();

        List<Map<String, Object>> details = repo.getAssignmentDetails(TENANT_A, List.of(c1, c2));
        assertThat(details).hasSize(2);
        assertThat(details).anySatisfy(r -> {
            assertThat(r.get("doc_id")).isEqualTo(c1);
            assertThat(r.get("topic_id")).isEqualTo(t1);
            assertThat(r.get("assigned_by")).isEqualTo("centroid");
            assertThat(r.get("similarity")).as("own pass: similarity always NULL (compute_assignments"
                + " else-branch: \"similarity\": None)").isNull();
            assertThat(r.get("source_collection")).as("RDR-194 D1 (nexus-tk070.p3a): the centroid"
                + " branch now persists source_collection = p_collection instead of NULL,"
                + " the D1 composite-FK prerequisite").isEqualTo(col);
        });
        assertThat(details).anySatisfy(r -> {
            assertThat(r.get("doc_id")).isEqualTo(c2);
            assertThat(r.get("topic_id")).isEqualTo(t2);
        });
    }

    @Test
    void ownPass_onConflict_doesNothing_firstWriteWins() throws Exception {
        String col = "code__afc_idem__voyage-code-3__v1";  // dedicated (see previous test's note)
        String c1 = hexChash("afc-chash-idem-1");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        long t1 = seedTopic(TENANT_A, col, "idem-topic-1");
        long t2 = seedTopic(TENANT_A, col, "idem-topic-2");
        seedCentroid(TENANT_A, col, t1, unit(1.0f, 0.0f));
        seedCentroid(TENANT_A, col, t2, unit(0.9f, 0.436f));  // slightly worse match

        Map<String, Object> first = repo.assignFromChashes(TENANT_A, col, List.of(c1), false);
        assertThat(first.get("assigned")).isEqualTo(1);
        assertThat(repo.getAssignmentDetails(TENANT_A, List.of(c1)))
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(t1));

        // Re-run: compute_assignments still COMPUTES an assignment (assigned=1 —
        // the count reflects what was computed, not what changed), but persist is
        // ON CONFLICT DO NOTHING — the existing row (t1) must survive untouched
        // even though this call recomputes the same nearest centroid.
        Map<String, Object> second = repo.assignFromChashes(TENANT_A, col, List.of(c1), false);
        assertThat(second.get("assigned")).as("recomputed, still counted").isEqualTo(1);
        assertThat(repo.getAssignmentDetails(TENANT_A, List.of(c1)))
            .as("DO NOTHING: still exactly one row, still t1")
            .singleElement().satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(t1));
    }

    @Test
    void emptyTaxonomy_noCentroids_isSilentNoOp_notUnmatched() throws Exception {
        // Deliberately NO centroids seeded for this fresh chash's scope check —
        // use a dedicated collection so no other test's centroids leak in.
        String freshCol = "code__afc_freshtax__voyage-code-3__v1";
        String c1fresh = hexChash("afc-chash-empty-tax-fresh");
        seedChunk(TENANT_A, freshCol, c1fresh, unit(1.0f, 0.0f));

        Map<String, Object> out = repo.assignFromChashes(
            TENANT_A, freshCol, List.of(c1fresh), false);
        assertThat(out.get("assigned")).as("no centroids in scope: compute_assignments'"
            + " `if not c_embs: return []` short-circuit").isEqualTo(0);
        assertThat((List<?>) out.get("unmatched_chashes"))
            .as("the chunk EXISTS — an empty taxonomy is not an unmatched chash")
            .isEmpty();
    }

    @Test
    void unmatchedChash_neverUpserted_isNamedNeverSilentlyDropped() throws Exception {
        String col = "code__afc_unmatched__voyage-code-3__v1";  // dedicated, see above note
        String real = hexChash("afc-chash-real");
        String ghost = hexChash("afc-chash-never-upserted");
        seedChunk(TENANT_A, col, real, unit(1.0f, 0.0f));
        long t = seedTopic(TENANT_A, col, "unmatched-topic");
        seedCentroid(TENANT_A, col, t, unit(1.0f, 0.0f));

        Map<String, Object> out = repo.assignFromChashes(TENANT_A, col, List.of(real, ghost), false);
        assertThat(out.get("assigned")).isEqualTo(1);
        assertThat((List<String>) (List<?>) out.get("unmatched_chashes"))
            .as("a chash never upserted into this collection is NAMED, not silently dropped")
            .containsExactly(ghost);
    }

    @Test
    void uppercaseHexChash_assignsAndIsNotReportedUnmatched() throws Exception {
        // nexus-lns3o review fix (code-review-expert MEDIUM): the SQL function
        // decodes case-insensitively (decode(x,'hex') -> bytea, byte-level match)
        // but the existence probe's ChashHex.hex(...) always renders lowercase —
        // an uppercase-hex input must still assign AND must not be misreported as
        // unmatched. The seeded chunk row itself is stored lowercase (as production
        // always writes it); only the CALLER's input casing varies here.
        String col = "code__afc_uppercase__voyage-code-3__v1";  // dedicated, see above note
        String lower = hexChash("afc-chash-uppercase-input");
        String upper = lower.toUpperCase(java.util.Locale.ROOT);
        seedChunk(TENANT_A, col, lower, unit(1.0f, 0.0f));
        long t = seedTopic(TENANT_A, col, "uppercase-topic");
        seedCentroid(TENANT_A, col, t, unit(1.0f, 0.0f));

        Map<String, Object> out = repo.assignFromChashes(TENANT_A, col, List.of(upper), false);
        assertThat(out.get("assigned")).as("uppercase-hex input must still assign").isEqualTo(1);
        assertThat((List<?>) out.get("unmatched_chashes"))
            .as("an uppercase-hex input that DID assign must not also be named unmatched")
            .isEmpty();

        List<Map<String, Object>> details = repo.getAssignmentDetails(TENANT_A, List.of(lower));
        assertThat(details).singleElement()
            .satisfies(r -> assertThat(r.get("topic_id")).isEqualTo(t));
    }

    @Test
    void maxChashesCap_atLimit_accepted() {
        List<String> chashes = IntStream.range(0, TaxonomyRepository.MAX_ASSIGN_FROM_CHASHES)
            .mapToObj(i -> hexChash("afc-cap-at-limit-" + i))
            .toList();
        Map<String, Object> out = repo.assignFromChashes(TENANT_A, COL_A, chashes, false);
        assertThat(out.get("assigned")).as("exactly MAX_ASSIGN_FROM_CHASHES is accepted, not rejected").isEqualTo(0);
        assertThat((List<?>) out.get("unmatched_chashes")).hasSize(TaxonomyRepository.MAX_ASSIGN_FROM_CHASHES);
    }

    @Test
    void maxChashesCap_overLimit_rejected() {
        List<String> chashes = IntStream.range(0, TaxonomyRepository.MAX_ASSIGN_FROM_CHASHES + 1)
            .mapToObj(i -> hexChash("afc-cap-over-limit-" + i))
            .toList();
        assertThatThrownBy(() -> repo.assignFromChashes(TENANT_A, COL_A, chashes, false))
            .as("MAX_ASSIGN_FROM_CHASHES + 1 must be rejected")
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── cross pass ("projection") ───────────────────────────────────────────────

    @Test
    void crossPass_assignsToForeignCollectionTopic_withRealSimilarity() throws Exception {
        // Dedicated TENANT (not just collection — nexus-lns3o review S2): cross_collection
        // matches EVERY OTHER collection FOR THIS TENANT (RLS-scoped), so with a shared
        // PER_CLASS tenant, any OTHER test's "own" collection becomes "foreign" here too
        // and pollutes the argmax. Same isolation technique TaxonomyCentroidRepositoryTest
        // uses for its cross-collection tests (dedicated "tenant-xcoll"/"tenant-foreign").
        String tenant = "afc-tenant-cross1";
        String col = "code__afc_cross1__voyage-code-3__v1";
        String colForeign = "code__afc_cross1fgn__voyage-code-3__v1";
        String c1 = hexChash("afc-chash-cross-1");
        seedChunk(tenant, col, c1, unit(1.0f, 0.0f));
        // No centroids in `col` itself for this chash's scope (own pass -> 0);
        // a foreign collection carries the matching centroid.
        long tForeign = seedTopic(tenant, colForeign, "cross-topic-foreign");
        seedCentroid(tenant, colForeign, tForeign, unit(0.6f, 0.8f));

        Map<String, Object> out = repo.assignFromChashes(tenant, col, List.of(c1), true);
        assertThat(out.get("cross_assigned")).isEqualTo(1);

        List<Map<String, Object>> details = repo.getAssignmentDetails(tenant, List.of(c1));
        assertThat(details).singleElement().satisfies(r -> {
            assertThat(r.get("topic_id")).isEqualTo(tForeign);
            assertThat(r.get("assigned_by")).isEqualTo("projection");
            // cosine(  (1,0), (0.6,0.8) ) = 0.6 exactly (both already near-unit length).
            assertThat((Double) r.get("similarity")).isCloseTo(0.6, within(1e-4));
            assertThat(r.get("source_collection"))
                .as("source_collection is the collection the chash came FROM, not the"
                    + " foreign collection owning the matched topic")
                .isEqualTo(col);
            assertThat(r.get("assigned_at")).isNotNull();
        });
    }

    @Test
    void crossPass_onConflict_greatestSimilarityWins_bothDirections() throws Exception {
        // Dedicated tenant (see crossPass_assignsToForeignCollectionTopic's note) — cross
        // pass sees every foreign collection for the tenant, so an accumulated shared
        // tenant would let another test's centroid win this test's argmax.
        String tenant = "afc-tenant-greatest";
        String col = "code__afc_greatest__voyage-code-3__v1";
        String c1 = hexChash("afc-chash-greatest");
        seedChunk(tenant, col, c1, unit(1.0f, 0.0f));

        // First: a WEAK foreign match (low similarity).
        String weakCol = "code__afc_weak__voyage-code-3__v1";
        long tWeak = seedTopic(tenant, weakCol, "greatest-topic-weak");
        seedCentroid(tenant, weakCol, tWeak, unit(0.1f, 0.995f));
        repo.assignFromChashes(tenant, col, List.of(c1), true);
        double afterWeak = (Double) repo.getAssignmentDetails(tenant, List.of(c1))
            .stream().filter(r -> r.get("topic_id").equals(tWeak)).findFirst().orElseThrow()
            .get("similarity");

        // Manually insert a STRONGER prior projection assignment to a DIFFERENT
        // topic with a similarity higher than anything computable in this fixture,
        // to prove GREATEST retains the existing winner on a lower recompute.
        // The topic must exist for the FK.
        long tStrong = repo.insertTopic(tenant, "afc-greatest-topic-strong", null, weakCol, 0,
            "2026-01-01T00:00:00Z", null);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // RDR-194 P3c: doc_id is bytea now -- decode('hex') the bind parameter.
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.topic_assignments"
                    + " (tenant_id, doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection)"
                    + " VALUES (?, decode(?, 'hex'), ?, 'projection', 0.999, now(), ?)")) {
                ps.setString(1, tenant);
                ps.setString(2, c1);
                ps.setLong(3, tStrong);
                ps.setString(4, col);
                ps.executeUpdate();
            }
        }

        // Re-run the cross pass: tWeak's row must NOT regress below what it already
        // had (GREATEST), and tStrong's manually-seeded 0.999 must survive untouched
        // (this recompute never targets tStrong — different centroid).
        repo.assignFromChashes(tenant, col, List.of(c1), true);
        List<Map<String, Object>> details = repo.getAssignmentDetails(tenant, List.of(c1));
        assertThat(details).anySatisfy(r -> {
            if (r.get("topic_id").equals(tWeak)) {
                assertThat((Double) r.get("similarity"))
                    .as("GREATEST never regresses an existing similarity on a re-run"
                        + " that recomputes the same or a lower value")
                    .isGreaterThanOrEqualTo(afterWeak - 1e-9);
            }
        });
        assertThat(details).anySatisfy(r -> {
            if (r.get("topic_id").equals(tStrong)) {
                assertThat((Double) r.get("similarity"))
                    .as("a higher pre-existing similarity survives an unrelated recompute")
                    .isCloseTo(0.999, within(1e-6));
            }
        });
    }

    @Test
    void crossPass_preRegistersSourceCollection_forSourceCollectionFk() throws Exception {
        String c1 = hexChash("afc-chash-fkreg");
        String freshCol = "code__afc_fkreg__voyage-code-3__v1";
        String colForeign = "code__afc_fkregfgn__voyage-code-3__v1";
        seedChunk(TENANT_A, freshCol, c1, unit(1.0f, 0.0f));
        long tForeign = seedTopic(TENANT_A, colForeign, "fkreg-topic-foreign");
        seedCentroid(TENANT_A, colForeign, tForeign, unit(1.0f, 0.0f));

        // freshCol has never been registered in catalog_collections — the function
        // must self-register it (ensureCollectionRegistered parity) before the
        // projection INSERT, or this throws an FK violation.
        Map<String, Object> out = repo.assignFromChashes(TENANT_A, freshCol, List.of(c1), true);
        assertThat(out.get("cross_assigned")).isEqualTo(1);
    }

    // ── RLS + dim resolution ─────────────────────────────────────────────────────

    @Test
    void foreignTenant_seesNothing_chashReportedUnmatched() throws Exception {
        String col = "code__afc_rls__voyage-code-3__v1";  // dedicated, see above note
        String c1 = hexChash("afc-chash-rls");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        long t = seedTopic(TENANT_A, col, "rls-topic");
        seedCentroid(TENANT_A, col, t, unit(1.0f, 0.0f));

        Map<String, Object> out = repo.assignFromChashes(TENANT_B, col, List.of(c1), false);
        assertThat(out.get("assigned")).as("tenant B cannot see tenant A's chunk row").isEqualTo(0);
        assertThat((List<String>) (List<?>) out.get("unmatched_chashes"))
            .as("RLS-invisible chash reports as unmatched from tenant B's perspective")
            .containsExactly(c1);
    }

    @Test
    void foreignTenant_crossPass_seesNothing_chashReportedUnmatched() throws Exception {
        // Symmetric to foreignTenant_seesNothing_chashReportedUnmatched (code-review-expert
        // LOW): own-pass RLS isolation was pinned but the cross ("projection") pass — the
        // branch that ALSO reads across every other collection FOR THE TENANT — had no
        // isolation coverage of its own. cross_collection=true here must behave identically:
        // tenant B's call sees none of tenant A's chunk row, in EITHER pass.
        String col = "code__afc_rls_cross__voyage-code-3__v1";  // dedicated, see above note
        String c1 = hexChash("afc-chash-rls-cross");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        long t = seedTopic(TENANT_A, col, "rls-cross-topic");
        seedCentroid(TENANT_A, col, t, unit(1.0f, 0.0f));

        Map<String, Object> out = repo.assignFromChashes(TENANT_B, col, List.of(c1), true);
        assertThat(out.get("assigned")).as("tenant B cannot see tenant A's chunk row (own pass)").isEqualTo(0);
        assertThat(out.get("cross_assigned")).as("tenant B cannot see tenant A's chunk row (cross pass)").isEqualTo(0);
        assertThat((List<String>) (List<?>) out.get("unmatched_chashes"))
            .as("RLS-invisible chash reports as unmatched from tenant B's perspective, cross pass included")
            .containsExactly(c1);
    }

    // ── dim parity (384/768/1024) ───────────────────────────────────────────────

    @ParameterizedTest
    @ValueSource(ints = {384, 768, 1024})
    void crossPass_allDims_computesRealSimilarity_endToEnd(int dim) throws Exception {
        // substantive-critic SIGNIFICANT: prior coverage only exercised DIM=1024
        // (voyage-code-3); the 384/768 SQL function bodies (taxonomy-006's
        // assign_from_chashes_384/768) are mechanically duplicated from the 1024
        // body, not shared code — a copy-paste slip isolated to one dim (wrong
        // table name, wrong vector cast, etc.) would be invisible to a 1024-only
        // suite. This replicates crossPass_assignsToForeignCollectionTopic_withRealSimilarity
        // per dim, with the SAME hand-computable unit-vector geometry (cosine((1,0),
        // (0.6,0.8)) = 0.6 exactly), so a per-dim SQL-body bug goes red.
        String model = modelTokenForDim(dim);
        String tenant = "afc-tenant-dimparity-" + dim;
        String col = "code__afc_dp" + dim + "__" + model + "__v1";
        String colForeign = "code__afc_dp" + dim + "fgn__" + model + "__v1";
        String c1 = hexChash("afc-chash-dimparity-" + dim);
        seedChunk(tenant, col, c1, unit(dim, 1.0f, 0.0f), dim);
        long tForeign = seedTopic(tenant, colForeign, "dimparity-topic-" + dim);
        seedCentroid(tenant, colForeign, tForeign, unit(dim, 0.6f, 0.8f), dim);

        Map<String, Object> out = repo.assignFromChashes(tenant, col, List.of(c1), true);
        assertThat(out.get("cross_assigned")).as("dim " + dim + ": cross pass must assign").isEqualTo(1);

        List<Map<String, Object>> details = repo.getAssignmentDetails(tenant, List.of(c1));
        assertThat(details).singleElement().satisfies(r -> {
            assertThat(r.get("topic_id")).as("dim " + dim).isEqualTo(tForeign);
            assertThat(r.get("assigned_by")).as("dim " + dim).isEqualTo("projection");
            assertThat((Double) r.get("similarity")).as("dim " + dim
                + ": cosine((1,0),(0.6,0.8)) = 0.6 exactly").isCloseTo(0.6, within(1e-4));
            assertThat(r.get("source_collection")).as("dim " + dim).isEqualTo(col);
        });
    }

    @ParameterizedTest
    @ValueSource(ints = {384, 768, 1024})
    void ownPass_allDims_persistsSourceCollectionOnCentroidBranch(int dim) throws Exception {
        // RDR-194 P3a (nexus-tk070.p3a), sequential-thinking-planned falsification:
        // the centroid (own-pass, cross_collection=false) branch of
        // assign_from_chashes_{384,768,1024} must persist source_collection on ALL
        // THREE dims, not just the projection branch. Falsify by removing
        // "source_collection" from taxonomy-009's centroid-branch INSERT column
        // list (and its SELECT value) for any one dim: this test goes red for
        // exactly that dim, proving per-dim coverage rather than a single
        // DIM=1024 assertion that a copy-paste miss on 384/768 could hide.
        String model = modelTokenForDim(dim);
        String tenant = "afc-tenant-srccoll-" + dim;
        String col = "code__afc_sc" + dim + "__" + model + "__v1";
        String c1 = hexChash("afc-chash-srccoll-" + dim);
        seedChunk(tenant, col, c1, unit(dim, 1.0f, 0.0f), dim);
        long t = seedTopic(tenant, col, "srccoll-topic-" + dim);
        seedCentroid(tenant, col, t, unit(dim, 1.0f, 0.0f), dim);

        Map<String, Object> out = repo.assignFromChashes(tenant, col, List.of(c1), false);
        assertThat(out.get("assigned")).as("dim " + dim).isEqualTo(1);

        List<Map<String, Object>> details = repo.getAssignmentDetails(tenant, List.of(c1));
        assertThat(details).singleElement().satisfies(r -> {
            assertThat(r.get("topic_id")).as("dim " + dim).isEqualTo(t);
            assertThat(r.get("assigned_by")).as("dim " + dim).isEqualTo("centroid");
            assertThat(r.get("source_collection")).as("dim " + dim + ": centroid branch must"
                + " persist source_collection = p_collection (RDR-194 D1 prerequisite for"
                + " the composite FK; a NULL here escapes it through MATCH SIMPLE)")
                .isEqualTo(col);
        });
    }

    private static String modelTokenForDim(int dim) {
        return switch (dim) {
            case 384  -> "minilm-l6-v2-384";
            case 768  -> "bge-base-en-v15-768";
            case 1024 -> "voyage-code-3";
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    @Test
    void nonConformantCollectionName_failsLoud() {
        assertThatThrownBy(() -> repo.assignFromChashes(TENANT_A, "not-conformant", List.of("x"), false))
            .as("dimForCollection requires a 4-segment conformant name — fail loud, no silent dimension")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void emptyChashList_isNoOp() {
        Map<String, Object> out = repo.assignFromChashes(TENANT_A, COL_A, List.of(), true);
        assertThat(out.get("assigned")).isEqualTo(0);
        assertThat(out.get("cross_assigned")).isEqualTo(0);
        assertThat((List<?>) out.get("unmatched_chashes")).isEmpty();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────

    private static float[] unit(float x, float y) {
        return unit(DIM, x, y);
    }

    /** Dim-parametrized variant (nexus-lns3o review: 384/768 dim-parity coverage) —
     * same unit-vector idiom, sized for whichever chunks_&lt;dim&gt;/taxonomy_centroids_&lt;dim&gt;
     * table the caller is targeting. */
    private static float[] unit(int dim, float x, float y) {
        float[] v = new float[dim];
        v[0] = x;
        v[1] = y;
        return v;
    }

    /** Deterministic 64-lowercase-hex chash from a readable seed (RDR-180: chunks_&lt;dim&gt;.chash
     * is bytea; the route/route-adjacent Java layer carries hex, so every chash value used in
     * these fixtures — including deliberately-never-upserted "ghost" chashes — must be
     * syntactically valid hex, exactly like a real sha256 content hash would be). */
    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private void seedChunk(String tenant, String collection, String hexChashValue, float[] emb) throws Exception {
        seedChunk(tenant, collection, hexChashValue, emb, DIM);
    }

    /** Dim-parametrized variant (nexus-lns3o review: 384/768 dim-parity coverage).
     * RDR-191 unify: targets the unified nexus.chunks table, dim expressed via
     * the correct embedding_&lt;dim&gt; column (the other two stay NULL, satisfying
     * the exactly_one_embedding CHECK) rather than a separate chunks_&lt;dim&gt; table. */
    private void seedChunk(String tenant, String collection, String hexChashValue, float[] emb, int dim) throws Exception {
        registerCollection(tenant, collection);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks"
                    + " (tenant_id, collection, chash, chunk_text, embedding_" + dim + ")"
                    + " VALUES (?, ?, decode(?, 'hex'), ?, ?::vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setString(3, hexChashValue);
                ps.setString(4, "seed text " + hexChashValue);
                ps.setString(5, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
    }

    /** Pre-register every fixture collection before seeding chunk rows (idempotent).
     * The three source chunks_&lt;dim&gt; tables carried a validated FK to
     * catalog_collections(tenant_id, name); the unified nexus.chunks ships with
     * ZERO collection-FK enforcement until RDR-191 Phase 5 (vectors-004-unify-chunks.xml
     * header, "NO COLLECTION FK ON nexus.chunks UNTIL PHASE 5") — this call is now
     * belt-and-suspenders rather than FK-required, kept for parity with the SVC_ROLE
     * grant on catalog_collections and to mirror production's registration path. */
    private void registerCollection(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?)"
                    + " ON CONFLICT (tenant_id, name) DO NOTHING")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.executeUpdate();
            }
        }
    }

    /** topic_assignments.topic_id has a real FK to topics(id) — a centroid's topic_id must
     * name an actual row in nexus.topics (production creates both together at discover
     * time). Returns the generated id. */
    private long seedTopic(String tenant, String collection, String label) {
        return repo.insertTopic(tenant, label, null, collection, 0, "2026-01-01T00:00:00Z", null);
    }

    private void seedCentroid(String tenant, String collection, long topicId, float[] emb) throws Exception {
        seedCentroid(tenant, collection, topicId, emb, DIM);
    }

    /** Dim-parametrized variant (nexus-lns3o review: 384/768 dim-parity coverage).
     * RDR-191 unify: targets the unified nexus.taxonomy_centroids table, dim
     * expressed via the correct embedding_&lt;dim&gt; column. */
    private void seedCentroid(String tenant, String collection, long topicId, float[] emb, int dim) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.taxonomy_centroids"
                    + " (tenant_id, collection, topic_id, embedding_" + dim + ") VALUES (?, ?, ?, ?::vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setLong(3, topicId);
                ps.setString(4, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
    }

    private static String vectorLiteral(float[] vec) {
        StringBuilder sb = new StringBuilder(vec.length * 8 + 2).append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }
}
