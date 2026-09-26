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
 * production actually runs.
 *
 * <p>Same technique as {@code TaxonomyAssignCrossLateralHnswTest}'s prosrc
 * drift check (fad445ff3, nexus-swam7): a single hand-maintained text
 * constant per dim (this class's own source, not reverse-engineered from
 * either function), whitespace-normalized, asserted to be a SUBSTRING of
 * BOTH functions' real {@code pg_proc.prosrc}. If either function's shared
 * span ever changes without the other, exactly one of the two assertions
 * below fails, naming which side moved.
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
}
