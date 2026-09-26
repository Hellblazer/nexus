// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Pure-string unit tests for {@link HnswSettingsExtractor} (round-3
 * review, critic Significant, nexus-v4pj4). No database, no container --
 * these prove the extractor itself catches the three failure modes a
 * plain {@code prosrc.contains("set_config(...)")} substring check
 * cannot: an extra setting (exclusivity), a later call silently
 * overriding an earlier one's effective value (precedence), and the
 * expected pin sitting only inside a comment, never actually executed
 * (reachability).
 */
class HnswSettingsExtractorTest {

    private static final String CANONICAL_FOUR_LINES =
        "PERFORM set_config('hnsw.iterative_scan', 'strict_order', true);\n"
        + "PERFORM set_config('hnsw.ef_search', '400', true);\n"
        + "PERFORM set_config('enable_seqscan', 'off', true);\n"
        + "PERFORM set_config('enable_sort', 'off', true);\n";

    @Test
    void positiveControl_theCanonicalFourLinesExtractExactlyToTheExpectedMap() {
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(CANONICAL_FOUR_LINES);
        assertThat(x.effective()).isEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS);
        assertThat(x.calls()).hasSize(4);
    }

    @Test
    void exclusivity_aFifthSettingIsCaught() {
        String withFifth = CANONICAL_FOUR_LINES
            + "PERFORM set_config('work_mem', '64MB', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(withFifth);
        assertThat(x.effective())
            .as("a fifth, unexpected setting must make the effective map NOT equal to the"
                + " expected four -- a substring check for the four expected pins would still"
                + " pass unchanged with a fifth setting present")
            .isNotEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS)
            .containsEntry("work_mem", "64MB")
            .hasSize(5);
        assertThat(x.calls()).hasSize(5);
    }

    @Test
    void exclusivity_aFifthCallRepeatingAnExistingNameIsCaughtByCountAlone() {
        // A duplicate call for a name/value ALREADY in the canonical four:
        // the effective map is UNCHANGED (still exactly the expected four),
        // so only the CALL COUNT (5, not 4) exposes the stray extra call --
        // exactly the case an effective-map-only comparison would miss.
        String withDuplicate = CANONICAL_FOUR_LINES
            + "PERFORM set_config('enable_sort', 'off', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(withDuplicate);
        assertThat(x.effective()).isEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS);
        assertThat(x.calls())
            .as("the effective map alone is blind to this stray duplicate call; only the"
                + " total call count catches it")
            .hasSize(5);
    }

    @Test
    void precedence_aLaterOverrideOfEfSearchIsCaught() {
        String withOverride = CANONICAL_FOUR_LINES
            + "PERFORM set_config('hnsw.ef_search', '399', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(withOverride);
        assertThat(x.effective())
            .as("a substring check for \"set_config('hnsw.ef_search', '400', true)\" would"
                + " still find that literal text earlier in the source and pass -- but the"
                + " EFFECTIVE value Postgres actually applies is the LATER call's '399', which"
                + " the extractor's last-occurrence-wins reduction must surface")
            .containsEntry("hnsw.ef_search", "399")
            .isNotEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS);
    }

    @Test
    void reachability_aPinPresentOnlyInsideACommentIsNotCounted() {
        String commentOnly =
            "PERFORM set_config('hnsw.iterative_scan', 'strict_order', true);\n"
            + "PERFORM set_config('hnsw.ef_search', '400', true);\n"
            + "-- PERFORM set_config('enable_seqscan', 'off', true);\n"
            + "PERFORM set_config('enable_sort', 'off', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(commentOnly);
        assertThat(x.effective())
            .as("a substring check against RAW prosrc would find 'enable_seqscan', 'off'"
                + " sitting inside the comment and wrongly conclude the pin is set -- the"
                + " extractor strips comments first, so a pin that only ever appears inside"
                + " one must be ABSENT from the effective map")
            .doesNotContainKey("enable_seqscan")
            .isNotEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS)
            .hasSize(3);
    }

    @Test
    void reachability_aPinInsideABlockCommentIsAlsoNotCounted() {
        String blockCommentOnly =
            "PERFORM set_config('hnsw.iterative_scan', 'strict_order', true);\n"
            + "PERFORM set_config('hnsw.ef_search', '400', true);\n"
            + "/* PERFORM set_config('enable_seqscan', 'off', true); */\n"
            + "PERFORM set_config('enable_sort', 'off', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(blockCommentOnly);
        assertThat(x.effective()).doesNotContainKey("enable_seqscan").hasSize(3);
    }

    @Test
    void nonVacuity_extractionOfAnEmptyStringIsEmpty() {
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract("");
        assertThat(x.effective()).isEmpty();
        assertThat(x.calls()).isEmpty();
    }

    // ════════════════════════════════════════════════════════════════════════
    // Round-4 review (critic, Significant): the comment stripper must be
    // QUOTE-AWARE. A plain regex strip of `--`/block comments also strips
    // a `--` or `/*` sitting inside an ordinary single-quoted string
    // literal -- assign_from_chashes' own RAISE EXCEPTION message
    // ('... %% -- register it first via ...', 12 occurrences across
    // taxonomy-018/020) is exactly such a literal, harmless today only
    // because no real set_config pin shares that literal's line.
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void quoteAware_aDashDashInsideAStringLiteralDoesNotSwallowTheRestOfTheLine() {
        // The naive (pre-round-4) regex strip would see the `--` inside
        // the quoted message and treat EVERYTHING from there to end of
        // line as a comment -- including the real set_config call that
        // follows it on the SAME line.
        String sameLine =
            "RAISE EXCEPTION 'collection %% is not registered -- register it first';\n"
            + "PERFORM set_config('hnsw.ef_search', '400', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(sameLine);
        assertThat(x.effective())
            .as("the set_config call after the quoted '--' must still be found -- a"
                + " quote-UNAWARE stripper would have swallowed it as part of a fake"
                + " comment starting inside the string literal")
            .containsEntry("hnsw.ef_search", "400")
            .hasSize(1);
    }

    @Test
    void quoteAware_aSlashStarInsideAStringLiteralDoesNotSwallowTheRestOfTheLine() {
        String sameLine =
            "RAISE EXCEPTION 'malformed /* input */ detected here';\n"
            + "PERFORM set_config('enable_sort', 'off', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(sameLine);
        assertThat(x.effective())
            .as("a `/*` inside a string literal must not be treated as a block-comment"
                + " opener -- the real set_config call after the string must still be found")
            .containsEntry("enable_sort", "off")
            .hasSize(1);
    }

    @Test
    void quoteAware_aDoubledQuoteEscapeInsideAStringDoesNotEndTheStringEarly() {
        // Postgres's '' doubled-quote escape represents a literal ' inside
        // a string. A scanner that ends the string at the FIRST quote
        // would then misparse everything after it, including a `--` that
        // is actually still inside the (longer) real string.
        String withEscapedQuote =
            "RAISE EXCEPTION 'it''s not registered -- register it first';\n"
            + "PERFORM set_config('hnsw.iterative_scan', 'strict_order', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(withEscapedQuote);
        assertThat(x.effective())
            .containsEntry("hnsw.iterative_scan", "strict_order")
            .hasSize(1);
    }

    @Test
    void quoteAware_theRealAssignFromChashesRaiseMessageDoesNotCorruptExtraction() {
        // The EXACT shape of assign_from_chashes' own message (taxonomy-018's
        // header names it explicitly), verbatim, immediately followed by
        // the real settings block -- reproduces the round-4 finding
        // directly against production's own literal text.
        String realShape =
            "IF NOT EXISTS (SELECT 1 FROM nexus.catalog_collections) THEN\n"
            + "    RAISE EXCEPTION 'assign_from_chashes_1024: collection %% is not"
            + " registered for tenant %% -- register it first via POST"
            + " /v1/catalog/collections/upsert', p_collection, current_setting('nexus.tenant', true)"
            + " USING ERRCODE = 'foreign_key_violation';\n"
            + "END IF;\n"
            + "PERFORM set_config('hnsw.iterative_scan', 'strict_order', true);\n"
            + "PERFORM set_config('hnsw.ef_search', '400', true);\n"
            + "PERFORM set_config('enable_seqscan', 'off', true);\n"
            + "PERFORM set_config('enable_sort', 'off', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(realShape);
        assertThat(x.effective()).isEqualTo(HnswSettingsExtractor.EXPECTED_HNSW_SETTINGS);
        assertThat(x.calls()).hasSize(4);
    }
}
