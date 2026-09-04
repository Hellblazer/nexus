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
 * pathological scan cancels instead of pinning xmin for hours. "ALSO" is a
 * SUPERSET relationship, not a fourth strict-equality leg (nexus-zrcj7,
 * 2026-09-03): {@code text_gated_search_<dim>}'s (vectors-010) row_number()-
 * over-the-materialized-gate shape never touches the HNSW index by
 * construction, so {@link PgVectorRepository#hybridSearch}'s
 * {@code withTenant} block sets {@code setSearchStatementTimeout}/
 * {@code setSearchPlanCacheMode} but deliberately NOT
 * {@code hnsw.iterative_scan}/{@code setHnswEfSearch} — a real HNSW site
 * still needs BOTH bounds, but a bounded site need not be an HNSW site.
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
                // iter/ef stay a strict PAIR (nexus-4ktfm: neither substitutes for the
                // other at an HNSW site) and timeout/planMode stay a strict PAIR
                // (nexus-6nkn3); nexus-g17tf's "ALSO bound the statement" is a SUPERSET
                // relationship, not a fourth strict-equality leg — timeout/planMode must
                // cover AT LEAST every iter/ef site (every HNSW site is bounded) but may
                // exceed it (a non-HNSW vector-ranking site, e.g. text_gated_search_<dim>'s
                // materializing rank, is bounded without being an HNSW site).
                boolean pairMismatch = iter != ef || timeout != planMode;
                boolean supersetViolated = timeout < iter || planMode < ef;
                if (pairMismatch || supersetViolated) {
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
            .as("files where hnsw.iterative_scan/setHnswEfSearch or "
                + "setSearchStatementTimeout/setSearchPlanCacheMode diverge from each "
                + "other, or where the statement-bound count fails to cover every HNSW "
                + "site — pair them (nexus-4ktfm, nexus-6nkn3) or bound every HNSW site "
                + "(nexus-g17tf) or record a deliberate exemption here")
            .isEmpty();
        // Non-vacuity: the scan must actually see the known production sites
        // (PgVectorRepository x3 -- searchWithTokens, runCombinedQuery,
        // runCombinedQueryWithChash -- + TaxonomyCentroidRepository x1). Was x4/5
        // before nexus-zrcj7 retired hybridSearch's dense-gate HNSW-first branch
        // (text_gated_search_<dim>'s materializing rank never touches the index);
        // a refactor that renames the literal out of visibility must fail here,
        // not pass.
        assertThat(iterativeSites)
            .as("iterative_scan sites visible to the sweep")
            .isGreaterThanOrEqualTo(4);
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
