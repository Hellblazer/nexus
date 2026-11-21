// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * House-rule gate (bead nexus-ft04v.15, RDR-204 Phase 2 item 3; ACCEPTANCE
 * SIGNAL 1 of 2): pins the count of collection-name PARSE sites in {@code
 * service/src/main/java} at the measured value, reduce-only, so the sibling
 * bead nexus-ft04v.16 (which replaces those sites with a registry lookup)
 * has a visible ratchet to drive to zero. Modeled on {@link
 * dev.nexus.service.db.RawSqlGateTest}'s per-file declared-count ceiling
 * (the pattern §Existing Infrastructure Audit names): comment-blanked source,
 * a per-file {@code Map<String, Integer>} pinned at the measured count, an
 * over-count fails as a REGRESSION, a file falling BELOW its declared count
 * fails as a STALE FINGERPRINT (the direction that keeps a pin from silently
 * rotting once the sites it counts are deleted), and a non-vacuity assert
 * that the walk actually saw files and matched at least the declared total.
 *
 * <p><b>Why this does not count {@code split("__")} alone.</b> The RDR text
 * that seeded this bead describes the target as "pins the count of {@code
 * split("__")} on collection names", but the measured engine census (T2
 * {@code nexus/rdr-204-planning-source-corrections-2026-09-07} [24871],
 * finding 1) found that {@code EmbedderRouter}'s content-type routing
 * ({@code CCE_PREFIXES}/{@code CODE_PREFIX} and their {@code startsWith}
 * uses, and {@code stripTrailingSeparator}'s {@code endsWith("__")}) derives
 * content type from the collection name WITHOUT ever calling {@code split}.
 * A gate that grepped {@code split("__")} alone would sit at zero the moment
 * those two files' {@code split} calls were converted while the prefix-based
 * routing survived untouched — exactly the kind of gate that reports a false
 * "done" (the class javadoc's own worked example: {@code
 * StagingPromoteOps}'s {@code contains}/{@code indexOf}/{@code substring}
 * trio was a second call shape entirely absent from a split-only scan, and
 * was in fact already deleted by the time this bead landed).
 *
 * <p><b>What is actually counted.</b> Every constant that feeds a parse
 * call ({@code CCE_PREFIXES}, {@code CODE_PREFIX}) and every inline literal
 * argument to a parse call ({@code split("__")}, {@code endsWith("__")}) is,
 * at the Java source level, a STRING LITERAL containing {@code "__"} — the
 * constant's own declaration site is one such literal, and an inline call is
 * another. So {@link #countDunderLiterals} counts every non-comment string
 * literal containing {@code "__"} per file; that census covers constants and
 * inline calls alike without having to track which named constant feeds
 * which call site (a dataflow problem this gate deliberately does not take
 * on). {@link #countSplitDunderCalls} additionally pins {@code split("__")}
 * specifically, as its own narrower per-file count, purely so nexus-ft04v.16's
 * acceptance criterion — "the split pin falls to zero" — has a number to
 * point at; it is a subset of the literal census, not an independent one.
 *
 * <p><b>Reduce-only.</b> Exactly like {@link
 * dev.nexus.service.db.RawSqlGateTest#TEST_TREE_RAW_SQL_CEILING}: a file
 * whose count goes to zero must have its entry REMOVED, not left at {@code
 * 0} — the "no entry means zero expected" default already covers that, and a
 * stray {@code file -> 0} entry is dead weight the stale-fingerprint check
 * cannot distinguish from a real ceiling.
 *
 * <p><b>Outcome at nexus-ft04v.16 (superseded by the Phase 2 fix round —
 * see the next paragraph).</b> At nexus-ft04v.16 itself, {@link
 * #COLLECTION_SPLIT_CENSUS} reached zero (empty map, ceiling 0) but {@link
 * #COLLECTION_LITERAL_CENSUS} did not: {@code EmbedderRouter.java} kept a
 * 5-literal entry ({@code CCE_PREFIXES}/{@code CODE_PREFIX}/{@code
 * stripTrailingSeparator}'s {@code endsWith("__")}), justified by two uses —
 * {@code resolveEmbedder}'s legacy prefix routing, kept for what its own
 * javadoc called "the collection-less {@code /v1/vectors/embed} parity
 * path," and {@code contentTypeModelTokens}'s profile-seeding map.
 *
 * <p><b>Outcome at the Phase 2 fix round (nexus-ft04v.16 fix round,
 * code-review-expert Critical finding).</b> The "collection-less parity
 * path" justification above was FALSE: {@code /v1/vectors/embed} always
 * required a non-null {@code collection} (or, after the fix round, a
 * {@code model}) — no production caller ever invoked {@code
 * resolveEmbedder(String)} with a real argument, and {@code
 * resolveEmbedderStrict}'s {@code collection == null} branch that fell
 * through to it had no caller either. Both are deleted outright, along with
 * {@code CCE_PREFIXES}/{@code CODE_PREFIX}/{@code stripTrailingSeparator}:
 * {@code contentTypeModelTokens} now keys its profile-seeding map on bare
 * content-type string literals directly, needing no prefix constant to
 * derive them from. {@link #COLLECTION_LITERAL_CENSUS} now reaches zero
 * (empty map, ceiling 0) for BOTH files — the prediction the original
 * nexus-ft04v.15 javadoc made before the true shape of the surviving use
 * was known.
 *
 * <p>This gate is a pure source scanner (no jOOQ, no DB, no {@code
 * scripts/mvnw-leased.sh} substrate beyond plain {@code javac}/JUnit) — it
 * walks {@code service/src/main/java} once per {@code @Test} run and reads
 * plain text, exactly like {@code RawSqlGateTest} does for its own tree
 * walks.
 */
class CollectionParseGateTest {

    /** The index just past the text block that opens with {@code """} at {@code open}; the end of the source when unterminated. */
    static int textBlockEnd(String src, int open) {
        int i = open + 3;
        while (i < src.length()) {
            if (src.charAt(i) == '\\') { i += 2; continue; }
            if (src.startsWith("\"\"\"", i)) return i + 3;
            i++;
        }
        return src.length();
    }

    /**
     * Comment-blanking identical in shape to {@link
     * dev.nexus.service.db.RawSqlGateTest#blankComments} — duplicated here
     * rather than shared because that method is package-private to {@code
     * dev.nexus.service.db} and this gate lives in {@code
     * dev.nexus.service.vectors} (the package the parse sites themselves
     * live in, per this bead's own instructions). Blanks {@code //} and
     * {@code /* *}{@code /} comments to spaces (preserving line numbers);
     * string and char literals are skipped over WITHOUT being blanked, so a
     * literal's own content — including one that happens to contain {@code
     * /}{@code *} or {@code //} — survives intact for {@link
     * #countDunderLiterals} to inspect.
     */
    static String blankComments(String src) {
        char[] out = src.toCharArray();
        int i = 0;
        while (i < out.length) {
            char c = out[i];
            if (c == '/' && i + 1 < out.length && out[i + 1] == '*') {
                int end = src.indexOf("*/", i + 2);
                end = (end < 0) ? out.length : end + 2;
                for (int j = i; j < end; j++) if (out[j] != '\n') out[j] = ' ';
                i = end;
            } else if (c == '/' && i + 1 < out.length && out[i + 1] == '/') {
                while (i < out.length && out[i] != '\n') out[i++] = ' ';
            } else if (c == '"' && src.startsWith("\"\"\"", i)) {
                i = textBlockEnd(src, i);  // a text block: its quotes and comment-like text are content
            } else if (c == '"' || c == '\'') {
                char q = c;
                i++;
                while (i < out.length && out[i] != q) {
                    if (src.charAt(i) == '\\' && i + 1 < out.length) i++;
                    i++;
                }
                i++;  // closing quote
            } else {
                i++;
            }
        }
        return new String(out);
    }

    /**
     * Every double-quoted string literal in (comment-blanked) source, found by a
     * hand-rolled scan rather than a regex. The first cut used
     * {@code "(?:[^"\\]|\\.)*"}, whose alternation-inside-a-loop recurses once
     * per character in {@code java.util.regex}; a long literal overflowed the
     * Linux CI runner's thread stack ({@code StackOverflowError} on every
     * develop push from aaf585006 to f754d2eda, 2026-09-08/09) while the larger
     * default stack on macOS hid it locally. A linear scan cannot overflow.
     */
    static List<String> stringLiterals(String blanked) {
        List<String> out = new ArrayList<>();
        int i = 0;
        int n = blanked.length();
        while (i < n) {
            char first = blanked.charAt(i);
            if (first == '\'') {  // a char literal: its content, a quote included, is not a string
                i++;
                while (i < n && blanked.charAt(i) != '\'' && blanked.charAt(i) != '\n') i += (blanked.charAt(i) == '\\') ? 2 : 1;
                i++;
                continue;
            }
            if (first != '"') { i++; continue; }
            if (blanked.startsWith("\"\"\"", i)) {  // a text block is one literal, quotes inside it included
                int end = textBlockEnd(blanked, i);
                out.add(blanked.substring(i, Math.min(end, n)));
                i = end;
                continue;
            }
            int start = i;
            i++;
            while (i < n) {
                char c = blanked.charAt(i);
                if (c == '\\' && i + 1 < n) { i += 2; continue; }
                if (c == '"') { i++; break; }
                if (c == '\n') { break; }  // unterminated: a char literal or text block edge; stop at the line
                i++;
            }
            out.add(blanked.substring(start, Math.min(i, n)));
        }
        return out;
    }

    /** {@code .split(\"__\")} specifically — a strict subset of {@link
     * #stringLiterals} matches, pinned separately so nexus-ft04v.16's
     * acceptance ("the split pin falls to zero") is a visible number. */
    private static final Pattern SPLIT_DUNDER = Pattern.compile("\\.split\\s*\\(\\s*\"__\"\\s*\\)");

    /**
     * Count of non-comment string literals containing {@code "__"} in
     * {@code rawSource}. This is the primary census: it catches both a
     * constant DECLARATION ({@code CCE_PREFIXES}, {@code CODE_PREFIX}) and
     * an INLINE call-site literal ({@code split("__")}, {@code
     * endsWith("__")}) by construction, since both are string literals at
     * the source level — see this class's own javadoc for why counting
     * {@code split} alone would miss the prefix-routing half of the census.
     */
    static int countDunderLiterals(String rawSource) {
        String blanked = blankComments(rawSource);
        int count = 0;
        for (String literal : stringLiterals(blanked)) {
            if (literal.contains("__")) count++;
        }
        return count;
    }

    /** Count of {@code .split("__")} call sites in {@code rawSource}, comment-blanked. */
    static int countSplitDunderCalls(String rawSource) {
        String blanked = blankComments(rawSource);
        Matcher m = SPLIT_DUNDER.matcher(blanked);
        int count = 0;
        while (m.find()) count++;
        return count;
    }

    /**
     * Per-file declared count of {@link #countDunderLiterals}, relative to
     * {@code src/main/java} (same path convention as {@link
     * dev.nexus.service.db.RawSqlGateTest#TEST_TREE_RAW_SQL_CEILING}).
     * Measured after nexus-ft04v.16 (RDR-204 Phase 2 item 2): every remaining
     * engine collection-name PARSE site (both {@code split("__")} call sites
     * and their message literals) now reads the {@link
     * dev.nexus.service.db.CollectionRegistry} row instead — {@code
     * PgVectorRepository.java} reaches ZERO literals and loses its census
     * entry entirely (a file at zero carries no entry, per this gate's own
     * reduce-only rule).
     *
     * <p>{@code EmbedderRouter.java} reaches ZERO (Phase 2 fix round,
     * nexus-ft04v.16 fix round): the 5-literal group this gate used to pin
     * ({@code CCE_PREFIXES}' three entries, {@code CODE_PREFIX}, and {@code
     * stripTrailingSeparator}'s {@code endsWith("__")}) is deleted outright.
     * Its two claimed justifications did not survive scrutiny: (1) {@code
     * resolveEmbedder}'s legacy prefix routing was kept for what its own
     * javadoc called "the collection-less {@code /v1/vectors/embed} parity
     * path" — a path that never existed in production (that route always
     * required a non-null {@code collection} or, after the fix round, a
     * {@code model}; grepping every call site found zero non-test,
     * non-null invocations) — so the method itself, and the {@code
     * resolveEmbedderStrict(collection == null)} branch that was its only
     * production-reachable caller, are both deleted as dead code; and (2)
     * {@code contentTypeModelTokens}'s profile-seeding map no longer needs
     * the prefix constants at all — it now keys its content-type → model-token
     * map directly on the bare content-type strings ({@code "knowledge"},
     * {@code "docs"}, {@code "rdr"}, {@code "code"}, {@code "unknown"})
     * rather than deriving them from name-routing literals. No entry
     * survives for this file; a file at zero carries no entry, per this
     * gate's own reduce-only rule.
     *
     * <p>{@code db/NoteUri.java} declares 3 (RDR-231, nexus-bivz3.1): its
     * {@code FILE_ROUTED_PREFIXES} ({@code rdr__}, {@code docs__},
     * {@code code__}). It classifies a source_uri STRING, not a live
     * collection: a note URI's collection segment can name a collection that
     * was renamed or never registered, so a {@link
     * dev.nexus.service.db.CollectionRegistry} lookup cannot answer it. The
     * list mirrors the client's {@code aspect_readers.FILE_ROUTED_PREFIXES}
     * and the backfill predicate, and {@code NoteUriCorpusTest} pins all
     * three to one corpus file.
     *
     * <p>{@code db/CollectionRegistration.java} declares 1 (RDR-229 P2.5 D1, nexus-p2m1x.10): the conformant-name
     * pattern that reads the {@code vN} of a conformant name. It has two callers: the ETL import of a NEW row that
     * carries no {@code model_version} (a name from another install or an older era, a MIGRATION SOURCE in the RDR's
     * classification), and {@code ReembedHandler.register}, which reads the version off the target name the client
     * minted. It is the only place the engine reads a version out of a name, and it is retired at P4 with the name
     * parsers.
     *
     * <p>The same regex also reads a MODEL out of a name ({@code CollectionRegistration.modelOfConformantName}),
     * which adds no literal, so this per-file census cannot see it. Its callers are pinned separately by
     * {@code NameModelReadCensusTest.NAME_MODEL_READ_CALLS} (nexus-p2m1x.28): the one caller is {@code TargetSelection.foreignOrigin}, a
     * MIGRATION-SOURCE read of a re-embed source row deleted by cutover; it retires at P4, with the name parsers.
     */
    private static final Map<String, Integer> COLLECTION_LITERAL_CENSUS = Map.of(
        "dev/nexus/service/db/CollectionRegistration.java", 1,
        "dev/nexus/service/db/NoteUri.java", 3);

    private static final int COLLECTION_LITERAL_TOTAL_CEILING = 4;

    /**
     * Per-file declared count of {@link #countSplitDunderCalls} — the
     * narrower subset pin nexus-ft04v.16 drives to zero, in the same edit
     * that lowers {@link #COLLECTION_LITERAL_CENSUS} above: both {@code
     * split("__")} call sites this gate ever pinned (EmbedderRouter's
     * {@code resolveEmbedderStrict}; PgVectorRepository's {@code
     * dimForCollection} and {@code modelSegment}) are gone, so BOTH files
     * lose their entry (a file at zero carries no entry).
     */
    private static final Map<String, Integer> COLLECTION_SPLIT_CENSUS = Map.of();

    private static final int COLLECTION_SPLIT_TOTAL_CEILING = 0;

    // ── RDR-229 P1.3 (nexus-p2m1x.3): the quarantine- prefix ────────────

    /**
     * The one file and constant that renders the {@code quarantine-} prefix, which the literal census does not count
     * (RDR-229 Technical Design item 4: "outside the one constant that renders it"). The exemption is the declaration
     * {@code PREFIX = "quarantine-"} in this file and nothing else in it. {@link #QUARANTINE_RENDERER_DECLARATION} must
     * match exactly once there, so renaming or deleting the constant fails the gate instead of silently widening it.
     */
    static final String QUARANTINE_RENDERER_FILE = "dev/nexus/service/db/QuarantineOrigin.java";

    private static final Pattern QUARANTINE_RENDERER_DECLARATION =
        Pattern.compile("\\bString\\s+PREFIX\\s*=\\s*\"quarantine-\"");

    /**
     * Per-file declared count of non-comment string literals (text blocks included) that START with {@code quarantine-}
     * or hold it after an SQL quote ({@code LIKE 'quarantine-%'}, {@code starts_with(name, 'quarantine-')}), a name
     * rendered or tested with the prefix, outside {@link #QUARANTINE_RENDERER_FILE}'s one constant. A literal that merely mentions
     * the word ({@code "/gc/quarantine-restore"}, an error message) is not a name and is not counted. Measured at
     * develop {@code 1d341b77f}: 6, unchanged by round 1 (no SQL-quoted or text-block spelling exists in the tree).
     *
     * <ul>
     *   <li>{@code ChunkReaper.java} 1: {@code QUARANTINE_PREFIX}, a SECOND constant that renders the same prefix.
     *       RDR-229 names one constant, so the duplicate counts until it reads {@code QuarantineOrigin.PREFIX};</li>
     *   <li>{@code CatalogRepository.java} 4: two {@code startsWith("quarantine-")} tests in {@code upsertCollection}
     *       and two in {@code lifecycleStateForImport}, the sites that derive {@code lifecycle_state} from the name;</li>
     *   <li>{@code PgVectorRepository.java} 1: {@code "quarantine-" + originCollection} (the reaper's sibling name).</li>
     * </ul>
     * Reduce-only, both directions, a file at zero carries no entry (the rule of {@link #COLLECTION_LITERAL_CENSUS}).
     * RDR-229 P2.5 (nexus-p2m1x.10) took all six to zero: {@code ChunkReaper.QUARANTINE_PREFIX} aliases
     * {@code QuarantineOrigin.PREFIX}, registration takes {@code lifecycle_state} explicitly (the import fallback
     * reads the constant, in {@code CollectionRegistration}), and the sibling label reads the constant.
     *
     * <p>Known limit: a literal that holds the prefix after a JSON quote ({@code "{\"name\":\"quarantine-..."}) is not
     * counted; only a start-of-literal prefix and an SQL-quoted one are.
     */
    private static final Map<String, Integer> QUARANTINE_LITERAL_CENSUS = Map.of();

    private static final int QUARANTINE_LITERAL_TOTAL_CEILING = 0;

    /**
     * Reads of the prefix outside declarations and import lines, at develop {@code 1d341b77f}: 35. A literal-only census
     * would let a reader that goes through a constant escape the gate (RDR-229 research-22 counts 12 places that READ
     * the prefix; most do not spell the literal), so the reads are pinned too, in six shapes:
     * <ul>
     *   <li>{@code isQuarantineName(..)}, {@code QUARANTINE_PREFIX}, {@code QuarantineOrigin.PREFIX} (a bare {@code
     *       PREFIX} counts inside {@link #QUARANTINE_RENDERER_FILE} and in a file that statically imports it, at each
     *       use and not at the import);</li>
     *   <li>{@code requireNotQuarantine(..)}, the refusal every rename, rehome and store-delete route calls (11 call
     *       sites): it reads the prefix through {@code isQuarantineName}, so a replacement of that predicate has to
     *       reach these callers too;</li>
     *   <li>{@code BYPASS_PREFIXES} and {@code bypassPrefixes()}: {@code ChunkKeyPlan.BYPASS_PREFIXES} is {@code
     *       List.of(QuarantineOrigin.PREFIX, TAXONOMY_PREFIX)}, so every user of it carries the prefix, among them
     *       {@code ChunkKeyAllowlistCheck}, {@code ChunkKeySweep} and {@code ChunkConstraintAdmin} (the run-time CHECK
     *       built from the prefix). {@code TAXONOMY_PREFIX} is {@code "taxonomy" + COLLECTION_SEPARATOR}, built from a
     *       constant, so no literal in the tree holds the {@code taxonomy__} half: this census sees it only through
     *       the bypass set.</li>
     * </ul>
     * By file: ChunkReaper 4, QuarantineOrigin 6, ChunkKeyPlan 6, CatalogRepository 5, CatalogHandler 4,
     * PgVectorRepository 3, ChashRepository 2, ChunkConstraintAdmin 2, and 1 each in VectorHandler,
     * ChunkKeyAllowlistCheck and ChunkKeySweep. The first cut pinned 15 (the first three shapes); round 1 added the
     * other three, which is why this pin moved and the literal pin did not.
     *
     * <p>RDR-229 P2.5 (nexus-p2m1x.10), 35 -> 34. Guards on an existing collection read {@code lifecycle_state};
     * the prefix is read only where no row exists, so the {@code requireNotQuarantine} call sites still count
     * (the guard falls back to the prefix for an end with no row). ChunkReaper 4 -> 3, QuarantineOrigin 6 -> 4
     * ({@code isQuarantineName} is private, the row-less rule), VectorHandler 1 -> 0 (the name-only overload is
     * gone), PgVectorRepository 3 -> 2. New: CollectionRegistration 4, which is the named residuals the RDR's
     * item 2 lists: the ETL import's prefix fallback (two reads, its content-type and name tests, moved here from
     * CatalogRepository's literals), its state-and-name agreement check, and the client route's refusal of a new
     * {@code quarantine-} name. The bypass-set readers (ChunkKeyPlan, ChunkConstraintAdmin, ChunkKeyAllowlistCheck,
     * ChunkKeySweep) are unchanged pending the allow-list CHECK ruling.
     *
     * <p>Review round 2 (N1), 34 -> 35: ReembedHandler 0 -> 1, {@code requireRegistrable}'s refusal of a re-embed
     * target named {@code quarantine-...}, so the prefix guard stays "a quarantine- name is an engine mint". It is a
     * name read for a name no row exists for yet, the same class as the client route's refusal in
     * CollectionRegistration.
     *
     * <p>Review round 3, 35 -> 36: QuarantineOrigin 4 -> 5, {@code requireMoverSibling}, the 400 for a
     * {@code quarantine_collection} without the prefix on {@code POST /v1/vectors/gc/quarantine-orphans} (the SQL
     * mover registers whatever it is given as a quarantine collection). The same registration-side name-guard class as
     * the client route's refusal; nothing else was lowered to hide it.
     */
    private static final Map<String, Integer> QUARANTINE_READ_CENSUS = Map.ofEntries(
        Map.entry("dev/nexus/service/ChunkReaper.java", 3),
        Map.entry("dev/nexus/service/db/CatalogRepository.java", 5),
        Map.entry("dev/nexus/service/db/ChashRepository.java", 2),
        Map.entry("dev/nexus/service/db/CollectionRegistration.java", 4),
        Map.entry("dev/nexus/service/db/QuarantineOrigin.java", 5),
        Map.entry("dev/nexus/service/http/CatalogHandler.java", 4),
        Map.entry("dev/nexus/service/http/ReembedHandler.java", 1),
        Map.entry("dev/nexus/service/vectors/ChunkConstraintAdmin.java", 2),
        Map.entry("dev/nexus/service/vectors/ChunkKeyAllowlistCheck.java", 1),
        Map.entry("dev/nexus/service/vectors/ChunkKeyPlan.java", 6),
        Map.entry("dev/nexus/service/vectors/ChunkKeySweep.java", 1),
        Map.entry("dev/nexus/service/vectors/PgVectorRepository.java", 2));

    private static final int QUARANTINE_READ_TOTAL_CEILING = 36;

    private static final Pattern QUARANTINE_READ = Pattern.compile(
        "(?<!String\\s{1,40})\\bQUARANTINE_PREFIX\\b"
        + "|(?<!boolean\\s{1,40})\\bisQuarantineName\\b"
        + "|(?<!void\\s{1,40})\\brequireNotQuarantine\\b"
        + "|\\bQuarantineOrigin\\.PREFIX\\b"
        + "|(?<!List<String>\\s{1,40})\\bBYPASS_PREFIXES\\b"
        + "|\\bbypassPrefixes\\s*\\(\\s*\\)");

    /** The renderer file's own bare {@code PREFIX}, or a file's after a static import of it: not qualified, not a declaration. */
    private static final Pattern QUARANTINE_BARE_PREFIX_READ =
        Pattern.compile("(?<![\\w.])(?<!String\\s{1,40})\\bPREFIX\\b");

    private static final Pattern STATIC_IMPORT_OF_PREFIX =
        Pattern.compile("(?m)^\\s*import\\s+static\\s+[\\w.]*\\bQuarantineOrigin\\.(?:PREFIX|\\*)\\s*;");

    private static final Pattern IMPORT_LINE = Pattern.compile("(?m)^\\s*import\\s[^;]*;");

    /**
     * Count of non-comment string literals starting with {@code quarantine-}, with the renderer constant's own
     * declaration excluded when {@code isRendererFile}. Returns {@code {counted, exempted}}.
     */
    static int[] countQuarantineLiterals(String rawSource, boolean isRendererFile) {
        String blanked = blankComments(rawSource);
        int exempted = 0;
        if (isRendererFile) {
            Matcher d = QUARANTINE_RENDERER_DECLARATION.matcher(blanked);
            StringBuilder sb = new StringBuilder();
            while (d.find()) {
                exempted++;
                d.appendReplacement(sb, Matcher.quoteReplacement(" ".repeat(d.group().length())));
            }
            d.appendTail(sb);
            blanked = sb.toString();
        }
        int count = 0;
        for (String literal : stringLiterals(blanked)) {
            if (literal.startsWith("\"quarantine-") || literal.contains("'quarantine-")) count++;
        }
        return new int[] {count, exempted};
    }

    /**
     * Count of reads of the quarantine prefix, outside declarations and import lines: {@code isQuarantineName},
     * {@code QUARANTINE_PREFIX}, {@code QuarantineOrigin.PREFIX} (a bare {@code PREFIX} inside {@link
     * #QUARANTINE_RENDERER_FILE}, or in a file that statically imports it, counted at each use), {@code
     * requireNotQuarantine}, {@code BYPASS_PREFIXES} and {@code bypassPrefixes()}.
     */
    static int countQuarantinePrefixReads(String rawSource, boolean isRendererFile) {
        String blanked = blankComments(rawSource);
        boolean staticImport = STATIC_IMPORT_OF_PREFIX.matcher(blanked).find();
        blanked = IMPORT_LINE.matcher(blanked).replaceAll(m -> " ".repeat(m.group().length()));
        int count = 0;
        Matcher m = QUARANTINE_READ.matcher(blanked);
        while (m.find()) count++;
        if (isRendererFile || staticImport) {
            Matcher b = QUARANTINE_BARE_PREFIX_READ.matcher(blanked);
            while (b.find()) count++;
        }
        return count;
    }

    /**
     * Compares an {@code actual} per-file census against a {@code declared}
     * one in BOTH directions, exactly like {@link
     * dev.nexus.service.db.RawSqlGateTest#noRawExecuteSqlRegressionInTestSources}
     * does for its own ceiling map: a file counting MORE than its declared
     * pin is a REGRESSION (a new parse site, or a file the census has never
     * seen); a declared file counting FEWER than its pin is a STALE
     * FINGERPRINT (the site was removed without lowering the pin — the
     * direction that keeps this ratchet reduce-only rather than a one-way
     * ceiling that could silently stop meaning anything).
     */
    static List<String> checkCensus(String label, Map<String, Integer> actual, Map<String, Integer> declared) {
        List<String> violations = new ArrayList<>();
        for (var entry : actual.entrySet()) {
            int decl = declared.getOrDefault(entry.getKey(), 0);
            if (entry.getValue() > decl) {
                violations.add(entry.getKey() + ": REGRESSION -- " + entry.getValue() + " " + label
                    + " site(s) found, census declares " + decl
                    + " (new parse site added, or a file the census has never seen)");
            }
        }
        for (var entry : declared.entrySet()) {
            int found = actual.getOrDefault(entry.getKey(), 0);
            if (found < entry.getValue()) {
                violations.add(entry.getKey() + ": STALE FINGERPRINT -- declares " + entry.getValue()
                    + ", only " + found + " found -- lower this census entry (and its total ceiling) "
                    + "to match, or remove it outright if the file reached zero");
            }
        }
        return violations;
    }

    // ── the real-tree gate ──────────────────────────────────────────────

    /**
     * The gate itself: walks {@code src/main/java}, scans every file with
     * {@link #countDunderLiterals}/{@link #countSplitDunderCalls}, and
     * checks both censuses against {@link #COLLECTION_LITERAL_CENSUS}/
     * {@link #COLLECTION_SPLIT_CENSUS} in both directions, plus their
     * aggregate ceilings. Passes on the tree as it stands at the measured
     * pins, lowered by nexus-ft04v.16 in the same edit that replaced the
     * parse sites — see {@link #COLLECTION_LITERAL_CENSUS}'s javadoc for
     * why one 5-literal entry survives rather than reaching zero.
     */
    @Test
    void collectionNameParseCensus_pinnedAtMeasuredCount() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        Map<String, Integer> actualLiterals = new TreeMap<>();
        Map<String, Integer> actualSplits = new TreeMap<>();
        int filesWalked = 0;
        try (Stream<Path> files = Files.walk(root)) {
            List<Path> javaFiles = files.filter(p -> p.toString().endsWith(".java")).toList();
            filesWalked = javaFiles.size();
            for (Path p : javaFiles) {
                String rel = root.relativize(p).toString().replace(java.io.File.separatorChar, '/');
                String src = Files.readString(p);
                int lit = countDunderLiterals(src);
                int split = countSplitDunderCalls(src);
                if (lit > 0) actualLiterals.put(rel, lit);
                if (split > 0) actualSplits.put(rel, split);
            }
        }

        // Non-vacuity: a broken root path or an empty tree would let every
        // check below pass vacuously (an empty actual map never regresses
        // against anything). See the nexus-moht0 vacuous-gate doctrine.
        assertThat(filesWalked)
            .as("the walk over " + root + " must see a non-zero number of .java files -- "
                + "zero means the root path is wrong, not that nothing needed scanning")
            .isGreaterThan(0);

        List<String> violations = new ArrayList<>();
        violations.addAll(checkCensus("collection-name parse literal", actualLiterals, COLLECTION_LITERAL_CENSUS));
        violations.addAll(checkCensus("split(\"__\")", actualSplits, COLLECTION_SPLIT_CENSUS));

        assertThat(violations)
            .as("collection-name parse-site census (nexus-ft04v.15) -- a REGRESSION means a new "
                + "string-literal parse site was added; a STALE FINGERPRINT means a declared site "
                + "was removed without lowering its census entry (nexus-ft04v.16 should do exactly "
                + "that, in the same edit that replaces the parse site with a registry lookup)")
            .isEmpty();

        int totalLiterals = actualLiterals.values().stream().mapToInt(Integer::intValue).sum();
        assertThat(totalLiterals)
            .as("COLLECTION_LITERAL_TOTAL_CEILING must be lowered in the same edit as any "
                + "COLLECTION_LITERAL_CENSUS reduction")
            .isLessThanOrEqualTo(COLLECTION_LITERAL_TOTAL_CEILING);

        int totalSplits = actualSplits.values().stream().mapToInt(Integer::intValue).sum();
        assertThat(totalSplits)
            .as("COLLECTION_SPLIT_TOTAL_CEILING must be lowered in the same edit as any "
                + "COLLECTION_SPLIT_CENSUS reduction")
            .isLessThanOrEqualTo(COLLECTION_SPLIT_TOTAL_CEILING);
    }

    /**
     * {@code test_scanner_is_not_vacuous} equivalent: the walk saw a
     * non-zero number of files AND matched at least the declared literal
     * total. A regex that broke (matched nothing) would report {@code 0}
     * here even though {@link #COLLECTION_LITERAL_CENSUS} declares 10 --
     * that is a scanner failure, not a clean tree, and must fail loud.
     */
    @Test
    void scanner_isNotVacuous_walkedFilesAndMatchedDeclaredSites() throws IOException {
        Path root = Path.of("src", "main", "java");

        long javaFileCount;
        try (Stream<Path> files = Files.walk(root)) {
            javaFileCount = files.filter(p -> p.toString().endsWith(".java")).count();
        }
        assertThat(javaFileCount)
            .as("the walk must see a non-zero number of source files")
            .isGreaterThan(0);

        int totalLiteralHits = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(p -> p.toString().endsWith(".java")).toList()) {
                totalLiteralHits += countDunderLiterals(Files.readString(p));
            }
        }
        int declaredTotal = COLLECTION_LITERAL_CENSUS.values().stream().mapToInt(Integer::intValue).sum();
        assertThat(totalLiteralHits)
            .as("non-vacuity: the scanner found " + totalLiteralHits + " dunder-literal sites but the "
                + "census declares " + declaredTotal + " -- a broken regex or a broken root path would "
                + "both silently report a lower number here, not fail")
            .isGreaterThanOrEqualTo(declaredTotal);
    }

    /**
     * RDR-229 P1.3: the quarantine census over the real tree, both classes in both directions, plus the non-vacuity
     * assertions: the walk saw files, the renderer file was seen with its one exempt declaration, and the literal
     * scanner found at least the declared total (a broken scanner reports fewer, never an error).
     */
    @Test
    void quarantinePrefixCensus_pinnedAtMeasuredCount() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        Map<String, Integer> literals = new TreeMap<>();
        Map<String, Integer> reads = new TreeMap<>();
        int filesWalked = 0;
        int exemptions = 0;
        boolean rendererSeen = false;
        try (Stream<Path> files = Files.walk(root)) {
            List<Path> javaFiles = files.filter(p -> p.toString().endsWith(".java")).toList();
            filesWalked = javaFiles.size();
            for (Path p : javaFiles) {
                String rel = root.relativize(p).toString().replace(java.io.File.separatorChar, '/');
                boolean renderer = rel.equals(QUARANTINE_RENDERER_FILE);
                rendererSeen |= renderer;
                String src = Files.readString(p);
                int[] lit = countQuarantineLiterals(src, renderer);
                int read = countQuarantinePrefixReads(src, renderer);
                exemptions += lit[1];
                if (lit[0] > 0) literals.put(rel, lit[0]);
                if (read > 0) reads.put(rel, read);
            }
        }
        int totalLiterals = literals.values().stream().mapToInt(Integer::intValue).sum();
        int totalReads = reads.values().stream().mapToInt(Integer::intValue).sum();
        System.out.println("quarantine census: files=" + filesWalked + " exemptedRendererDeclarations=" + exemptions
            + " literals=" + totalLiterals + literals + " reads=" + totalReads + reads);

        assertThat(filesWalked).as("the walk over " + root + " must see .java files").isGreaterThan(0);
        assertThat(rendererSeen).as(QUARANTINE_RENDERER_FILE + " exists: the one constant that renders the prefix").isTrue();
        assertThat(exemptions).as("the renderer file declares `String PREFIX = \"quarantine-\"` exactly once; zero means the "
            + "constant was renamed or moved and the exemption no longer names it").isEqualTo(1);

        List<String> violations = new ArrayList<>();
        violations.addAll(checkCensus("quarantine- literal", literals, QUARANTINE_LITERAL_CENSUS));
        violations.addAll(checkCensus("quarantine- prefix read", reads, QUARANTINE_READ_CENSUS));
        assertThat(violations)
            .as("RDR-229 item 4 quarantine- census (nexus-p2m1x.3): a REGRESSION is a new site that spells or reads the "
                + "prefix; a STALE FINGERPRINT is a site removed without lowering its entry")
            .isEmpty();
        assertThat(totalLiterals).as("QUARANTINE_LITERAL_TOTAL_CEILING is lowered with the census")
            .isEqualTo(QUARANTINE_LITERAL_TOTAL_CEILING);
        assertThat(totalReads).as("QUARANTINE_READ_TOTAL_CEILING is lowered with the census")
            .isEqualTo(QUARANTINE_READ_TOTAL_CEILING);

        int declaredLiterals = QUARANTINE_LITERAL_CENSUS.values().stream().mapToInt(Integer::intValue).sum();
        int declaredReads = QUARANTINE_READ_CENSUS.values().stream().mapToInt(Integer::intValue).sum();
        assertThat(totalLiterals).as("non-vacuity: a broken scanner reports fewer literals than the census declares")
            .isGreaterThanOrEqualTo(declaredLiterals);
        assertThat(totalReads).as("non-vacuity: a broken scanner reports fewer reads than the census declares")
            .isGreaterThanOrEqualTo(declaredReads);
    }

    /** Each total ceiling equals the sum of its declared census, so lowering a census and forgetting its ceiling fails. */
    @Test
    void everyCeilingEqualsItsDeclaredCensusSum() {
        assertThat(COLLECTION_LITERAL_CENSUS.values().stream().mapToInt(Integer::intValue).sum())
            .isEqualTo(COLLECTION_LITERAL_TOTAL_CEILING);
        assertThat(COLLECTION_SPLIT_CENSUS.values().stream().mapToInt(Integer::intValue).sum())
            .isEqualTo(COLLECTION_SPLIT_TOTAL_CEILING);
        assertThat(QUARANTINE_LITERAL_CENSUS.values().stream().mapToInt(Integer::intValue).sum())
            .isEqualTo(QUARANTINE_LITERAL_TOTAL_CEILING);
        assertThat(QUARANTINE_READ_CENSUS.values().stream().mapToInt(Integer::intValue).sum())
            .isEqualTo(QUARANTINE_READ_TOTAL_CEILING);
    }

    // ── falsification: the detector must actually detect ────────────────

    /**
     * A synthetic source string with one MORE dunder literal than the file's
     * declared census fails as a REGRESSION. Driven through {@link
     * #countDunderLiterals} over a {@code String} (never a filesystem
     * mutation), exactly the shape {@code RawSqlGateTest}'s own synthetic
     * fixtures use for {@link #checkCensus}'s counterpart in that class.
     */
    @Test
    void syntheticExtraParseSite_failsAsRegression() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    private static final String A = \"code__\";",
            "    private static final String B = \"docs__\";",
            "    void extra() {",
            "        // a brand-new parse site nobody has declared yet",
            "        boolean matches = \"knowledge__\".equals(prefix);",
            "    }",
            "}");
        int found = countDunderLiterals(synthetic);
        assertThat(found).isEqualTo(3);

        Map<String, Integer> actual = Map.of("Whatever.java", found);
        Map<String, Integer> declared = Map.of("Whatever.java", 2);  // the pin predates the new site

        List<String> violations = checkCensus("collection-name parse literal", actual, declared);
        assertThat(violations)
            .as("a new parse-site literal beyond the declared pin must fail loud, never pass silently")
            .anyMatch(v -> v.contains("REGRESSION"));
    }

    /**
     * A declared file whose parse-site count falls BELOW its pin (the site
     * was deleted without lowering the census entry) fails as a STALE
     * FINGERPRINT — this is what makes the ratchet reduce-only: the count
     * can go down only via a deliberate edit to {@link
     * #COLLECTION_LITERAL_CENSUS}/{@link #COLLECTION_SPLIT_CENSUS}
     * themselves, never as a silent side effect of deleting code.
     */
    @Test
    void declaredSiteRemovedWithoutLoweringPin_failsAsStaleFingerprint() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    private static final String A = \"code__\";",
            "    // the second literal that used to live here was deleted without lowering the pin",
            "}");
        int found = countDunderLiterals(synthetic);
        assertThat(found).isEqualTo(1);

        Map<String, Integer> actual = Map.of("Whatever.java", found);
        Map<String, Integer> declared = Map.of("Whatever.java", 2);  // stale: the tree no longer has 2

        List<String> violations = checkCensus("collection-name parse literal", actual, declared);
        assertThat(violations)
            .as("a declared pin the tree no longer supports must fail as a stale fingerprint, "
                + "never silently pass")
            .anyMatch(v -> v.contains("STALE FINGERPRINT"));
    }

    /**
     * Regression for the CI-only StackOverflowError (develop aaf585006..f754d2eda):
     * a single very long string literal must scan in constant stack. Run with
     * a deliberately small stack so the failure mode is reproducible on any
     * platform rather than only on a Linux runner's default thread stack.
     */
    @Test
    void veryLongLiteral_scansWithoutStackOverflow() throws InterruptedException {
        String longLiteral = "\"" + "x".repeat(200_000) + "__" + "y".repeat(200_000) + "\"";
        String synthetic = "class W { static final String S = " + longLiteral + "; }";
        int[] found = new int[1];
        Throwable[] failure = new Throwable[1];
        Thread t = new Thread(null, () -> {
            try {
                found[0] = countDunderLiterals(synthetic);
            } catch (Throwable e) {
                failure[0] = e;
            }
        }, "small-stack-scan", 256 * 1024);
        t.start();
        t.join();
        assertThat(failure[0]).as("the literal scan must not recurse per character").isNull();
        assertThat(found[0]).isEqualTo(1);
    }

    /** Commented-out literals must not count -- {@link #blankComments} strips
     * both comment shapes before {@link #countDunderLiterals} scans. */
    @Test
    void commentedOutLiterals_areNotCounted() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    // private static final String OLD = \"code__\";",
            "    /* private static final String OLDER = \"docs__\"; */",
            "    private static final String REAL = \"knowledge__\";",
            "}");
        assertThat(countDunderLiterals(synthetic)).isEqualTo(1);
    }

    /** {@link #countSplitDunderCalls} counts only {@code split("__")}, a
     * strict subset of {@link #countDunderLiterals}'s broader literal
     * census -- a bare {@code "__"}-containing literal used elsewhere
     * (e.g. {@code endsWith("__")}) must not inflate the split-specific pin. */
    @Test
    void splitScan_isStrictSubsetOfLiteralScan() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    private static final String A = \"code__\";",
            "    void m(String collection, String prefix) {",
            "        boolean trailing = prefix.endsWith(\"__\");",
            "        String[] segments = collection.split(\"__\");",
            "    }",
            "}");
        assertThat(countDunderLiterals(synthetic)).isEqualTo(3);
        assertThat(countSplitDunderCalls(synthetic)).isEqualTo(1);
    }

    // ── RDR-229 P1.3 falsification: the quarantine census must detect ───

    @Test
    void syntheticQuarantineLiteral_failsAsRegression() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    boolean q(String name) { return name.startsWith(\"quarantine-\"); }",
            "    String sibling(String o) { return \"quarantine-\" + o; }",
            "}");
        int found = countQuarantineLiterals(synthetic, false)[0];
        assertThat(found).isEqualTo(2);
        assertThat(checkCensus("quarantine- literal", Map.of("Whatever.java", found), Map.of("Whatever.java", 1)))
            .anyMatch(v -> v.contains("REGRESSION"));
        assertThat(checkCensus("quarantine- literal", Map.of("Whatever.java", found), Map.of()))
            .as("a file the census has never seen").anyMatch(v -> v.contains("REGRESSION"));
    }

    @Test
    void quarantineLiteralRemovedWithoutLoweringPin_failsAsStaleFingerprint() {
        int found = countQuarantineLiterals("class W { String s = \"quarantine-\"; }", false)[0];
        assertThat(found).isEqualTo(1);
        assertThat(checkCensus("quarantine- literal", Map.of("W.java", found), Map.of("W.java", 2)))
            .anyMatch(v -> v.contains("STALE FINGERPRINT"));
    }

    @Test
    void commentedOutQuarantineLiterals_areNotCounted_andNeitherAreRoutesOrMessages() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    // private static final String OLD = \"quarantine-\";",
            "    /* String older = \"quarantine-\" + x; */",
            "    static final String ROUTE = \"/v1/vectors/gc/quarantine-restore\";",
            "    static final String MSG = \"not its quarantine- sibling\";",
            "    static final String STATE = \"quarantine\";",
            "    static final String REAL = \"quarantine-\";",
            "}");
        assertThat(countQuarantineLiterals(synthetic, false)[0]).isEqualTo(1);
    }

    @Test
    void theRendererConstantIsNotCounted_butASecondConstantIs() {
        String renderer = "public final class QuarantineOrigin {\n"
            + "    public static final String PREFIX = \"quarantine-\";\n"
            + "}";
        assertThat(countQuarantineLiterals(renderer, true)).as("{counted, exempted}").containsExactly(0, 1);

        String duplicate = "public final class ChunkReaper {\n"
            + "    private static final String QUARANTINE_PREFIX = \"quarantine-\";\n"
            + "}";
        assertThat(countQuarantineLiterals(duplicate, false)).as("a duplicate constant outside the renderer file counts")
            .containsExactly(1, 0);
        assertThat(countQuarantineLiterals(duplicate, true)).as("even in the renderer file, only `String PREFIX` is exempt")
            .containsExactly(1, 0);

        String second = renderer.replace("}", "    static final String OTHER = \"quarantine-x\";\n}");
        assertThat(countQuarantineLiterals(second, true)).as("a second literal in the renderer file counts")
            .containsExactly(1, 1);
    }

    @Test
    void quarantinePrefixReads_countTheConstantsAndThePredicate_notTheirDeclarations() {
        String reader = String.join("\n",
            "class R {",
            "    private static final String QUARANTINE_PREFIX = \"quarantine-\";",
            "    boolean a(String n) { return n.startsWith(QUARANTINE_PREFIX); }",
            "    boolean b(String n) { return QuarantineOrigin.isQuarantineName(n); }",
            "    String c(String n) { return dev.nexus.service.db.QuarantineOrigin.PREFIX + n; }",
            "    // QUARANTINE_PREFIX in a comment, isQuarantineName too",
            "    String other = TAXONOMY_PREFIX + PREFIX;",
            "}");
        assertThat(countQuarantinePrefixReads(reader, false)).as("a, b, c; not the declaration, the comment, "
            + "TAXONOMY_PREFIX or another class's bare PREFIX").isEqualTo(3);

        String renderer = String.join("\n",
            "public final class QuarantineOrigin {",
            "    public static final String PREFIX = \"quarantine-\";",
            "    public static boolean isQuarantineName(String name) { return name != null && name.startsWith(PREFIX); }",
            "    String sibling(String o) { return PREFIX + o; }",
            "    boolean again(String n) { return isQuarantineName(n); }",
            "}");
        assertThat(countQuarantinePrefixReads(renderer, true)).as("PREFIX twice and the predicate once; neither declaration")
            .isEqualTo(3);
        assertThat(countQuarantinePrefixReads(renderer, false)).as("outside the renderer file a bare PREFIX is another constant")
            .isEqualTo(1);
    }

    @Test
    void quarantineReadsAddedBeyondThePin_failAsRegression() {
        assertThat(checkCensus("quarantine- prefix read", Map.of("W.java", 2), Map.of("W.java", 1)))
            .anyMatch(v -> v.contains("REGRESSION"));
        assertThat(checkCensus("quarantine- prefix read", Map.of("W.java", 1), Map.of("W.java", 2)))
            .anyMatch(v -> v.contains("STALE FINGERPRINT"));
    }

    // ── RDR-229 P1.3 review round 1: respellings the first cut missed ───

    @Test
    void sqlQuotedQuarantinePrefix_isCounted_butAMessageOrRouteIsNot() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    static final String A = \"select 1 from t where name like 'quarantine-%'\";",
            "    static final String B = \"starts_with(collection, 'quarantine-')\";",
            "    static final String ROUTE = \"/v1/vectors/gc/quarantine-restore\";",
            "    static final String MSG = \"not its quarantine- sibling\";",
            "    static final String STATE = \"where lifecycle_state = 'quarantine'\";",
            "}");
        assertThat(countQuarantineLiterals(synthetic, false)[0]).isEqualTo(2);
    }

    @Test
    void aTextBlockHoldingTheSqlPrefix_isScanned() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    static final String SQL = \"\"\"",
            "        select 1 from nexus.catalog_collections",
            "         where name like 'quarantine-%'",
            "        \"\"\";",
            "}");
        assertThat(countQuarantineLiterals(synthetic, false)[0]).isEqualTo(1);
    }

    @Test
    void aQuoteInsideATextBlock_doesNotDerailTheLiteralAfterIt() {
        String sameLine = String.join("\n",
            "public final class Whatever {",
            "    static final String T = \"\"\"",
            "        he said \"hi\" // not a comment",
            "        \"\"\" + \"quarantine-\";",
            "}");
        assertThat(countQuarantineLiterals(sameLine, false)[0]).as("the real literal on the closing line").isEqualTo(1);

        String later = String.join("\n",
            "public final class Whatever {",
            "    static final String T = \"\"\"",
            "        one \" two /* not a comment",
            "        \"\"\";",
            "    String sibling(String o) { return \"quarantine-\" + o; }",
            "}");
        assertThat(countQuarantineLiterals(later, false)[0]).as("a literal on a later line").isEqualTo(1);
    }

    @Test
    void aCharLiteralQuote_doesNotDerailTheLiteralAfterIt() {
        String synthetic = "class W { boolean q(char c, String n) { return c == '\"' && n.startsWith(\"quarantine-\"); } }";
        assertThat(countQuarantineLiterals(synthetic, false)[0]).isEqualTo(1);
    }

    @Test
    void quarantineReads_alsoCountRequireNotQuarantineAndTheBypassSet() {
        String reader = String.join("\n",
            "class R {",
            "    void a(String n) { QuarantineOrigin.requireNotQuarantine(\"rename\", n); }",
            "    void b() { for (String p : ChunkKeyPlan.BYPASS_PREFIXES) { use(p); } }",
            "    void c(ChunkKeyPlan plan) { plan.bypassPrefixes().forEach(this::use); }",
            "    void d(List<String> bypassPrefixes) { use(bypassPrefixes); }",
            "}");
        assertThat(countQuarantinePrefixReads(reader, false))
            .as("requireNotQuarantine, BYPASS_PREFIXES and bypassPrefixes(); a parameter named bypassPrefixes is not a read")
            .isEqualTo(3);

        String declarations = String.join("\n",
            "public final class QuarantineOrigin {",
            "    public static void requireNotQuarantine(String verb, String name) { }",
            "}",
            "record ChunkKeyPlan(List<String> bypassPrefixes) {",
            "    public static final List<String> BYPASS_PREFIXES = List.of();",
            "}");
        assertThat(countQuarantinePrefixReads(declarations, false)).as("declarations are not reads").isZero();
    }

    @Test
    void aStaticImportOfPrefix_countsAtEachUse_notAtTheImport() {
        String reader = String.join("\n",
            "import static dev.nexus.service.db.QuarantineOrigin.PREFIX;",
            "class R {",
            "    String a(String o) { return PREFIX + o; }",
            "    boolean b(String n) { return n.startsWith(PREFIX); }",
            "}");
        assertThat(countQuarantinePrefixReads(reader, false)).isEqualTo(2);
        assertThat(countQuarantinePrefixReads(reader.replace("import static dev.nexus.service.db.QuarantineOrigin.PREFIX;",
            "import static dev.nexus.service.db.QuarantineOrigin.*;"), false)).as("a wildcard static import").isEqualTo(2);
        assertThat(countQuarantinePrefixReads(reader.replace("import static dev.nexus.service.db.QuarantineOrigin.PREFIX;", ""), false))
            .as("without the import a bare PREFIX is another class's constant").isZero();
    }

    @Test
    void theDeclarationLookbehinds_toleratePaddingAndLineBreaks() {
        String padded = "class C {\n    private static final String   QUARANTINE_PREFIX = \"x\";\n"
            + "    static boolean\n        isQuarantineName(String n) { return n != null; }\n"
            + "    static void    requireNotQuarantine(String v, String n) { }\n}";
        assertThat(countQuarantinePrefixReads(padded, false)).as("padded declarations are not reads").isZero();
    }
}
