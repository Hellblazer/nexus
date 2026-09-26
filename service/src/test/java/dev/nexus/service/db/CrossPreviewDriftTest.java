// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-v4pj4 (round-2 review decision): {@code nexus.cross_preview_<dim>}
 * (taxonomy-021) is a READ-ONLY twin of {@code nexus.assign_from_chashes_
 * <dim>}'s cross branch (taxonomy-018/020) — its {@code batch}/{@code
 * nearest} CTE, the actual nearest-foreign-centroid computation, is meant to
 * be BYTE-IDENTICAL between the two functions per dim, so the preview the
 * doctor check compares against can never silently drift from what
 * production actually runs. The four {@code set_config} planner/HNSW pins
 * that PRECEDE that CTE in both functions' bodies (taxonomy-018's
 * iterative-scan/ef_search pair, taxonomy-020's enable_seqscan/enable_sort
 * pair) are checked the same way, separately — round-2 review (critic,
 * Critical) found the original version of this class covered only the CTE,
 * leaving a one-sided retune of either function's settings block invisible
 * to every assertion here.
 *
 * <p>Same technique as {@code TaxonomyAssignCrossLateralHnswTest}'s prosrc
 * drift check (fad445ff3, nexus-swam7): hand-maintained text constants
 * (this class's own source, not reverse-engineered from either function),
 * whitespace-normalized, asserted to be a SUBSTRING of BOTH functions' real
 * {@code pg_proc.prosrc}. If either function's shared CTE or settings block
 * ever changes without the other, one of the assertions below fails,
 * naming which side moved.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CrossPreviewDriftTest {

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    /** Collapse all whitespace runs to a single space and trim — mirrors
     *  {@code TaxonomyAssignCrossLateralHnswTest.normalizeWhitespace}. */
    private static String normalizeWhitespace(String s) {
        return s.replaceAll("\\s+", " ").trim();
    }

    /**
     * The {@code batch}/{@code nearest} CTE text, hand-maintained here, in the
     * EXACT form both {@code assign_from_chashes_<dim>}'s cross branch and
     * {@code cross_preview_<dim>} carry verbatim (taxonomy-020/taxonomy-021's
     * own changelog SQL — see either file's header). Ends right at the
     * {@code nearest} CTE's own closing paren, deliberately BEFORE the comma
     * that (only in {@code assign_from_chashes_<dim>}) introduces the
     * {@code persisted} INSERT CTE — {@code cross_preview_<dim>} has no such
     * CTE at all, so the shared span must stop exactly where the two bodies
     * diverge, not one token later.
     */
    private static String sharedNearestCteText(int dim) {
        return "WITH batch AS ("
            + "    SELECT c.chash AS b_chash, c.embedding_" + dim + " AS b_emb"
            + "      FROM nexus.chunks c"
            + "     WHERE c.collection = p_collection"
            + "       AND c.embedding_" + dim + " IS NOT NULL"
            + "       AND c.chash = ANY(ARRAY(SELECT decode(x, 'hex') FROM unnest(p_chashes) x))"
            + " ),"
            + " nearest AS ("
            + "     SELECT encode(b.b_chash, 'hex')                     AS m_chash,"
            + "            b.b_chash                                    AS m_chash_bytes,"
            + "            n.n_topic_id                                 AS m_topic_id,"
            + "            (1 - n.n_dist)::double precision              AS m_sim"
            + "       FROM batch b"
            + "       CROSS JOIN LATERAL ("
            + "           SELECT ct.topic_id AS n_topic_id,"
            + "                  (ct.embedding_" + dim + " OPERATOR(nexus.<=>) b.b_emb) AS n_dist"
            + "             FROM nexus.taxonomy_centroids ct"
            + "            WHERE ct.collection <> p_collection"
            + "              AND ct.embedding_" + dim + " IS NOT NULL"
            + "            ORDER BY ct.embedding_" + dim + " OPERATOR(nexus.<=>) b.b_emb, ct.topic_id ASC"
            + "            LIMIT 1"
            + "       ) n"
            + " )";
    }

    /**
     * The four transaction-local {@code set_config} pins (taxonomy-018's
     * iterative-scan/ef_search pair, taxonomy-020's enable_seqscan/
     * enable_sort pair, nexus-swam7) that make the cross branch's LATERAL
     * take the same plan production runs. Dim-independent — none of the
     * four lines names a dimension — so ONE list covers all three dims,
     * unlike {@link #sharedNearestCteText(int)}.
     *
     * <p>Round-2 review (critic, Critical): the drift check above starts
     * at {@code "WITH batch AS ("}, AFTER these four lines, so a
     * one-sided retune of either function's settings block (e.g.
     * {@code cross_preview_<dim>}'s {@code ef_search} silently left at an
     * old value while {@code assign_from_chashes_<dim>}'s moved, or vice
     * versa) passed every existing assertion. This closes that gap by
     * asserting the settings block itself, not just the CTE that follows
     * it, is present in both functions.
     *
     * <p>Checked as FOUR SEPARATE substrings, each its own {@code
     * .contains(...)} — same technique as {@code
     * TaxonomyAssignCrossLateralHnswTest.
     * assignFromChashesFunctions_carryTheLoadBearingHnswSettings}, and for
     * the same reason: {@code assign_from_chashes_<dim>}'s real body
     * carries multi-line explanatory comments BETWEEN these statements
     * (taxonomy-018/020's own header commentary), so the four lines are
     * NOT byte-contiguous there even though {@code cross_preview_<dim>}
     * (no such comments) carries them back to back. A single contiguous
     * 4-line span would therefore never match {@code assign_from_chashes}
     * at all — discovered live: the first version of this fix asserted
     * one blob and failed against BOTH functions, not just a mutated one.
     */
    private static List<String> sharedHnswSettingsLines() {
        return List.of(
            "set_config('hnsw.iterative_scan', 'strict_order', true)",
            "set_config('hnsw.ef_search', '400', true)",
            "set_config('enable_seqscan', 'off', true)",
            "set_config('enable_sort', 'off', true)");
    }

    private String prosrc(String functionName) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "SELECT prosrc FROM pg_catalog.pg_proc"
                    + " WHERE proname = ? AND pronamespace = 'nexus'::regnamespace")) {
                ps.setString(1, functionName);
                try (var rs = ps.executeQuery()) {
                    assertThat(rs.next()).as(functionName + " must exist").isTrue();
                    return rs.getString(1);
                }
            }
        }
    }

    @Test
    void sharedNearestCte_isIdenticalBetweenAssignFromChashesAndCrossPreview_384() throws Exception {
        assertSharedSpanPresentInBoth(384);
    }

    @Test
    void sharedNearestCte_isIdenticalBetweenAssignFromChashesAndCrossPreview_768() throws Exception {
        assertSharedSpanPresentInBoth(768);
    }

    @Test
    void sharedNearestCte_isIdenticalBetweenAssignFromChashesAndCrossPreview_1024() throws Exception {
        assertSharedSpanPresentInBoth(1024);
    }

    private void assertSharedSpanPresentInBoth(int dim) throws Exception {
        String shared = normalizeWhitespace(sharedNearestCteText(dim));
        String assignProsrc = normalizeWhitespace(prosrc("assign_from_chashes_" + dim));
        String previewProsrc = normalizeWhitespace(prosrc("cross_preview_" + dim));

        assertThat(assignProsrc)
            .as("assign_from_chashes_%d's cross branch must still carry this exact"
                + " batch/nearest CTE text — if this fails, taxonomy-018/020's function"
                + " changed and cross_preview_%d (taxonomy-021) has silently drifted out"
                + " of sync with what production actually runs. Shared text was:%n%s",
                dim, dim, shared)
            .contains(shared);
        assertThat(previewProsrc)
            .as("cross_preview_%d must carry this exact batch/nearest CTE text — if this"
                + " fails, taxonomy-021's function changed and no longer matches"
                + " assign_from_chashes_%d's cross branch, so the doctor check's"
                + " same-moment comparison would silently stop meaning what it claims to."
                + " Shared text was:%n%s",
                dim, dim, shared)
            .contains(shared);

        assertSharedHnswSettingsPresentInBoth(dim, assignProsrc, previewProsrc);
    }

    /**
     * Round-2 review (critic, Critical): the settings block itself — not
     * just the CTE that follows it — must be byte-identical between
     * {@code assign_from_chashes_<dim>} and {@code cross_preview_<dim>}.
     * A one-sided retune of either function's {@code hnsw.ef_search} (or
     * any of the other three pins) makes the preview take a DIFFERENT
     * plan than production, which silently invalidates the doctor
     * check's same-moment comparison without touching the CTE text at
     * all — the exact gap the pre-fix version of this class left open.
     */
    private void assertSharedHnswSettingsPresentInBoth(
            int dim, String assignProsrc, String previewProsrc) {
        for (String line : sharedHnswSettingsLines()) {
            assertThat(assignProsrc)
                .as("assign_from_chashes_%d must still carry `%s` — if this fails,"
                    + " taxonomy-018/020's pins changed and cross_preview_%d (taxonomy-021)"
                    + " may now run under a DIFFERENT plan than production, even if the CTE"
                    + " text above still matches.",
                    dim, line, dim)
                .contains(line);
            assertThat(previewProsrc)
                .as("cross_preview_%d must carry `%s` — if this fails, taxonomy-021's pins"
                    + " no longer match assign_from_chashes_%d's, so the preview the doctor"
                    + " check compares against would silently stop taking the same plan"
                    + " production takes.",
                    dim, line, dim)
                .contains(line);
        }
    }

    /**
     * Non-vacuity (mirrors the house convention every prosrc-substring test in
     * this codebase carries): prove the shared-span constant is not simply
     * something both prosrc values happen to contain by coincidence — a
     * deliberately WRONG span (a token that exists in neither function) must
     * NOT be found.
     */
    @Test
    void nonVacuity_aWrongSpanIsNotFoundInEitherFunction() throws Exception {
        String bogus = normalizeWhitespace("SELECT 'nexus-v4pj4-drift-test-canary-should-never-match'");
        String assignProsrc = normalizeWhitespace(prosrc("assign_from_chashes_1024"));
        String previewProsrc = normalizeWhitespace(prosrc("cross_preview_1024"));
        assertThat(assignProsrc).doesNotContain(bogus);
        assertThat(previewProsrc).doesNotContain(bogus);
    }

    /**
     * Round-2 review (critic, Critical) asked for a demonstration that the
     * settings-block assertion is not vacuous — that it would actually
     * fail if one side's {@code ef_search} value moved out from under the
     * other. A live scratch edit + revert against the real database
     * proved that (see the round-2 fix commit's report: retuning
     * {@code cross_preview_1024}'s pin from {@code '400'} to {@code
     * '399'} made {@code assertSharedHnswSettingsPresentInBoth(1024, ...)}
     * fail naming exactly that function, then reverting restored green).
     * This permanent test pins the SAME failure mode mechanically: the
     * off-by-one-value line must not be found in either function's real
     * prosrc, proving both real values are exactly {@code '400'} and not
     * merely "some string containing 400".
     */
    @Test
    void nonVacuity_aOneSidedEfSearchRetuneWouldNotBeFoundInEitherFunction() throws Exception {
        String retunedLine = "set_config('hnsw.ef_search', '399', true)";
        String assignProsrc = normalizeWhitespace(prosrc("assign_from_chashes_1024"));
        String previewProsrc = normalizeWhitespace(prosrc("cross_preview_1024"));
        assertThat(assignProsrc).doesNotContain(retunedLine);
        assertThat(previewProsrc).doesNotContain(retunedLine);
    }
}
