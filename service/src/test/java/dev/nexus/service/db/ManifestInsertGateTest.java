// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * nexus-11gh6 / nexus-t76bp: structural enforcement over {@code
 * catalog_document_chunks} write sites — both verbs that can create or
 * re-point a LIVE manifest reference (INSERT and UPDATE), across the whole
 * tree.
 *
 * <p>Three tests across two verbs: INSERT gets two strength levels (single-homing
 * within one file, then a whole-tree allowlist scan); UPDATE gets one (whole-tree
 * allowlist scan only — there is no single-homed UPDATE helper to check against,
 * since three legitimate, independently-reachable UPDATE sites exist by design,
 * not funneled through one door):
 * <ol>
 *   <li>{@link #exactlyOneInsertIntoCatalogDocumentChunksSite} — WITHIN {@code
 *       CatalogRepository.java} only, exactly ONE {@code insertInto(CATALOG_DOCUMENT_CHUNKS}
 *       jOOQ call site, which must be the single-homed, gate-adjacent {@code
 *       insertManifestChunkRows} helper. Rev 1 of this design proposed scanning
 *       each of the four manifest-insert sites for an INLINE gate call — the
 *       substantive-critic correctly flagged that as blind to a future
 *       helper-indirection refactor (T2 nexus/critique-design-11gh6-sweep-gate-
 *       2026-08-08, Significant #3). This test instead asserts an invariant
 *       about STRUCTURE — there is only one door, period.</li>
 *   <li>{@link #everyCatalogDocumentChunksInsertAcrossTheWholeTree_isOnTheAllowlist}
 *       — the WHOLE {@code src/main/java} tree, BOTH INSERT shapes (jOOQ-typed
 *       AND raw-SQL string). Added post-review (T2 nexus/critique-11gh6-
 *       gate-impl-2026-08-08 [21798] Critical finding, T2 nexus/review-11gh6-
 *       gate-2026-08-08 [21797]): test #1's scope (one file, one jOOQ pattern)
 *       is EXACTLY why {@code StagingPromoteOps.finalizeTenant}'s raw-SQL
 *       {@code INSERT INTO nexus.catalog_document_chunks} was invisible to
 *       both the design's own coverage grep and the original version of this
 *       test — the same overclaim shape round 1 of the design was withdrawn
 *       for, recurring one layer down. A future writer (raw-SQL or jOOQ) in a
 *       NEW file, anywhere in the tree, now fails this test unless it is
 *       named in {@link #ALLOWED_INSERT_SITES}.</li>
 *   <li>{@link #everyCatalogDocumentChunksUpdateAcrossTheWholeTree_isOnTheAllowlist}
 *       — the UPDATE-shape sibling of test #2 (nexus-t76bp: an UPDATE that
 *       re-points {@code catalog_document_chunks.chash} — or bulk re-homes its
 *       {@code collection} column — creates or moves a LIVE reference exactly
 *       like an INSERT does, and the whole-tree INSERT scan above is
 *       STRUCTURALLY blind to it by construction (different SQL verb, not
 *       matched by either INSERT pattern). {@code RekeyOps.rekey}'s manifest
 *       cascade UPDATE was exactly this miss — found only by a human-directed
 *       fresh grep, not by any existing test, which is the whole reason this
 *       sibling test exists now. Whole-tree grep re-run for THIS bead (see
 *       {@link #ALLOWED_UPDATE_SITES}'s comment for the exact command and
 *       result) confirms the allowlist below is exhaustive as of 2026-08-08.</li>
 * </ol>
 *
 * <p>Complements the behavioural block-tests in {@code
 * CatalogManifestSweepRepositoryTest}, {@code ChashRepositoryTest}, and
 * {@code StagingPromoteOpsIntegrationTest} (external gate holder, assert each
 * public entry point blocks): these structural tests make it hard to add a
 * NEW ungated site; the behavioural tests catch it at runtime if an EXISTING
 * allowlisted site's gate call is silently removed (a structural scan alone
 * cannot tell "calls the gate" from "doesn't" — only "is textually present in
 * an allowed method or not").
 */
class ManifestInsertGateTest {

    private static final Pattern JOOQ_INSERT_CALL =
        Pattern.compile("\\.insertInto\\(\\s*CATALOG_DOCUMENT_CHUNKS\\b");

    /** Case-insensitive, whitespace/qualifier tolerant: matches {@code INSERT INTO
     *  catalog_document_chunks} or {@code insert   into   nexus . catalog_document_chunks}
     *  alike — the raw-SQL sibling of {@link #JOOQ_INSERT_CALL}. */
    private static final Pattern RAW_INSERT_CALL =
        Pattern.compile("(?i)insert\\s+into\\s+(nexus\\s*\\.\\s*)?catalog_document_chunks\\b");

    /** jOOQ typed UPDATE call against the manifest table — the sibling shape
     *  {@code .insertInto(...)} has for INSERT. Matches {@code
     *  .update(CATALOG_DOCUMENT_CHUNKS)} (jOOQ's fluent update-then-set-then-
     *  where-then-execute chain always opens with {@code .update(TABLE)}). */
    private static final Pattern JOOQ_UPDATE_CALL =
        Pattern.compile("\\.update\\(\\s*CATALOG_DOCUMENT_CHUNKS\\b");

    /** Case-insensitive, whitespace/qualifier tolerant raw-SQL UPDATE —
     *  the raw-SQL sibling of {@link #RAW_INSERT_CALL}. */
    private static final Pattern RAW_UPDATE_CALL =
        Pattern.compile("(?i)update\\s+(nexus\\s*\\.\\s*)?catalog_document_chunks\\b");

    /**
     * Whole-tree allowlist: {@code fileName -> {method names}} permitted to
     * INSERT into {@code catalog_document_chunks} via EITHER shape, verified
     * (by brace-region matching, reusing {@link RawSqlGateTest#sanctionedRegions})
     * to actually contain the match — not merely "this file is trusted."
     * Every entry here is independently confirmed (post-review, 2026-08-08)
     * to call {@code CatalogRepository.acquireSweepGateShared} before its
     * insert. A file/method not listed here that matches either pattern
     * fails {@link #everyCatalogDocumentChunksInsertAcrossTheWholeTree_isOnTheAllowlist}.
     */
    private static final Map<String, Set<String>> ALLOWED_INSERT_SITES = Map.of(
        // The single-homed helper every writer in CatalogRepository.java routes through.
        "CatalogRepository.java", Set.of("insertManifestChunkRows"),
        // RDR-180 land-then-transform's tenant-wide manifest promote — raw SQL,
        // gated per-distinct-target-collection post-review (nexus-11gh6).
        "StagingPromoteOps.java", Set.of("finalizeTenant"));

    /**
     * Whole-tree allowlist for UPDATE shapes (nexus-t76bp). Seeded from a
     * FRESH whole-tree grep run for this bead (2026-08-08), not carried over
     * from the INSERT list or guessed:
     * <pre>
     *   grep -rnoE '\.update\(\s*CATALOG_DOCUMENT_CHUNKS' src/main/java
     *   grep -rniE 'update[[:space:]]+(nexus\.)?catalog_document_chunks' src/main/java
     * </pre>
     * Result: exactly THREE sites, all three confirmed (by reading, this
     * session) to call {@code acquireSweepGateShared} before their UPDATE:
     * <ul>
     *   <li>{@code CatalogRepository.renameCollectionTxn} — bulk re-homes
     *       {@code .collection} into a new scope on its live-target COPY
     *       branch; gated both endpoints since nexus-11gh6 rev 2 §3.2.</li>
     *   <li>{@code ChashRepository.renameCollection} — the second,
     *       independently-reachable rename implementation; gated both
     *       endpoints since nexus-11gh6 post-review (T2 nexus/review-11gh6-
     *       gate-2026-08-08 [21797] Important finding).</li>
     *   <li>{@code RekeyOps.rekey} — the manifest cascade's plain {@code chash}
     *       repoint (step 5); gated per-distinct-target-collection since
     *       nexus-t76bp, 2026-08-08. REWORKED same day (critic-p1 Critical,
     *       T2 nexus/critique-t76bp-rekey-gate-2026-08-08 [21807]): the
     *       resolution now runs BEFORE step 3 (not immediately before step
     *       5), joining the alias map's {@code old_bytes} values against
     *       {@code chunks_384/768/1024} — gating steps 3-4's content
     *       mutations too, not just step 5's manifest UPDATE. See {@code
     *       RekeyOps.rekey}'s own step-(2b) comment for the full
     *       coextensiveness argument and the named orphan-synthesize
     *       residual its step-6 abort-on-nonzero covers instead.</li>
     * </ul>
     * A file/method not listed here that matches either UPDATE pattern fails
     * {@link #everyCatalogDocumentChunksUpdateAcrossTheWholeTree_isOnTheAllowlist}.
     */
    private static final Map<String, Set<String>> ALLOWED_UPDATE_SITES = Map.of(
        "CatalogRepository.java", Set.of("renameCollectionTxn"),
        "ChashRepository.java", Set.of("renameCollection"),
        "RekeyOps.java", Set.of("rekey"));

    @Test
    void exactlyOneInsertIntoCatalogDocumentChunksSite() throws IOException {
        Path path = Path.of("src", "main", "java", "dev", "nexus", "service", "db", "CatalogRepository.java");
        assertThat(path).exists();
        String source = Files.readString(path);
        // Reuse RawSqlGateTest's comment/string blanking so a mention of the
        // call inside a javadoc comment (this class's own file included, but
        // more importantly CatalogRepository.java's own javadoc referencing
        // itself) never counts as a code site.
        String blanked = RawSqlGateTest.blank(source);

        Matcher m = JOOQ_INSERT_CALL.matcher(blanked);
        int count = 0;
        int lastAt = -1;
        while (m.find()) {
            count++;
            lastAt = m.start();
        }
        assertThat(count)
            .as("every catalog_document_chunks insert must route through the single-homed "
                + "insertManifestChunkRows helper (nexus-11gh6 §7d) — found %d call site(s) instead of 1",
                count)
            .isEqualTo(1);

        int methodStart = blanked.indexOf("insertManifestChunkRows(DSLContext");
        assertThat(methodStart)
            .as("insertManifestChunkRows must exist in CatalogRepository.java").isPositive();
        assertThat(lastAt)
            .as("the single insertInto(CATALOG_DOCUMENT_CHUNKS call must be textually inside "
                + "insertManifestChunkRows itself")
            .isGreaterThan(methodStart);
    }

    /** Shared whole-tree scan: every match of any of {@code patterns} in every
     *  {@code .java} file under {@code src/main/java} must fall inside a
     *  method named in {@code allowlist} for that file. Returns the violation
     *  strings (empty means clean). */
    private static List<String> scanWholeTree(List<Pattern> patterns, Map<String, Set<String>> allowlist)
            throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    String fileName = p.getFileName().toString();
                    String blanked = RawSqlGateTest.blank(Files.readString(p));
                    List<int[]> allowedRegions = RawSqlGateTest.sanctionedRegions(
                        blanked, allowlist.getOrDefault(fileName, Set.of()));

                    for (Pattern pattern : patterns) {
                        Matcher m = pattern.matcher(blanked);
                        while (m.find()) {
                            int at = m.start();
                            boolean allowed = allowedRegions.stream()
                                .anyMatch(r -> r[0] <= at && at < r[1]);
                            if (!allowed) {
                                int line = 1 + (int) blanked.substring(0, at).chars()
                                    .filter(c -> c == '\n').count();
                                violations.add(fileName + ":" + line + "  " + m.group().strip());
                            }
                        }
                    }
                } catch (IOException e) {
                    throw new RuntimeException(e);
                }
            });
        }
        return violations;
    }

    @Test
    void everyCatalogDocumentChunksInsertAcrossTheWholeTree_isOnTheAllowlist() throws IOException {
        List<String> violations = scanWholeTree(List.of(JOOQ_INSERT_CALL, RAW_INSERT_CALL), ALLOWED_INSERT_SITES);

        assertThat(violations)
            .as("every catalog_document_chunks INSERT (jOOQ-typed OR raw-SQL, anywhere in "
                + "src/main/java) must be inside a method named in "
                + "ManifestInsertGateTest.ALLOWED_INSERT_SITES, and that site must call "
                + "CatalogRepository.acquireSweepGateShared before it (nexus-11gh6 post-review) "
                + "-- a genuinely NEW writer must be added to the allowlist only AFTER verifying "
                + "it gates; otherwise this is a real regression of the write-skew closure")
            .isEmpty();
    }

    @Test
    void everyCatalogDocumentChunksUpdateAcrossTheWholeTree_isOnTheAllowlist() throws IOException {
        List<String> violations = scanWholeTree(List.of(JOOQ_UPDATE_CALL, RAW_UPDATE_CALL), ALLOWED_UPDATE_SITES);

        assertThat(violations)
            .as("every catalog_document_chunks UPDATE (jOOQ-typed OR raw-SQL, anywhere in "
                + "src/main/java) must be inside a method named in "
                + "ManifestInsertGateTest.ALLOWED_UPDATE_SITES, and that site must call "
                + "CatalogRepository.acquireSweepGateShared before it (nexus-t76bp) -- an UPDATE "
                + "that re-points or bulk re-homes a manifest row is the SAME write-skew hazard "
                + "class as an INSERT, one SQL verb over; a genuinely NEW writer must be added to "
                + "the allowlist only AFTER verifying it gates")
            .isEmpty();
    }
}
