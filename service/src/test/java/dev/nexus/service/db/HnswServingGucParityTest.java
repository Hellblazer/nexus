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
 *
 * <p>nexus-g17tf extends the pairing: every such site must ALSO bound the
 * statement ({@link PgSession#setSearchStatementTimeout}) so an orphaned or
 * pathological scan cancels instead of pinning xmin for hours.
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
                int timeout = count(body, "setSearchStatementTimeout(");
                int planMode = count(body, "setSearchPlanCacheMode(");
                if (p.getFileName().toString().equals("PgSession.java")) {
                    continue; // the definitions themselves
                }
                iterativeSites += iter;
                if (iter != ef || iter != timeout || iter != planMode) {
                    unpaired.add(p.getFileName() + ": " + iter
                        + " iterative_scan site(s) vs " + ef + " setHnswEfSearch call(s) vs "
                        + timeout + " setSearchStatementTimeout call(s) vs "
                        + planMode + " setSearchPlanCacheMode call(s)");
                }
            }
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        assertThat(unpaired)
            .as("files where hnsw.iterative_scan, setHnswEfSearch and setSearchStatementTimeout "
                + "counts diverge — pair them (nexus-4ktfm, nexus-g17tf) or record a deliberate "
                + "exemption here")
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

    /**
     * nexus-g17tf (substantive-critic finding, 2026-09-02): the HNSW-literal
     * pairing above cannot see a vector read that never sets
     * {@code hnsw.iterative_scan} — hybridSearch's selective-gate branch ranks
     * by chash with no HNSW at all, and its gate probe is a trigram
     * heap-recheck. So the bound is ALSO required by shape: every
     * {@code tenantScope.withTenant(} block that performs a raw fetch must set
     * the statement timeout BEFORE its first {@code rawVectorFetch(}.
     */
    @Test
    void everyRawFetchingTenantBlockBoundsItsStatementsFirst() {
        Path root = Path.of("src", "main", "java");
        List<String> unbounded = new ArrayList<>();
        int fetchingBlocks = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                String body;
                try {
                    body = Files.readString(p);
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                }
                for (int at = body.indexOf("tenantScope.withTenant("); at >= 0;
                     at = body.indexOf("tenantScope.withTenant(", at + 1)) {
                    // Window = the rest of the enclosing method (closing brace at
                    // member indent), so a later helper DEFINITION is never read as
                    // a call of the block above it.
                    int end = body.indexOf("\n    }\n", at);
                    String block = body.substring(at, end < 0 ? body.length() : end);
                    int fetch = block.indexOf("rawVectorFetch(");
                    if (fetch < 0) {
                        continue;
                    }
                    fetchingBlocks++;
                    int bound = block.indexOf("setSearchStatementTimeout(");
                    int plan = block.indexOf("setSearchPlanCacheMode(");
                    if (bound < 0 || bound > fetch || plan < 0 || plan > fetch) {
                        unbounded.add(p.getFileName() + " @" + lineOf(body, at));
                    }
                }
            }
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        assertThat(unbounded)
            .as("withTenant blocks that rawVectorFetch without first calling both "
                + "PgSession.setSearchStatementTimeout (nexus-g17tf) and "
                + "PgSession.setSearchPlanCacheMode (nexus-6nkn3)")
            .isEmpty();
        // Non-vacuity: the two raw-SQL search blocks (searchWithTokens and
        // hybridSearch) must be visible. The combined-query paths fetch through
        // jOOQ, not rawVectorFetch, and are covered by the HNSW-literal pairing.
        assertThat(fetchingBlocks)
            .as("raw-fetching withTenant blocks visible to the sweep")
            .isGreaterThanOrEqualTo(2);
    }

    private static int lineOf(String body, int index) {
        int line = 1;
        for (int i = 0; i < index; i++) {
            if (body.charAt(i) == '\n') {
                line++;
            }
        }
        return line;
    }
}
