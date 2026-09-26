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
        // Round-5 review (code review, Significant): a NEWLINE between the
        // confounding literal and the real call would make this pass under
        // the OLD line-bounded regex too (`--[^\n]*` already stops at that
        // newline on its own), proving nothing about quote-awareness. Both
        // must sit on the SAME LINE so the OLD regex's `--` match, which
        // starts INSIDE the string, actually reaches -- and swallows -- the
        // real call before hitting a newline.
        String sameLine =
            "RAISE EXCEPTION 'not registered -- see docs'; PERFORM set_config('hnsw.ef_search', '400', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(sameLine);
        assertThat(x.effective())
            .as("the set_config call after the quoted '--', on the SAME line, must still be"
                + " found -- a quote-UNAWARE stripper's `--[^\\n]*` would start matching"
                + " INSIDE the string literal and swallow everything to end of line,"
                + " including this real call")
            .containsEntry("hnsw.ef_search", "400")
            .hasSize(1);
    }

    @Test
    void quoteAware_aSlashStarInsideAStringLiteralDoesNotSwallowTheRestOfTheLine() {
        // Round-5 review (code review, Significant): the string's `/*` must
        // have NO closing `*/` before the real call, so the OLD DOTALL
        // block-comment regex's reluctant match -- which starts at THIS
        // `/*` -- is forced to keep scanning past the string, past the real
        // call, to the FIRST `*/` it finds anywhere, which is the stray one
        // placed deliberately AFTER the call. A `/* ... */` fully closed
        // inside the string (the round-4 version) lets the old regex's
        // match end there too, proving nothing.
        String sameLine =
            "RAISE EXCEPTION 'malformed /* input'; PERFORM set_config('enable_sort', 'off', true); */\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(sameLine);
        assertThat(x.effective())
            .as("a `/*` inside a string literal, with no closing `*/` until AFTER the real"
                + " call, must not be treated as a block-comment opener -- a quote-UNAWARE"
                + " DOTALL regex would match from this `/*` all the way to the stray `*/`"
                + " after the call, swallowing the entire real call in between")
            .containsEntry("enable_sort", "off")
            .hasSize(1);
    }

    @Test
    void quoteAware_aDoubledQuoteEscapeInsideAStringDoesNotEndTheStringEarly() {
        // Postgres's '' doubled-quote escape represents a literal ' inside
        // a string. Same-line, same reasoning as the `--` test above: this
        // exercises escape-awareness AND the OLD-regex discrimination
        // together, since the `--` after the escaped quote is still inside
        // the (longer) real string and must not be treated as a comment
        // opener, on either scanner.
        String withEscapedQuote =
            "RAISE EXCEPTION 'it''s not registered -- see docs'; PERFORM set_config('hnsw.iterative_scan', 'strict_order', true);\n";
        HnswSettingsExtractor.Extraction x = HnswSettingsExtractor.extract(withEscapedQuote);
        assertThat(x.effective())
            .containsEntry("hnsw.iterative_scan", "strict_order")
            .hasSize(1);
    }

    @Test
    void quoteAware_theRealAssignFromChashesRaiseMessageDoesNotCorruptExtraction() {
        // The EXACT shape of assign_from_chashes' own message (taxonomy-018's
        // header names it explicitly), verbatim, immediately followed by
        // the real settings block. NOT the discriminating test (round-5
        // review, code review, Significant): production's own message ends
        // its own line before the settings block starts on later lines, so
        // even the OLD line-bounded regex extracts this correctly too --
        // the two same-line tests above are what actually distinguish this
        // scanner from a plain regex strip. This one stays as a REGRESSION
        // PIN against production's real literal text specifically.
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
