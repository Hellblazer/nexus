// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Shared prosrc {@code set_config(...)} extractor (round-3 review, critic
 * Significant, nexus-v4pj4): {@code CrossPreviewDriftTest}'s and {@code
 * TaxonomyAssignCrossLateralHnswTest}'s per-line {@code .contains(...)}
 * checks prove PRESENCE only -- not exclusivity, precedence, or
 * reachability. A fifth setting, a LATER call that silently overrides an
 * earlier one's effective value, or the expected pin sitting only inside a
 * SQL comment (never actually executed) would all still pass a substring
 * check unchanged.
 *
 * <p>This extractor strips SQL comments first ({@code --} to end of line,
 * plus slash-star/star-slash block comments), then regex-matches every
 * {@code set_config('<name>', '<value>', true)} call IN SOURCE ORDER, and reduces
 * them to the LAST-OCCURRENCE-WINS map Postgres itself would apply at
 * runtime (a later {@code set_config} call for the same name overrides an
 * earlier one within the same transaction), plus the total call COUNT. A
 * caller can therefore assert both "this is the value that actually takes
 * effect" (exclusivity + precedence) and "no stray extra or missing call
 * site" (reachability), none of which a substring check can distinguish.
 */
public final class HnswSettingsExtractor {

    private HnswSettingsExtractor() {}

    /** One extracted {@code set_config} call, in source order. */
    public record Call(String name, String value) {}

    /**
     * {@code effective} is the LAST-OCCURRENCE-WINS map (Postgres's own
     * runtime semantics for repeated {@code set_config} calls on the same
     * name); {@code calls} is every call found, in source order -- its
     * size is the "total call count" a caller compares between two
     * functions to catch a duplicate or stray extra call that an
     * effective-map comparison alone could miss (a duplicate call
     * repeating an EXISTING name/value changes the count but not the
     * map).
     */
    public record Extraction(Map<String, String> effective, List<Call> calls) {}

    /**
     * The four pins this bead's engine functions carry (taxonomy-018's
     * {@code hnsw.iterative_scan}/{@code hnsw.ef_search} pair, taxonomy-020's
     * {@code enable_seqscan}/{@code enable_sort} pair, nexus-swam7) -- the
     * single source of truth both {@code CrossPreviewDriftTest} and {@code
     * TaxonomyAssignCrossLateralHnswTest} assert their extracted {@code
     * effective} map equals.
     */
    public static final Map<String, String> EXPECTED_HNSW_SETTINGS = Map.of(
        "hnsw.iterative_scan", "strict_order",
        "hnsw.ef_search", "400",
        "enable_seqscan", "off",
        "enable_sort", "off");

    // DOTALL so a block comment spanning multiple lines is stripped whole;
    // reluctant (.*?) so two SEPARATE block comments in one body don't
    // collapse into one match spanning the real code between them.
    private static final Pattern BLOCK_COMMENT = Pattern.compile("/\\*.*?\\*/", Pattern.DOTALL);
    // A line comment runs from `--` to the next newline (or end of
    // string); matched AFTER block-comment stripping so a `--` sitting
    // inside a block comment is not double-processed.
    private static final Pattern LINE_COMMENT = Pattern.compile("--[^\\n]*");
    private static final Pattern SET_CONFIG = Pattern.compile(
        "set_config\\(\\s*'([^']+)'\\s*,\\s*'([^']*)'\\s*,\\s*true\\s*\\)");

    /**
     * Strip comments, then extract every {@code set_config(...)} call in
     * source order. Callers MUST pass the RAW {@code prosrc} (real
     * newlines) -- a whitespace-normalized string (all runs collapsed to
     * one space) has no newline left to stop a {@code --} line comment,
     * which would then swallow everything after it, including real code.
     */
    public static Extraction extract(String rawProsrc) {
        String stripped = BLOCK_COMMENT.matcher(rawProsrc).replaceAll(" ");
        stripped = LINE_COMMENT.matcher(stripped).replaceAll(" ");

        List<Call> calls = new ArrayList<>();
        Map<String, String> effective = new LinkedHashMap<>();
        Matcher m = SET_CONFIG.matcher(stripped);
        while (m.find()) {
            String name = m.group(1);
            String value = m.group(2);
            calls.add(new Call(name, value));
            effective.put(name, value); // last-occurrence-wins: a later put() overwrites
        }
        return new Extraction(effective, calls);
    }
}
