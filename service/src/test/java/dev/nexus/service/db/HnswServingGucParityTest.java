/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4ktfm — every vector-ranked site that sets
 * {@code hnsw.iterative_scan} must ALSO size the serving
 * {@code hnsw.ef_search} ({@link PgSession#setHnswEfSearch}).
 *
 * <p>The two settings fix DIFFERENT failure classes and neither substitutes
 * for the other: iterative scan covers filtered under-RETURN (starvation),
 * while the ef floor covers cross-tenant crowd-out mis-ranking — neighbors
 * pruned by the ef-bounded traversal are gone before iterative continuation
 * can ever see them (measured live with iterative scan ON: conexus-szjl,
 * v0.1.92 STEP-6). A future HNSW query path that adds the iterative-scan
 * line without the ef line silently reintroduces the crowding class; this
 * source-scan gate (RawSqlGateTest's shape) makes that a compile-adjacent
 * failure instead of a production recall dip.
 */
class HnswServingGucParityTest {

    @Test
    void everyIterativeScanSiteAlsoSizesEfSearch() {
        Path root = Path.of("src", "main", "java");
        List<String> unpaired = new ArrayList<>();
        int iterativeSites = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                String body;
                try {
                    body = Files.readString(p);
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                }
                int iter = count(body, "\"hnsw.iterative_scan\"");
                int ef = count(body, "setHnswEfSearch(");
                if (p.getFileName().toString().equals("PgSession.java")) {
                    continue; // the definitions themselves
                }
                iterativeSites += iter;
                if (iter != ef) {
                    unpaired.add(p.getFileName() + ": " + iter
                        + " iterative_scan site(s) vs " + ef + " setHnswEfSearch call(s)");
                }
            }
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        assertThat(unpaired)
            .as("files where hnsw.iterative_scan and setHnswEfSearch counts diverge — "
                + "pair them (nexus-4ktfm) or record a deliberate exemption here")
            .isEmpty();
        // Non-vacuity: the scan must actually see the known production sites
        // (PgVectorRepository x4 + TaxonomyCentroidRepository x1); a refactor
        // that renames the literal out of visibility must fail here, not pass.
        assertThat(iterativeSites)
            .as("iterative_scan sites visible to the sweep")
            .isGreaterThanOrEqualTo(5);
    }

    private static int count(String haystack, String needle) {
        int n = 0;
        for (int i = haystack.indexOf(needle); i >= 0; i = haystack.indexOf(needle, i + 1)) {
            n++;
        }
        return n;
    }
}
