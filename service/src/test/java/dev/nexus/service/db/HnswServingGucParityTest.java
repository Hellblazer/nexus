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
 *
 * <p>nexus-zrcj7 (2026-09-03): briefly loosened this to a strict-pair-plus-
 * superset relationship when {@code text_gated_search_<dim>}'s single
 * materializing-CTE design replaced {@link PgVectorRepository#hybridSearch}'s
 * HNSW-first branch entirely. RESTORED to the original strict 4-way equality
 * here (coordinator finding, confirmed by EXPLAIN evidence the single-function
 * design did not preserve dense-gate HNSW reachability, T2 nexus/finding-
 * zrcj7-dense-gate-hnsw-not-preserved [24216]): the lcogi/x7z7l selectivity-
 * aware two-branch dispatch is back (vectors-011, {@code
 * text_gate_probe_<dim>} + {@code text_gated_search_hnsw_first_<dim>}), so
 * hybridSearch's {@code withTenant} block sets all four GUCs again, exactly
 * as before.
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
     * REPURPOSED, not retired (nexus-zrcj7, 2026-09-03; coordinator pushback — was
     * {@code everyRawFetchingTenantBlockBoundsItsStatementsFirst}, anchored on the now-
     * deleted {@code rawVectorFetch(} literal). The invariant nexus-g17tf named — every
     * {@code tenantScope.withTenant(} block that performs a vector-ranking FETCH must set
     * {@code setSearchStatementTimeout}/{@code setSearchPlanCacheMode} BEFORE that fetch —
     * still applies to every current fetch shape (jOOQ generated-function-table SELECTs,
     * {@link #exactSelectFrom}'s own supplier, {@code runCombinedQuery}/{@code
     * runCombinedQueryWithChash}'s {@code ctx.selectFrom(fn).fetch()}), so this scans for
     * their two literal anchors instead of the retired wrapper's name. The anchor is
     * {@code .selectFrom(fn)} — the exact identifier {@code fn} every vector-ranking
     * dispatch site binds its switch-selected {@code Table<?>} to — NOT bare {@code
     * .selectFrom(}, which also matches unrelated GC/quarantine call sites
     * ({@code quarantineOrphans}/{@code expireQuarantine}'s {@code ctx.selectFrom(
     * GC_QUARANTINE_ORPHANS.call(...))}) that never need these GUCs and would otherwise
     * false-positive as "unbounded".
     */
    @Test
    void everyFetchingTenantBlockBoundsItsStatementsFirst() {
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
                    int fetch = firstIndexOfAny(block, "exactSelectFrom(", ".selectFrom(fn)");
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
            .as("withTenant blocks that fetch (exactSelectFrom/.selectFrom(fn)) without first "
                + "calling both PgSession.setSearchStatementTimeout (nexus-g17tf) and "
                + "PgSession.setSearchPlanCacheMode (nexus-6nkn3)")
            .isEmpty();
        // Non-vacuity: at least the four known production sites (PgVectorRepository:
        // searchWithTokens/exactSelectFrom, hybridSearch, runCombinedQuery,
        // runCombinedQueryWithChash) must be visible.
        assertThat(fetchingBlocks)
            .as("fetching withTenant blocks visible to the sweep")
            .isGreaterThanOrEqualTo(4);
    }

    private static int firstIndexOfAny(String haystack, String... needles) {
        int best = -1;
        for (String n : needles) {
            int idx = haystack.indexOf(n);
            if (idx >= 0 && (best < 0 || idx < best)) best = idx;
        }
        return best;
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
