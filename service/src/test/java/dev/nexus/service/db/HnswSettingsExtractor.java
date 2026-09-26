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
 * <p>This extractor strips SQL comments first (a QUOTE-AWARE scan --
 * {@link #stripComments} -- so a {@code --} or block-comment starter
 * sitting inside a single-quoted string literal is never mistaken for a
 * real comment; round-4 review, critic Significant), then regex-matches
 * every {@code set_config('<name>', '<value>', true)} call IN SOURCE
 * ORDER, and reduces them to the LAST-OCCURRENCE-WINS map Postgres itself
 * would apply at runtime (a later {@code set_config} call for the same
 * name overrides an earlier one within the same transaction), plus the
 * total call COUNT. A caller can therefore assert both "this is the value
 * that actually takes effect" (exclusivity + precedence) and "no stray
 * extra or missing call site" (reachability), none of which a substring
 * check can distinguish.
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
        String stripped = stripComments(rawProsrc);

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

    /**
     * Round-4 review (critic, Significant): a plain regex strip of a line
     * comment or a block comment is NOT quote-aware, so it also strips the
     * {@code --} inside every plain single-quoted string literal that
     * happens to contain one -- {@code assign_from_chashes}'s own {@code
     * RAISE EXCEPTION} message ({@code '... %% -- register it first via
     * ...'}, 12 occurrences across taxonomy-018/020) is exactly such a
     * literal. Harmless only by the accident that no real {@code
     * set_config} pin shares that literal's line; a future body that DID
     * would silently lose the pin to a corrupted comment strip.
     *
     * <p>This is a small single-pass scanner instead: it tracks whether
     * the cursor is INSIDE a single-quoted string (handling Postgres's
     * {@code ''} doubled-quote escape, so a {@code '} inside a literal
     * does not end the string early) and treats a comment starter as a
     * comment ONLY when outside a string. A quote character found while
     * scanning a comment is NOT special -- a comment runs to end-of-line
     * or its closer regardless of what it contains, exactly like
     * Postgres's own lexer.
     *
     * <p>DOLLAR-QUOTING (Postgres's {@code $$...$$} / {@code $tag$...$tag$}
     * string form) is deliberately NOT handled: it is not needed for these
     * bodies. {@code cross_preview_<dim>} and {@code assign_from_chashes_
     * <dim>} use ordinary single-quoted literals throughout (visible in
     * their own changelog SQL); dollar-quoting in Postgres is used to
     * nest a string containing unescaped single quotes, or to write a
     * function body without doubling every quote in it, neither of which
     * applies to a `pg_proc.prosrc` value being scanned FROM THE OUTSIDE
     * (prosrc is already the un-dollar-quoted body text; a NESTED
     * dollar-quoted string inside a plpgsql body, e.g. for a dynamic
     * {@code EXECUTE}, would need this handled -- it would misparse quote
     * state from that point on -- but neither function does that).
     */
    private static String stripComments(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int n = src.length();
        int i = 0;
        boolean inString = false;
        while (i < n) {
            char c = src.charAt(i);
            if (inString) {
                if (c == '\'') {
                    if (i + 1 < n && src.charAt(i + 1) == '\'') {
                        out.append("''"); // escaped quote: stays inside the string
                        i += 2;
                    } else {
                        out.append(c); // closing quote
                        inString = false;
                        i++;
                    }
                } else {
                    out.append(c);
                    i++;
                }
                continue;
            }
            // Outside a string: a comment starter takes priority over
            // entering a new string, since neither comment form can
            // itself open one.
            if (c == '-' && i + 1 < n && src.charAt(i + 1) == '-') {
                int j = i + 2;
                while (j < n && src.charAt(j) != '\n') {
                    j++;
                }
                out.append(' ');
                i = j; // leaves the newline itself (if any) for the next iteration
                continue;
            }
            if (c == '/' && i + 1 < n && src.charAt(i + 1) == '*') {
                int j = i + 2;
                while (j + 1 < n && !(src.charAt(j) == '*' && src.charAt(j + 1) == '/')) {
                    j++;
                }
                i = (j + 1 < n) ? j + 2 : n; // unterminated: consume to end
                out.append(' ');
                continue;
            }
            if (c == '\'') {
                inString = true;
                out.append(c);
                i++;
                continue;
            }
            out.append(c);
            i++;
        }
        return out.toString();
    }
}
