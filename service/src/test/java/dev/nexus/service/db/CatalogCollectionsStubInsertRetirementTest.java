// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 1 (bead nexus-ft04v.7) — regression gate on the stub-insert
 * retirement.
 *
 * <p>Eleven {@code insertInto(CATALOG_COLLECTIONS} call sites existed on develop
 * dd8974496 (204-research-17 finding 5, verified by fix check [24892]): SEVEN
 * stub-insert paths that auto-created a blank-{@code content_type}/{@code
 * owner_id}/{@code embedding_model} row on first write with {@code onConflict()
 * .doNothing()} — {@code AspectRepository}, {@code TaxonomyRepository}, {@code
 * ChashRepository}, {@code StagingPromoteOps}, {@code CombinedWriteService}, and
 * TWO in {@code PgVectorRepository} — and FOUR registration/import paths that
 * carry real, client-supplied attributes and stay: {@code CatalogRepository
 * .upsertCollection}, the rename copy, {@code importCollectionsBatch}, and {@code
 * doImportCollection}.
 *
 * <p>This bead deletes the seven; every retired call site's write now goes
 * through {@link CollectionRegistry#requireRegistered}, which never writes a row
 * — it throws {@link UnregisteredCollectionException} instead. This test proves
 * the deletion mechanically rather than trusting the enumeration to stay true:
 * a future stub-insert reintroduced anywhere under {@code service/src/main/java}
 * fails this count.
 *
 * <p>Non-vacuous by construction (nexus-moht0 doctrine): the walk asserts it
 * visited a substantial slice of the source tree before counting matches, so a
 * misdirected or empty walk fails loud instead of vacuously passing on zero
 * examined files.
 */
class CatalogCollectionsStubInsertRetirementTest {

    private static final Pattern CALL_SITE = Pattern.compile(Pattern.quote("insertInto(CATALOG_COLLECTIONS"));

    @Test
    void exactlyFourSurvivingInsertSites() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).as("service/src/main/java must exist from the test's working directory").exists();

        List<Path> javaFiles = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(root)) {
            walk.filter(p -> p.toString().endsWith(".java")).forEach(javaFiles::add);
        }
        assertThat(javaFiles.size())
            .as("non-vacuity: the walk must visit a substantial slice of "
                + "service/src/main/java (hundreds of files expected) or this "
                + "gate proves nothing about the real source tree")
            .isGreaterThan(50);

        List<String> hits = new ArrayList<>();
        for (Path p : javaFiles) {
            String content = Files.readString(p);
            Matcher m = CALL_SITE.matcher(content);
            while (m.find()) {
                hits.add(root.relativize(p).toString().replace(java.io.File.separatorChar, '/'));
            }
        }

        assertThat(hits)
            .as("insertInto(CATALOG_COLLECTIONS call sites under service/src/main/java — "
                + "the seven RDR-204 stub-insert paths are retired; only the four "
                + "registration/import paths carrying client-supplied attributes "
                + "(CatalogRepository.upsertCollection, the rename copy, "
                + "importCollectionsBatch, doImportCollection) may remain. A hit "
                + "outside dev/nexus/service/db/CatalogRepository.java here means a "
                + "stub-insert path was reintroduced.")
            .hasSize(4)
            .allMatch(f -> f.equals("dev/nexus/service/db/CatalogRepository.java"),
                "every surviving call site must be in CatalogRepository.java");
    }
}
