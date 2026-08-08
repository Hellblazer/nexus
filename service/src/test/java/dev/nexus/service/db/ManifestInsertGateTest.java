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
 * nexus-11gh6 §7d: structural enforcement over {@code catalog_document_chunks}
 * INSERT sites.
 *
 * <p>Two tests, two different strength levels:
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
 *       — the WHOLE {@code src/main/java} tree, BOTH shapes (jOOQ-typed AND
 *       raw-SQL string INSERT). Added post-review (T2 nexus/critique-11gh6-
 *       gate-impl-2026-08-08 [21798] Critical finding, T2 nexus/review-11gh6-
 *       gate-2026-08-08 [21797]): test #1's scope (one file, one jOOQ pattern)
 *       is EXACTLY why {@code StagingPromoteOps.finalizeTenant}'s raw-SQL
 *       {@code INSERT INTO nexus.catalog_document_chunks} was invisible to
 *       both the design's own coverage grep and the original version of this
 *       test — the same overclaim shape round 1 of the design was withdrawn
 *       for, recurring one layer down. A future writer (raw-SQL or jOOQ) in a
 *       NEW file, anywhere in the tree, now fails this test unless it is
 *       named in {@link #ALLOWED_INSERT_SITES} — closing exactly the
 *       structural-test blindness that let both post-review findings hide.</li>
 * </ol>
 *
 * <p>Complements {@code CatalogManifestSweepRepositoryTest}'s §7c behavioural
 * tests (external gate holder, assert each public entry point blocks):
 * these tests make the structure hard to break; those catch it at runtime if
 * someone breaks it anyway.
 */
class ManifestInsertGateTest {

    private static final Pattern JOOQ_INSERT_CALL =
        Pattern.compile("\\.insertInto\\(\\s*CATALOG_DOCUMENT_CHUNKS\\b");

    /** Case-insensitive, whitespace/qualifier tolerant: matches {@code INSERT INTO
     *  catalog_document_chunks} or {@code insert   into   nexus . catalog_document_chunks}
     *  alike — the raw-SQL sibling of {@link #JOOQ_INSERT_CALL}. */
    private static final Pattern RAW_INSERT_CALL =
        Pattern.compile("(?i)insert\\s+into\\s+(nexus\\s*\\.\\s*)?catalog_document_chunks\\b");

    /**
     * Whole-tree allowlist: {@code fileName -> {method names}} permitted to
     * write {@code catalog_document_chunks} via EITHER shape, verified (by
     * brace-region matching, reusing {@link RawSqlGateTest#sanctionedRegions})
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

    @Test
    void everyCatalogDocumentChunksInsertAcrossTheWholeTree_isOnTheAllowlist() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        List<String> violations = new ArrayList<>();
        try (Stream<Path> files = Files.walk(root)) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                try {
                    String fileName = p.getFileName().toString();
                    String blanked = RawSqlGateTest.blank(Files.readString(p));
                    List<int[]> allowedRegions = RawSqlGateTest.sanctionedRegions(
                        blanked, ALLOWED_INSERT_SITES.getOrDefault(fileName, Set.of()));

                    for (Pattern pattern : List.of(JOOQ_INSERT_CALL, RAW_INSERT_CALL)) {
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

        assertThat(violations)
            .as("every catalog_document_chunks INSERT (jOOQ-typed OR raw-SQL, anywhere in "
                + "src/main/java) must be inside a method named in "
                + "ManifestInsertGateTest.ALLOWED_INSERT_SITES, and that site must call "
                + "CatalogRepository.acquireSweepGateShared before it (nexus-11gh6 post-review) "
                + "-- a genuinely NEW writer must be added to the allowlist only AFTER verifying "
                + "it gates; otherwise this is a real regression of the write-skew closure")
            .isEmpty();
    }
}
