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

/**
 * House-rule gate (task: end the nexus-mqd6t class; audit finding C-1):
 * every {@code SELECT} read and {@code UPDATE} write against
 * {@code CATALOG_DOCUMENTS} (or one of the four tombstone-aware
 * {@code catalog-019} read-shape views) in {@link CatalogRepository},
 * {@link dev.nexus.service.vectors.PgVectorRepository}, and
 * {@link StagingPromoteOps} must exclude tombstoned rows from its result
 * set, OR sit on a named, rationale-carrying exemption. Same rule for
 * {@code CATALOG_DOCUMENT_CHUNKS} reads (a tombstoned parent's chunks must
 * not leak through a chunk-level query either — the nexus-mqd6t BUG 1/2
 * shape). A fourth, narrower check covers the D-5/eldyi
 * resurrection-write class: any {@code .set(CATALOG_DOCUMENTS.DELETED_AT,
 * ...)} must either self-guard via a {@code DELETED_AT.isNull()} WHERE in
 * the same statement (the tombstone/idempotent-restore writers) or sit on
 * a named exemption (the ONE sanctioned un-tombstone, {@code
 * upsertDocument}).
 *
 * <p>Model: {@link RawSqlGateTest} (same package) for the comment/string
 * neutralizer ({@link RawSqlGateTest#blank}) and the brace-depth
 * method-region attribution ({@link RawSqlGateTest#sanctionedRegions}),
 * reused directly rather than re-implemented — see that class's nexus-8kbzu
 * history for why brace-depth truth replaced regex declaration heuristics.
 * Crossed with {@code tests/test_changelog_rls_lint.py}'s rot-proofing
 * discipline: a rationale-carrying, tuple-shaped allowlist; a zero-violations
 * AND full-allowlist-consumption assertion (both {@code ==}, never {@code
 * >=}); explicit non-vacuity floors on the walked corpus; and a meta-test of
 * the rot detector itself (an allowlist entry that matches nothing in a live
 * scan must be caught, not silently accepted).
 *
 * <p><b>Statement-level, not method-level, attribution.</b> The excusal
 * check is scoped to the single jOOQ fluent statement carrying the
 * {@code select}/{@code update} initiator (paren-matched, via {@link
 * #splitStatements}), never "does the enclosing method contain the filter
 * token anywhere." {@code coverageByContentType} (nexus-l1nre) is the ground
 * truth for why: before the catalog-019 view fix, its owner-prefix branch
 * filtered {@code deleted_at IS NULL} by hand while its view branch did not
 * — a method-level scan would have seen the filter token present *somewhere*
 * in the method and silently passed both branches. Both branches now filter
 * (the view itself is tombstone-aware post catalog-019), so that method can
 * no longer serve as a live regression fixture; {@link
 * #attribution_twoBranchesOneUnfiltered_methodLevelWouldFalsePass} is the
 * synthetic replacement, run directly against {@link #scanDocAndChunkSites}.
 *
 * <p>Two curated, narrow escape hatches, each manually verified against the
 * current tree (never a blanket fallback — a blanket "widen on failure"
 * would silently launder exactly the coverageByContentType shape above):
 * <ul>
 *   <li>{@link #TOMBSTONE_EXEMPT} — the statement's own filter is genuinely
 *       absent and stays absent by design (rationale-as-data, in-code {@code
 *       // TOMBSTONE-EXEMPT} marker at the definition site).</li>
 *   <li>{@link #WIDEN} — the filter genuinely IS present, just in a
 *       different Java statement than the initiator (an imperative
 *       SET-loop, or a precomputed {@code Condition} variable applied
 *       later) — the check widens to the enclosing method, which is safe
 *       ONLY because each entry was hand-verified to hold exactly one
 *       query in scope (no sibling branch to launder).</li>
 * </ul>
 *
 * <p>A tombstone-aware view ({@code catalog_stats}, {@code
 * collection_doc_counts}, {@code collection_health_meta}, {@code
 * coverage_by_content_type} — all four predicate-fixed by changeset
 * catalog-019) bakes {@code deleted_at IS NULL} into its own SQL definition;
 * selecting FROM one structurally satisfies this gate without a Java-side
 * predicate, recognized via {@link #VIEW_TOKENS}.
 *
 * <p>{@code catalog_links} reads: dormant by design. No writer sets a
 * link-level tombstone column today, so there is currently nothing for this
 * gate to require here — noted per the spec, not enforced as a consumed
 * allowlist entry (an entry that can never fire is exactly the un-falsifiable
 * rot class {@link #test_realTree_zeroUnusedExemptEntries} exists to
 * reject). Delete this note the day a writer sets one.
 */
class TombstoneFilterGateTest {

    // ── Corpus ──────────────────────────────────────────────────────────────

    private static final Map<String, Path> TARGET_FILES = Map.of(
        "CatalogRepository.java",
            Path.of("src", "main", "java", "dev", "nexus", "service", "db", "CatalogRepository.java"),
        "PgVectorRepository.java",
            Path.of("src", "main", "java", "dev", "nexus", "service", "vectors", "PgVectorRepository.java"),
        "StagingPromoteOps.java",
            Path.of("src", "main", "java", "dev", "nexus", "service", "db", "StagingPromoteOps.java")
    );

    // ── Tokens ──────────────────────────────────────────────────────────────

    private static final String CATALOG_DOCUMENTS = "CATALOG_DOCUMENTS";
    private static final String CATALOG_DOCUMENT_CHUNKS = "CATALOG_DOCUMENT_CHUNKS";

    /** catalog-019: predicate-fixed read-shape views — deleted_at IS NULL is
     * baked into the view's own SQL, not the Java call site. */
    private static final List<String> VIEW_TOKENS = List.of(
        "CATALOG_STATS", "COLLECTION_DOC_COUNTS", "COLLECTION_HEALTH_META", "COVERAGE_BY_CONTENT_TYPE");

    private static final List<String> SELECT_INITIATORS = List.of(
        ".select(", ".selectFrom(", ".selectCount(", ".selectDistinct(");
    private static final List<String> UPDATE_INITIATORS = List.of(".update(");

    /** Any one of these, present in scope, excuses a read/update site. */
    private static final List<String> FILTER_TOKENS = List.of(
        "DELETED_AT.isNull(", "liveParentDoc(", "liveDocument(", "documentExists(");

    // ── Allowlists (rationale-as-data) ──────────────────────────────────────

    record ExemptEntry(String file, String method, String rationale) {}

    /**
     * Statements whose filter is genuinely, deliberately absent. Each entry's
     * definition site carries a {@code // TOMBSTONE-EXEMPT (nexus-mqd6t):}
     * comment restating this rationale (auditable in the source, not only
     * here). Only entries that actually suppress a would-be violation belong
     * here — {@link #test_realTree_zeroUnusedExemptEntries} enforces that an
     * entry matching nothing in a live scan is rot, not documentation.
     *
     * <p>Deliberately NOT here despite being named in the originating audit:
     * {@code relationCounts} builds its target table dynamically ({@code
     * DSL.table(DSL.name(...))} from a caller-supplied relation string) —
     * the literal {@code CATALOG_DOCUMENTS} token never appears in that
     * method, so it is structurally outside this token-based gate's reach,
     * not a suppressed violation. {@code deleteDocument} / {@code
     * deleteDocumentsMany} already carry {@code DELETED_AT.isNull()} in the
     * SAME statement as their {@code .update(CATALOG_DOCUMENTS)} initiator
     * (idempotent re-tombstone guard, not this gate's read-visibility
     * concern) — they pass on their own merits and would be an unfalsifiable
     * always-unused entry if listed. Both still carry an in-code note for
     * human readers; see their definition sites.
     */
    private static final List<ExemptEntry> TOMBSTONE_EXEMPT = List.of(
        new ExemptEntry("CatalogRepository.java", "highestChildSeq",
            "tumbler allocator: the tumbler PK does not exclude tombstones, and filtering "
            + "would re-issue an already-taken child sequence number to a NEW document"),
        new ExemptEntry("CatalogRepository.java", "physicalCollectionOf",
            "manifest WRITE-path helper (nexus-x6kdz): its only callers are the manifest "
            + "writers (writeManifestRows/appendManifestChunks/the import-chunk paths), never "
            + "a reader — filtering here would silently stamp NULL onto rows being written for "
            + "a tombstoned document on the ETL/import leg, re-introducing the silent-empty "
            + "class the nexus-x6kdz stamp exists to fix"),
        new ExemptEntry("CatalogRepository.java", "upsertDocument",
            "the ONE sanctioned way a tombstoned row is revived (nexus-mqd6t Hal ruling): an "
            + "explicit re-register clears deleted_at via the ON CONFLICT DO UPDATE arm, "
            + "deliberately unconditional — updateDocument refuses tombstoned targets outright "
            + "so an incidental field write can never resurrect one"),
        new ExemptEntry("CatalogRepository.java", "manifestRowCount",
            "A-1 constraint: a correlated SELECT count(*) used ONLY as a SET expression inside "
            + "an already tombstone-guarded UPDATE (the enclosing update's own WHERE carries "
            + "DELETED_AT.isNull() — see buildUpdateDocumentQuery/writeManifestRows/"
            + "appendManifestChunks) — filtering this subquery in isolation would silently zero "
            + "the count instead of failing loud on the guarded D-5/eldyi paths"),
        new ExemptEntry("CatalogRepository.java", "renameCollectionTxn",
            "collection rename must repoint ALL documents under the old physical_collection "
            + "name, tombstoned or not — a tombstoned document's physical_collection has to "
            + "track the collection's CURRENT name so a later restore does not resurrect it "
            + "pointing at a retired name (found during gate authorship; undocumented before "
            + "this — see the in-code marker at the call site for the fix)"),
        new ExemptEntry("PgVectorRepository.java", "fetchDocumentChunks",
            "the manifest chunk read is gated by the PRECEDING live-document existence check "
            + "in the same method: a tombstoned or unknown tumbler throws IllegalStateException "
            + "before this select ever executes (found during gate authorship; undocumented "
            + "before this)"),
        new ExemptEntry("StagingPromoteOps.java", "finalizeTenant",
            "RDR-180 land-then-transform migration leg (nexus-jxizy.10.3/10.4) — same sanction "
            + "class as RawSqlGateTest's raw-SQL allowance for this file: one-shot migration "
            + "statements over a landing zone, never serving-path")
    );

    record WidenEntry(String file, String method, String rationale) {}

    /**
     * Methods where the filter genuinely IS present, just split across
     * Java statements from the select/update initiator (an imperative
     * SET-loop, or a precomputed {@code Condition} applied later) — the
     * check widens to the whole enclosing method. Each entry is manually
     * verified to hold exactly ONE query in scope; this is NOT a blanket
     * "retry at method scope on failure" (that would silently launder the
     * coverageByContentType shape — see the class javadoc).
     */
    private static final List<WidenEntry> WIDEN = List.of(
        new WidenEntry("CatalogRepository.java", "buildUpdateDocumentQuery",
            "imperative SET-loop: ctx.update(CATALOG_DOCUMENTS) and its terminal "
            + ".where(...DELETED_AT.isNull()...) are different Java statements — the SET "
            + "clause is built across a loop over the caller's field map"),
        new WidenEntry("CatalogRepository.java", "searchDocuments",
            "the WHERE Condition is assembled into a local variable in an earlier statement "
            + "(folding the FTS match with DELETED_AT.isNull()) and applied via .where(where) "
            + "in the initiator's own statement")
    );

    private static List<String> exemptNames(String file) {
        List<String> names = new ArrayList<>();
        for (var e : TOMBSTONE_EXEMPT) if (e.file().equals(file)) names.add(e.method());
        return names;
    }

    private static List<String> widenNames(String file) {
        List<String> names = new ArrayList<>();
        for (var w : WIDEN) if (w.file().equals(file)) names.add(w.method());
        return names;
    }

    // ── Statement splitting (brace-stack aware) ─────────────────────────────

    /**
     * Top-level Java statement spans: split on {@code ;} when the running
     * paren depth is 0 RELATIVE TO THE CURRENT BRACE BLOCK. A {@code {}
     * pushes the running paren depth and resets it to 0, and a matching
     * {@code }} restores it — otherwise a lambda block passed as a call
     * argument ({@code tenantScope.withTenant(tenant, ctx -> { A(); B(); })})
     * would glue A();/B(); into one giant span running from {@code
     * withTenant(} to the final {@code );}, since the outer call's own
     * paren never closes until after the block. Each inner statement must
     * split on its own, independent of how deeply the ENCLOSING call is
     * itself paren-nested.
     */
    static List<int[]> splitStatements(String blanked) {
        List<int[]> spans = new ArrayList<>();
        int depth = 0;
        var stack = new ArrayList<Integer>();
        int start = 0;
        for (int i = 0; i < blanked.length(); i++) {
            char c = blanked.charAt(i);
            if (c == '(') {
                depth++;
            } else if (c == ')') {
                depth--;
            } else if (c == '{') {
                stack.add(depth);
                depth = 0;
            } else if (c == '}') {
                if (!stack.isEmpty()) depth = stack.remove(stack.size() - 1);
            } else if (c == ';' && depth == 0) {
                spans.add(new int[] {start, i + 1});
                start = i + 1;
            }
        }
        if (start < blanked.length()) spans.add(new int[] {start, blanked.length()});
        return spans;
    }

    private static int lineOf(String text, int pos) {
        int line = 1;
        for (int i = 0; i < pos && i < text.length(); i++) if (text.charAt(i) == '\n') line++;
        return line;
    }

    /** A brace-depth region attributed to the method NAME that produced it —
     * {@link RawSqlGateTest#sanctionedRegions} returns bare {@code int[]}
     * spans for a whole Set of names at once with no name attached, so this
     * gate (which needs to report WHICH exempt method excused a site, for
     * the consumption/rot check) computes one region-set PER NAME and tags
     * it, rather than reusing the combined form. Same underlying brace-depth
     * algorithm either way — {@link RawSqlGateTest#sanctionedRegions} is
     * called once per name below, not reimplemented. */
    record NamedRegion(String method, int start, int end) {}

    private static List<NamedRegion> namedRegions(String blanked, List<String> names) {
        List<NamedRegion> out = new ArrayList<>();
        for (String name : names) {
            for (int[] r : RawSqlGateTest.sanctionedRegions(blanked, Set.of(name))) {
                out.add(new NamedRegion(name, r[0], r[1]));
            }
        }
        return out;
    }

    private static String methodAt(List<NamedRegion> regions, int pos) {
        for (NamedRegion r : regions) if (r.start() <= pos && pos < r.end()) return r.method();
        return null;
    }

    // ── Findings ─────────────────────────────────────────────────────────────

    record Finding(String file, String kind, int line, String snippet, boolean excused, String exemptMethod) {}

    /** Every candidate site examined (excused or not) — the corpus this gate
     * actually walked, for the non-vacuity floors. */
    record ScanResult(List<Finding> docSites, List<Finding> chunkSites,
                       List<Finding> setDeletedSites, List<Finding> rawSqlSites) {}

    static ScanResult scan() throws IOException {
        List<Finding> docSites = new ArrayList<>();
        List<Finding> chunkSites = new ArrayList<>();
        List<Finding> setDeletedSites = new ArrayList<>();
        List<Finding> rawSqlSites = new ArrayList<>();

        for (var entry : TARGET_FILES.entrySet()) {
            String fname = entry.getKey();
            String src = Files.readString(entry.getValue());
            String blanked = RawSqlGateTest.blank(src);
            List<NamedRegion> exemptRegions = namedRegions(blanked, exemptNames(fname));
            List<NamedRegion> widenRegions = namedRegions(blanked, widenNames(fname));

            scanDocAndChunkSites(fname, blanked, exemptRegions, widenRegions, docSites, chunkSites);
            scanSetDeletedSites(fname, blanked, exemptRegions, setDeletedSites);
            if (fname.equals("StagingPromoteOps.java")) {
                scanRawSqlSites(fname, src, blanked, exemptRegions, rawSqlSites);
            }
        }
        return new ScanResult(docSites, chunkSites, setDeletedSites, rawSqlSites);
    }

    /** Exposed separately for the statement-vs-method-level meta-test, which
     * runs this against SYNTHETIC sources rather than the real tree. */
    static void scanDocAndChunkSites(String fname, String blanked, List<NamedRegion> exemptRegions,
                                      List<NamedRegion> widenRegions, List<Finding> docSites, List<Finding> chunkSites) {
        for (int[] span : splitStatements(blanked)) {
            String stmt = blanked.substring(span[0], span[1]);
            boolean hasSelectInit = containsAny(stmt, SELECT_INITIATORS);
            boolean hasUpdateInit = containsAny(stmt, UPDATE_INITIATORS);
            if (!hasSelectInit && !hasUpdateInit) continue;

            boolean refsDoc = stmt.contains(CATALOG_DOCUMENTS) || containsAny(stmt, VIEW_TOKENS);
            boolean refsChunks = stmt.contains(CATALOG_DOCUMENT_CHUNKS) && hasSelectInit;
            if (!refsDoc && !refsChunks) continue;

            int initPos = firstIndexOfAny(stmt, joinLists(SELECT_INITIATORS, UPDATE_INITIATORS));
            int sitePos = span[0] + Math.max(initPos, 0);
            int line = lineOf(blanked, sitePos);
            String snippet = snippet(stmt, initPos);

            String checkText = stmt;
            for (NamedRegion wr : widenRegions) {
                if (wr.start() <= sitePos && sitePos < wr.end()) {
                    checkText = blanked.substring(wr.start(), wr.end());
                    break;
                }
            }

            if (refsDoc) {
                boolean hasFilter = containsAny(checkText, FILTER_TOKENS);
                String compact = stmt.replace(" ", "").replace("\n", "");
                boolean viewFiltered = false;
                for (String v : VIEW_TOKENS) {
                    if (compact.contains(".from(" + v) || compact.contains(".selectFrom(" + v)) {
                        viewFiltered = true;
                        break;
                    }
                }
                boolean passed = hasFilter || viewFiltered;
                String exemptMethod = passed ? null : methodAt(exemptRegions, sitePos);
                docSites.add(new Finding(fname, "doc", line, snippet, passed || exemptMethod != null, exemptMethod));
            }
            if (refsChunks) {
                boolean hasFilter = containsAny(checkText, FILTER_TOKENS);
                String exemptMethod = hasFilter ? null : methodAt(exemptRegions, sitePos);
                chunkSites.add(new Finding(fname, "chunk", line, snippet, hasFilter || exemptMethod != null, exemptMethod));
            }
        }
    }

    private static final Pattern SET_DELETED_AT =
        Pattern.compile("\\.set\\(\\s*CATALOG_DOCUMENTS\\.DELETED_AT\\s*,");

    static void scanSetDeletedSites(String fname, String blanked, List<NamedRegion> exemptRegions, List<Finding> out) {
        List<int[]> spans = splitStatements(blanked);
        Matcher m = SET_DELETED_AT.matcher(blanked);
        List<Integer> seenLines = new ArrayList<>();
        while (m.find()) {
            int pos = m.start();
            int line = lineOf(blanked, pos);
            if (seenLines.contains(line)) continue;
            seenLines.add(line);
            String stmt = "";
            for (int[] span : spans) {
                if (span[0] <= pos && pos < span[1]) {
                    stmt = blanked.substring(span[0], span[1]);
                    break;
                }
            }
            boolean selfGuarded = stmt.contains("DELETED_AT.isNull(");
            String exemptMethod = selfGuarded ? null : methodAt(exemptRegions, pos);
            String snippet = blanked.substring(pos, Math.min(pos + 60, blanked.length())).replace('\n', ' ').trim();
            out.add(new Finding(fname, "set_deleted", line, snippet, selfGuarded || exemptMethod != null, exemptMethod));
        }
    }

    private static final Pattern CATALOG_DOCUMENTS_RAW =
        Pattern.compile("catalog_documents", Pattern.CASE_INSENSITIVE);

    /**
     * Raw-SQL string sub-scan, restricted to {@code StagingPromoteOps.java}:
     * {@link RawSqlGateTest#SANCTIONED_METHODS} proves {@code
     * CatalogRepository.java} / {@code PgVectorRepository.java} contain no
     * raw string-SQL execute()/fetch() at all — a {@code "catalog_documents"}
     * match inside a STRING literal there is {@code DSL.name(...)} /
     * error-message text, not executed SQL, and would be pure noise here.
     * Reads a STRING-LITERAL-ONLY view of the source (the inverse of {@link
     * RawSqlGateTest#blank}: keep string/char contents, blank out code and
     * comments) so a prose mention in a javadoc comment cannot masquerade as
     * a real SQL reference — the exact comment/string non-excusal discipline
     * this gate's meta-test enforces in the other direction.
     */
    static void scanRawSqlSites(String fname, String src, String blanked, List<NamedRegion> exemptRegions, List<Finding> out) {
        String strView = stringLiteralView(src);
        Matcher m = CATALOG_DOCUMENTS_RAW.matcher(strView);
        while (m.find()) {
            int pos = m.start();
            int line = lineOf(blanked, pos);
            String exemptMethod = methodAt(exemptRegions, pos);
            String snippet = strView.substring(Math.max(0, pos - 40), Math.min(strView.length(), pos + 40))
                .replace('\n', ' ').trim();
            out.add(new Finding(fname, "raw_sql", line, snippet, exemptMethod != null, exemptMethod));
        }
    }

    /** Inverse of {@link RawSqlGateTest#blank}: blank out code and comments,
     * KEEP string/char literal contents (with delimiters) — length- and
     * newline-preserving, same offset contract as {@code blank()}. */
    static String stringLiteralView(String src) {
        char[] out = src.toCharArray();
        int i = 0;
        int n = out.length;
        while (i < n) {
            char c = out[i];
            if (c == '/' && i + 1 < n && out[i + 1] == '*') {
                int end = src.indexOf("*/", i + 2);
                end = (end < 0) ? n : end + 2;
                for (int j = i; j < end; j++) if (out[j] != '\n') out[j] = ' ';
                i = end;
            } else if (c == '/' && i + 1 < n && out[i + 1] == '/') {
                while (i < n && out[i] != '\n') out[i++] = ' ';
            } else if (c == '"' || c == '\'') {
                char q = c;
                out[i] = ' ';
                i++;
                while (i < n && out[i] != q) {
                    if (src.charAt(i) == '\\' && i + 1 < n) i++;
                    i++;
                }
                if (i < n) out[i] = ' ';  // closing quote blanked too (content-only view)
                i++;
            } else {
                if (out[i] != '\n') out[i] = ' ';
                i++;
            }
        }
        return new String(out);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static boolean containsAny(String haystack, List<String> needles) {
        for (String n : needles) if (haystack.contains(n)) return true;
        return false;
    }

    private static int firstIndexOfAny(String haystack, List<String> needles) {
        int best = -1;
        for (String n : needles) {
            int idx = haystack.indexOf(n);
            if (idx >= 0 && (best < 0 || idx < best)) best = idx;
        }
        return best;
    }

    private static List<String> joinLists(List<String> a, List<String> b) {
        List<String> out = new ArrayList<>(a);
        out.addAll(b);
        return out;
    }

    private static String snippet(String stmt, int initPos) {
        int from = Math.max(0, initPos - 20);
        int to = Math.min(stmt.length(), initPos + 100);
        return stmt.substring(from, to).replaceAll("\\s+", " ").trim();
    }

    // ── The gate ─────────────────────────────────────────────────────────────

    @Test
    void noUnguardedTombstoneReadsOrWrites() throws IOException {
        var result = scan();
        var violations = new ArrayList<Finding>();
        var consumed = new java.util.LinkedHashSet<String>();  // "file::method"

        for (var f : List.of(result.docSites(), result.chunkSites(), result.setDeletedSites(), result.rawSqlSites())) {
            for (Finding fnd : f) {
                if (!fnd.excused()) violations.add(fnd);
                if (fnd.exemptMethod() != null) consumed.add(fnd.file() + "::" + fnd.exemptMethod());
            }
        }

        assertThat(violations)
            .as("unguarded CATALOG_DOCUMENTS/CATALOG_DOCUMENT_CHUNKS read or write "
                + "(missing DELETED_AT.isNull()/liveParentDoc()/liveDocument()/documentExists() "
                + "or an unguarded resurrection SET) — filter it, or add a named, rationale-"
                + "carrying TOMBSTONE_EXEMPT entry: %s", violations)
            .isEmpty();

        var allExemptKeys = new java.util.LinkedHashSet<String>();
        for (var e : TOMBSTONE_EXEMPT) allExemptKeys.add(e.file() + "::" + e.method());
        var unused = new java.util.LinkedHashSet<>(allExemptKeys);
        unused.removeAll(consumed);
        assertThat(unused)
            .as("TOMBSTONE_EXEMPT entries not reproduced by a live scan (rot — the site was "
                + "fixed/removed, or the scan's classification regressed): %s", unused)
            .isEmpty();
    }

    // ── Non-vacuity floors (re-counted on the final tree; see class javadoc) ──

    @Test
    void floor_catalogDocumentsOrViewSites() throws IOException {
        var result = scan();
        assertThat(result.docSites().size())
            .as("CATALOG_DOCUMENTS/view-token sites walked — floor re-counted on the final "
                + "tree; a collapse below this suggests the scan silently stopped matching")
            .isGreaterThanOrEqualTo(51);
    }

    @Test
    void floor_catalogDocumentChunksReadSites() throws IOException {
        var result = scan();
        assertThat(result.chunkSites().size()).isGreaterThanOrEqualTo(9);
    }

    @Test
    void floor_setDeletedAtSites() throws IOException {
        // 3 distinct .set(CATALOG_DOCUMENTS.DELETED_AT, ...) call sites on the
        // final tree: upsertDocument's un-tombstone, deleteDocument's
        // tombstone, deleteDocumentsMany's batch tombstone.
        var result = scan();
        assertThat(result.setDeletedSites().size()).isGreaterThanOrEqualTo(3);
    }

    @Test
    void floor_stagingPromoteOpsRawSqlSites() throws IOException {
        var result = scan();
        assertThat(result.rawSqlSites().size()).isGreaterThanOrEqualTo(2);
    }

    @Test
    void floor_liveParentDocCallSites() throws IOException {
        int count = 0;
        Pattern p = Pattern.compile("\\bliveParentDoc\\s*\\(\\s*ctx\\s*,\\s*tenant\\s*\\)");
        for (var entry : TARGET_FILES.entrySet()) {
            String src = Files.readString(entry.getValue());
            Matcher m = p.matcher(RawSqlGateTest.blank(src));
            while (m.find()) count++;
        }
        assertThat(count)
            .as("liveParentDoc(ctx, tenant) USAGE call sites (excludes its own definition, "
                + "which takes the same two args but is declared, not called)")
            .isGreaterThanOrEqualTo(5);
    }

    // ── Meta-tests: statement-level vs method-level attribution ─────────────

    /**
     * The coverageByContentType-shaped bug, reproduced synthetically since
     * the real fixture no longer exhibits it (catalog-019 fixed the view
     * branch). A method-level scan ("does the filter token appear ANYWHERE
     * in this method") would see branch A's filter and silently pass branch
     * B too. Statement-level attribution must flag branch B alone.
     */
    @Test
    void attribution_twoBranchesOneUnfiltered_methodLevelWouldFalsePass() {
        String synthetic = String.join("\n",
            "public final class CatalogRepository {",
            "    private Map<String, Object> coverageByContentTypeSynthetic(DSLContext ctx, String tenant, String ownerPrefix) {",
            "        if (ownerPrefix != null) {",
            "            var a = ctx.select(CATALOG_DOCUMENTS.CONTENT_TYPE)",
            "                       .from(CATALOG_DOCUMENTS)",
            "                       .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))",
            "                       .fetch();",
            "            return Map.of();",
            "        }",
            "        var b = ctx.select(CATALOG_DOCUMENTS.CONTENT_TYPE)",
            "                   .from(CATALOG_DOCUMENTS)",
            "                   .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))",
            "                   .fetch();",
            "        return Map.of();",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        var docSites = new ArrayList<Finding>();
        var chunkSites = new ArrayList<Finding>();
        scanDocAndChunkSites("Synthetic.java", blanked, List.of(), List.of(), docSites, chunkSites);

        assertThat(docSites).hasSize(2);
        long unexcused = docSites.stream().filter(f -> !f.excused()).count();
        assertThat(unexcused)
            .as("exactly ONE branch (the unfiltered one) must be flagged — a method-level scan "
                + "would see branch A's filter token and false-pass both: %s", docSites)
            .isEqualTo(1);
        assertThat(docSites.stream().filter(f -> !f.excused()).findFirst().orElseThrow().line())
            .as("the FLAGGED site must be the unfiltered branch (b), not the filtered one (a)")
            .isGreaterThan(docSites.stream().filter(Finding::excused).findFirst().orElseThrow().line());
    }

    /** {@code DELETED_AT.isNull()} appearing only inside a comment or a
     * string literal (not real code) must not excuse a real violation — the
     * file is dense with {@code {@code deleted_at IS NULL}} javadoc prose. */
    @Test
    void attribution_filterTokenInCommentOrString_doesNotExcuse() {
        String synthetic = String.join("\n",
            "public final class Synthetic {",
            "    /** Every read here filters {@code DELETED_AT.isNull()} by convention. */",
            "    private void unfiltered(DSLContext ctx, String tenant) {",
            "        String label = \"DELETED_AT.isNull() -- not real code\";",
            "        var rows = ctx.select(CATALOG_DOCUMENTS.TUMBLER)",
            "                      .from(CATALOG_DOCUMENTS)",
            "                      .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))",
            "                      .fetch();",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        var docSites = new ArrayList<Finding>();
        var chunkSites = new ArrayList<Finding>();
        scanDocAndChunkSites("Synthetic.java", blanked, List.of(), List.of(), docSites, chunkSites);

        assertThat(docSites).hasSize(1);
        assertThat(docSites.get(0).excused())
            .as("a comment/string mentioning the filter predicate must not excuse a real, "
                + "unfiltered read — blank() neutralizes comments/strings before the scan runs")
            .isFalse();
    }

    // ── Meta-tests: adversarial exempt-region attribution (nexus-8kbzu shapes,
    // mirrored from RawSqlGateTest since both reuse the same brace-depth
    // algorithm — proving OUR scan pipeline inherits the same correctness) ──

    @Test
    void attribution_nestedClassAfterExemptMethod_isStillFlagged() {
        String synthetic = String.join("\n",
            "public final class CatalogRepository {",
            "    private static String physicalCollectionOf(DSLContext ctx, String tenant, String docId) {",
            "        String pc = ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)",
            "                       .from(CATALOG_DOCUMENTS)",
            "                       .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))",
            "                       .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION);",
            "        return pc;",
            "    }",
            "    static class Sneaky {",
            "        void hide(DSLContext ctx, String tenant) {",
            "            ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)",
            "               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).fetch();",
            "        }",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        List<NamedRegion> exempt = namedRegions(blanked, List.of("physicalCollectionOf"));
        var docSites = new ArrayList<Finding>();
        var chunkSites = new ArrayList<Finding>();
        scanDocAndChunkSites("CatalogRepository.java", blanked, exempt, List.of(), docSites, chunkSites);

        assertThat(docSites).hasSize(2);
        assertThat(docSites.stream().filter(f -> !f.excused()).count())
            .as("the nested class's own unfiltered read must NOT inherit physicalCollectionOf's "
                + "exemption: %s", docSites)
            .isEqualTo(1);
    }

    @Test
    void attribution_packagePrivateMethodAfterExemptMethod_resetsExemption() {
        String synthetic = String.join("\n",
            "public final class CatalogRepository {",
            "    private static String physicalCollectionOf(DSLContext ctx, String tenant, String docId) {",
            "        String pc = ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)",
            "                       .from(CATALOG_DOCUMENTS)",
            "                       .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))",
            "                       .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION);",
            "        return pc;",
            "    }",
            "    List<String> plainMethod(DSLContext ctx, String tenant) {",
            "        return ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)",
            "                  .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).fetch()",
            "                  .map(r -> r.value1());",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        List<NamedRegion> exempt = namedRegions(blanked, List.of("physicalCollectionOf"));
        var docSites = new ArrayList<Finding>();
        var chunkSites = new ArrayList<Finding>();
        scanDocAndChunkSites("CatalogRepository.java", blanked, exempt, List.of(), docSites, chunkSites);

        assertThat(docSites.stream().filter(f -> !f.excused()).count()).isEqualTo(1);
    }

    @Test
    void attribution_exemptMethodItself_isExcused() {
        String synthetic = String.join("\n",
            "public final class CatalogRepository {",
            "    private static long highestChildSeq(DSLContext ctx, String tenant, String ownerPrefix) {",
            "        Long max = ctx.select(DSL.field(\"1\"))",
            "                      .from(CATALOG_DOCUMENTS)",
            "                      .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))",
            "                      .fetchOne(0, Long.class);",
            "        return max == null ? 0L : max;",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        List<NamedRegion> exempt = namedRegions(blanked, List.of("highestChildSeq"));
        var docSites = new ArrayList<Finding>();
        var chunkSites = new ArrayList<Finding>();
        scanDocAndChunkSites("CatalogRepository.java", blanked, exempt, List.of(), docSites, chunkSites);

        assertThat(docSites).hasSize(1);
        assertThat(docSites.get(0).excused()).isTrue();
    }

    // ── Meta-test: the rot detector itself ──────────────────────────────────

    /**
     * Direct test of the UNUSED-entry detection mechanism (mirrors {@code
     * test_changelog_rls_lint.py}'s {@code
     * test_unused_allowlist_entry_is_detected_when_no_matching_finding}): a
     * bogus exempt entry that matches no live finding must be reported as
     * unused, not silently accepted. Proves the real-tree {@code
     * test_realTree_zeroUnusedExemptEntries} assertion actually detects
     * drift rather than trivially passing because nothing is ever checked.
     */
    @Test
    void rotDetector_bogusExemptEntryIsReportedUnused() {
        String synthetic = String.join("\n",
            "public final class Synthetic {",
            "    private void plain(DSLContext ctx, String tenant) {",
            "        ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)",
            "           .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))",
            "           .fetch();",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        // "neverMatchedMethod" names nothing in this source -- sanctionedRegions
        // returns an empty region list, exactly the "stale/wrong entry" shape.
        List<NamedRegion> exempt = namedRegions(blanked, List.of("neverMatchedMethod"));
        var docSites = new ArrayList<Finding>();
        var chunkSites = new ArrayList<Finding>();
        scanDocAndChunkSites("Synthetic.java", blanked, exempt, List.of(), docSites, chunkSites);

        // The one real site passes on its own filter (not via the bogus
        // exemption) -- consumed set stays empty, so "neverMatchedMethod"
        // would show up as unused against this corpus, exactly like a stale
        // TOMBSTONE_EXEMPT entry would against the real tree.
        assertThat(docSites).hasSize(1);
        assertThat(docSites.get(0).exemptMethod())
            .as("the passing site must not have been (mis)attributed to the bogus exemption")
            .isNull();
    }

    // ── Real-tree full-consumption (== not >=, see class javadoc) ──────────

    @Test
    void test_realTree_zeroUnusedExemptEntries() throws IOException {
        var result = scan();
        var consumed = new java.util.LinkedHashSet<String>();
        for (var f : List.of(result.docSites(), result.chunkSites(), result.setDeletedSites(), result.rawSqlSites())) {
            for (Finding fnd : f) if (fnd.exemptMethod() != null) consumed.add(fnd.file() + "::" + fnd.exemptMethod());
        }
        var allExemptKeys = new java.util.LinkedHashSet<String>();
        for (var e : TOMBSTONE_EXEMPT) allExemptKeys.add(e.file() + "::" + e.method());
        assertThat(consumed)
            .as("every TOMBSTONE_EXEMPT entry must be independently re-derived by a live scan "
                + "of the real tree, never hand-waved")
            .isEqualTo(allExemptKeys);
    }
}
