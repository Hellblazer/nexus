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
 * <p>The CTE check uses the same technique as {@code
 * TaxonomyAssignCrossLateralHnswTest}'s prosrc drift check (fad445ff3,
 * nexus-swam7): a hand-maintained text constant (this class's own source,
 * not reverse-engineered from either function), whitespace-normalized,
 * asserted to be a SUBSTRING of BOTH functions' real {@code pg_proc.prosrc}.
 * The settings-block check is EXHAUSTIVE rather than substring-based
 * (round-3 review, critic Significant: a per-line {@code .contains(...)}
 * proves presence only, not exclusivity/precedence/reachability) — see
 * {@link #assertSharedHnswSettingsPresentInBoth} and {@link
 * HnswSettingsExtractor}, shared with {@code
 * TaxonomyAssignCrossLateralHnswTest} so the two checks cannot drift from
 * each other either. If either function's shared CTE or settings block
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
        String assignProsrcRaw = prosrc("assign_from_chashes_" + dim);
        String previewProsrcRaw = prosrc("cross_preview_" + dim);
        String shared = normalizeWhitespace(sharedNearestCteText(dim));
        String assignProsrc = normalizeWhitespace(assignProsrcRaw);
        String previewProsrc = normalizeWhitespace(previewProsrcRaw);

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

        assertSharedHnswSettingsPresentInBoth(dim, assignProsrcRaw, previewProsrcRaw);
    }

    /**
     * Round-3 review (critic, Significant): the round-2 fix checked each of
     * the four {@code set_config} lines as its own {@code .contains(...)}
     * substring — presence only, never exclusivity (a fifth setting),
     * precedence (a LATER call silently overriding an earlier one's
     * effective value), or reachability (the pin sitting only inside a
     * comment, never actually executed). All three would have passed the
     * round-2 version of this method unchanged.
     *
     * <p>This uses {@link HnswSettingsExtractor} (shared with {@code
     * TaxonomyAssignCrossLateralHnswTest} so the two drift checks cannot
     * silently diverge from each other either) to strip SQL comments, then
     * reduce every {@code set_config} call to the LAST-OCCURRENCE-WINS
     * map Postgres itself would apply, plus the total call count. Passed
     * the RAW prosrc (real newlines) — a whitespace-normalized string has
     * no newline left to stop a {@code --} line comment.
     */
    private void assertSharedHnswSettingsPresentInBoth(
            int dim, String assignProsrcRaw, String previewProsrcRaw) {
        HnswSettingsExtractor.Extraction assignX = HnswSettingsExtractor.extract(assignProsrcRaw);
        HnswSettingsExtractor.Extraction previewX = HnswSettingsExtractor.extract(previewProsrcRaw);

        assertThat(assignX.effective())
            .as("assign_from_chashes_%d's EFFECTIVE (last-occurrence-wins, comments stripped)"
                + " hnsw/planner settings must be EXACTLY the four expected pins — if this"
                + " fails, taxonomy-018/020's pins changed and cross_preview_%d (taxonomy-021)"
                + " may now run under a DIFFERENT plan than production, even if the CTE text"
                + " above still matches. Extracted: %s", dim, dim, assignX.effective())
            .isEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS);
        assertThat(previewX.effective())
            .as("cross_preview_%d's EFFECTIVE (last-occurrence-wins, comments stripped)"
                + " hnsw/planner settings must be EXACTLY the four expected pins — if this"
                + " fails, taxonomy-021's pins no longer match assign_from_chashes_%d's, so the"
                + " preview the doctor check compares against would silently stop taking the"
                + " same plan production takes. Extracted: %s", dim, dim, previewX.effective())
            .isEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS);
        assertThat(previewX.calls())
            .as("cross_preview_%d must issue the SAME NUMBER of set_config calls as"
                + " assign_from_chashes_%d (%d vs %d) — a stray duplicate call repeating an"
                + " EXISTING setting name/value would be invisible to the effective-map"
                + " comparison above alone, since last-occurrence-wins collapses it silently.",
                dim, dim, previewX.calls().size(), assignX.calls().size())
            .hasSameSizeAs(assignX.calls());
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
     * Round-2 review proved the (now-superseded) per-line settings check was
     * not vacuous by a live scratch edit against the real database:
     * retuning {@code cross_preview_1024}'s {@code ef_search} pin from
     * {@code '400'} to {@code '399'} failed the assertion naming exactly
     * that function, then reverting restored green. Round-3 replaced that
     * per-line check with {@link #assertSharedHnswSettingsPresentInBoth}'s
     * exhaustive {@link HnswSettingsExtractor} comparison; the extractor's
     * own vacuity — that it actually catches an extra setting, a later
     * override, or a comment-only pin — is proven by pure-string tests in
     * {@code HnswSettingsExtractorTest} against the helper directly, which
     * need no database and run far faster than a live scratch edit.
     */
    @Test
    void assignFromChashesAndCrossPreview_1024_extractToExactlyTheExpectedSettings() throws Exception {
        HnswSettingsExtractor.Extraction assignX =
            HnswSettingsExtractor.extract(prosrc("assign_from_chashes_1024"));
        HnswSettingsExtractor.Extraction previewX =
            HnswSettingsExtractor.extract(prosrc("cross_preview_1024"));
        assertThat(assignX.effective()).isEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS);
        assertThat(previewX.effective()).isEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS);
        assertThat(assignX.calls()).hasSize(4);
        assertThat(previewX.calls()).hasSize(4);
    }
}
