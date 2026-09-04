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
 * upsertDocument}). A fifth and sixth check ({@link #scanRawChunksSites},
 * {@link #scanTypedChunksSites}) cover the two OTHER shapes a
 * {@code nexus.chunks_<dim>} read can structurally evade the first two
 * checks entirely: a hand-built {@code chunksTable(dim)} SQL string
 * (nexus-3ck2g, {@code searchWithTokens}/{@code hybridSearch}) and a typed
 * {@code DimTables.ChunkTable} accessor (nexus-8j1zx, the get-family reads
 * — found only in round-1 substantive critique of the FIRST fix, because
 * a typed jOOQ statement carries neither a {@code chunksTable(} call nor a
 * {@code CATALOG_DOCUMENTS}/{@code CATALOG_DOCUMENT_CHUNKS} token in the
 * same statement as the read).
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

    /**
     * nexus-3ck2g E2: files whose raw {@code chunksTable(dim)}-string-built reads must
     * carry the inline live_chunks predicate (E1). Unlike {@link #scanDocAndChunkSites}
     * (which recognizes jOOQ-typed {@code .select(...)}/{@code CATALOG_DOCUMENT_CHUNKS}
     * statements), {@code searchWithTokens}/{@code hybridSearch} build hand-written SQL
     * text across MULTIPLE Java statements within one method (a {@code chunksTable(dim)}
     * call, StringBuilder/String concatenation, the {@code rawVectorFetch(...)} chokepoint
     * call) — structurally invisible to the token scan above, which is exactly why
     * {@link #TARGET_FILES} could not see these two methods before this check existed
     * (nexus-3ck2g bug: the regression shipped and stayed invisible to this whole class of
     * gate). Method-scoped (WIDEN-style, not statement-scoped): each entry names a method
     * whose body raw-reads {@code nexus.chunks_<dim>} via {@code chunksTable(dim)}.
     *
     * <p>Empty (nexus-zrcj7, 2026-09-03): the sole entry, PgVectorRepository.java's
     * {@code searchWithTokens}/{@code hybridSearch}, is REMOVED. Both methods are retired
     * onto generated jOOQ function tables ({@code plain_search_<dim>}/
     * {@code text_gated_search_<dim>}, vectors-009/010) whose OWN inlined SQL body carries
     * the live_chunks/tombstone predicate directly — neither method calls
     * {@code chunksTable(dim)} any more (that raw-SQL-string channel is gone; {@code
     * chunksTable(dim)} itself survives only for {@link
     * dev.nexus.service.vectors.PgVectorRepository#upsertChunks}, which this scan never
     * named). Kept as a live (not deleted) mechanism, dormant by construction until a
     * future raw {@code chunksTable(dim)} string read needs it again — same "dormant, not
     * enforced, noted per spec" disposition this class's own header gives the
     * {@code catalog_links} case.
     */
    private static final Map<String, List<String>> RAW_CHUNKS_READ_METHODS = Map.of();

    /** Marker for a raw {@code chunksTable(dim)} table reference in the blanked source. */
    private static final String CHUNKS_TABLE_CALL = "chunksTable(";
    /** Marker for the inline live_chunks predicate call (E1's fix). */
    private static final String LIVE_CHUNKS_PREDICATE_CALL = "liveChunksPredicate(";

    /**
     * nexus-8j1zx (found during nexus-3ck2g's round-1 substantive critique): the get-family
     * reads ({@code get}, {@code getWhere}, {@code getEmbeddings}, {@code getAllMetadata})
     * go through the TYPED {@link dev.nexus.service.vectors.DimTables.ChunkTable} accessor
     * ({@code ch.table()}/{@code ch.chash()}/...) rather than the raw {@code chunksTable(dim)}
     * string helper {@link #RAW_CHUNKS_READ_METHODS} polices, and never reference a
     * {@code CATALOG_DOCUMENTS}/{@code CATALOG_DOCUMENT_CHUNKS} token in the SAME statement as
     * the read (the typed live-chunks condition is a separate helper call) — structurally
     * invisible to BOTH {@link #scanDocAndChunkSites} (keys off those literal tokens) and
     * {@link #scanRawChunksSites} (keys off the literal {@code chunksTable(} call). Method-scoped
     * (WIDEN-style) for the same reason as {@link #RAW_CHUNKS_READ_METHODS}: the {@code
     * DimTables.CHUNKS.get(dim)} accessor assignment and the {@code liveChunksCondition(...)}
     * call live in different Java statements.
     *
     * <p>{@code list} added by nexus-txcbo (nexus-3ck2g round-2 critique): the SAME typed-read
     * gap as the get-family, found in {@code PgVectorRepository#list} (backs {@code POST
     * /v1/vectors/store-list} / {@code nx store list} / MCP {@code store_list}) — unlike the
     * get-family it needs no out-of-band chash; a plain listing surfaced tombstoned content by
     * default. A live sweep of every {@code DimTables.CHUNKS.get(dim)} occurrence in the file at
     * fix time found exactly one other unfiltered candidate (this one); {@code count} is
     * PRE-EXISTING tracked scope of nexus-dzs62 (left untouched); {@code fetchChunkText} has zero
     * live HTTP callers (left untouched, noted as a landmine); every other occurrence is either
     * already filtered (get-family above) or a write-path / existence-probe helper feeding the
     * upsert flow, not a content-serving read.
     */
    private static final Map<String, List<String>> TYPED_CHUNKS_READ_METHODS = Map.of(
        "PgVectorRepository.java", List.of("get", "getWhere", "getEmbeddings", "getAllMetadata", "list"));

    /** Marker for a typed {@code DimTables.ChunkTable} accessor assignment in the blanked source. */
    private static final String TYPED_CHUNK_TABLE_ACCESS = "DimTables.CHUNKS.get(dim)";
    /** Marker for the typed live-chunks condition call (nexus-8j1zx's fix). */
    private static final String LIVE_CHUNKS_CONDITION_CALL = "liveChunksCondition(";

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
     *
     * <p>Also deliberately NOT here (nexus-j862l, RDR-191 GATE-2 follow-up):
     * {@code requireDocumentExists} (the renamed, narrowed successor to
     * {@code physicalCollectionOf}, which WAS listed here before RDR-191
     * removed its collection-resolution duty). Its existence check reads via
     * {@code ctx.fetchExists(ctx.selectOne().from(CATALOG_DOCUMENTS)...)} —
     * {@code .selectOne(}, not one of {@link #SELECT_INITIATORS} ({@code
     * .select(}/{@code .selectFrom(}/{@code .selectCount(}/{@code
     * .selectDistinct(}) — so {@link #scanDocAndChunkSites} never produces a
     * Finding for it at all; it is structurally outside this gate's scan
     * domain, the SAME category {@code CatalogRepository.strandedChunkCount}/
     * {@code hasProtectingManifest} and {@code PgVectorRepository.liveChunksCondition}
     * already occupy (see that method's own javadoc) — none of those appear
     * here either. A rename-only fix (keeping the entry, renaming
     * {@code physicalCollectionOf} to {@code requireDocumentExists}) was
     * tried first and correctly failed {@link #test_realTree_zeroUnusedExemptEntries}
     * / {@link #noUnguardedTombstoneReadsOrWrites} as rot: the live scan can
     * never reproduce an entry for a method it structurally cannot see,
     * regardless of name. Its EXISTS check still legitimately needs to see a
     * tombstoned row (to throw the correct {@code DocumentNotFoundException}
     * vs. {@code TombstonedDocumentException} — see its own definition-site
     * comment) — that reasoning did not change, only its visibility to THIS
     * particular gate mechanism did.
     */
    private static final List<ExemptEntry> TOMBSTONE_EXEMPT = List.of(
        new ExemptEntry("CatalogRepository.java", "highestChildSeq",
            "tumbler allocator: the tumbler PK does not exclude tombstones, and filtering "
            + "would re-issue an already-taken child sequence number to a NEW document"),
        new ExemptEntry("CatalogRepository.java", "upsertDocument",
            "the ONE sanctioned way a tombstoned row is revived (nexus-mqd6t Hal ruling): an "
            + "explicit re-register clears deleted_at via the ON CONFLICT DO UPDATE arm, "
            + "deliberately unconditional — updateDocument refuses tombstoned targets outright "
            + "so an incidental field write can never resurrect one"),
        new ExemptEntry("CatalogRepository.java", "collectionDocCountsIncludingDeleted",
            "counting tombstoned rows IS the contract (nexus-8tnz2): the drop-orphan-collections "
            + "classifier subtracts the tombstone-aware collectionDocCounts from this all-rows "
            + "count to tell orphan (safe to drop) from tombstoned-only (restorable; never drop) "
            + "-- a deleted_at filter here would collapse the two classes and resurrect the "
            + "hard-delete-of-restorable-data hazard the method exists to prevent"),
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
            + "statements over a landing zone, never serving-path"),
        new ExemptEntry("CatalogRepository.java", "agedTombstoneCount",
            "nexus-3ck2g E3 (/v1/catalog/purge-trash): this read's whole PURPOSE is counting "
            + "the TOMBSTONED population itself (deleted_at IS NOT NULL), mirroring "
            + "nexus.purge_trash's own Step 4 WHERE — the inverse of every other "
            + "CATALOG_DOCUMENTS read this gate polices, which must EXCLUDE tombstones"),
        new ExemptEntry("CatalogRepository.java", "requireImportLinkEndpointsExist",
            "RDR-194 P1 (nexus-tk070.p1, D2): the /import/link endpoint precheck mirrors the "
            + "catalog_links tumbler FKs (fk_catalog_links_from_document/_to_document, "
            + "catalog-032), which test ROW EXISTENCE in catalog_documents, not liveness — a "
            + "link to a TOMBSTONED document is still writable (soft delete does not fire ON "
            + "DELETE CASCADE), only a tumbler with NO row is a dangling_endpoint. Adding "
            + "DELETED_AT.isNull() here would reject rows the FK accepts, diverging the "
            + "precheck's 400 from the constraint of record; the FK remains the enforcement, "
            + "this SELECT only names the offending row before the INSERT would abort the tx")
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
     * actually walked, for the non-vacuity floors.
     *
     * <p>nexus-4okz4 increment 3: {@code rawSqlSites} (fed by the retired
     * {@code scanRawSqlSites}) is GONE, not merely emptied — see that
     * method's former javadoc, preserved below at its old call site, for
     * why removal (not a floor adjustment) is the correct response. */
    record ScanResult(List<Finding> docSites, List<Finding> chunkSites,
                       List<Finding> setDeletedSites,
                       List<Finding> rawChunksSites, List<Finding> typedChunksSites) {}

    static ScanResult scan() throws IOException {
        List<Finding> docSites = new ArrayList<>();
        List<Finding> chunkSites = new ArrayList<>();
        List<Finding> setDeletedSites = new ArrayList<>();
        List<Finding> rawChunksSites = new ArrayList<>();
        List<Finding> typedChunksSites = new ArrayList<>();

        for (var entry : TARGET_FILES.entrySet()) {
            String fname = entry.getKey();
            String src = Files.readString(entry.getValue());
            String blanked = RawSqlGateTest.blank(src);
            List<NamedRegion> exemptRegions = namedRegions(blanked, exemptNames(fname));
            List<NamedRegion> widenRegions = namedRegions(blanked, widenNames(fname));

            scanDocAndChunkSites(fname, blanked, exemptRegions, widenRegions, docSites, chunkSites);
            scanSetDeletedSites(fname, blanked, exemptRegions, setDeletedSites);
            scanRawChunksSites(fname, blanked, exemptRegions, rawChunksSites);
            scanTypedChunksSites(fname, blanked, exemptRegions, typedChunksSites);
        }
        return new ScanResult(docSites, chunkSites, setDeletedSites, rawChunksSites, typedChunksSites);
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

    /**
     * nexus-3ck2g E2: for each method named in {@link #RAW_CHUNKS_READ_METHODS} for
     * *fname*, flag it as a raw-chunks read site if its body calls {@link
     * #CHUNKS_TABLE_CALL} (reads {@code nexus.chunks_<dim>} via hand-built SQL text —
     * as opposed to a typed jOOQ {@code DimTables}/{@code ch.table()} read, which this
     * check does not concern itself with) and does NOT also call {@link
     * #LIVE_CHUNKS_PREDICATE_CALL} anywhere in the same method body. Method-scoped
     * (WIDEN-style) rather than statement-scoped by necessity: {@code chunksTable(dim)},
     * the SQL-string assembly, and the inline predicate live in different Java
     * statements within {@code searchWithTokens}/{@code hybridSearch} (see the class
     * javadoc on {@link #RAW_CHUNKS_READ_METHODS}). A method matching the name but
     * NOT calling {@link #CHUNKS_TABLE_CALL} (e.g. a thin delegating overload) is
     * silently skipped — it is not itself a raw chunks-table reader.
     */
    static void scanRawChunksSites(String fname, String blanked, List<NamedRegion> exemptRegions, List<Finding> out) {
        List<String> methodNames = RAW_CHUNKS_READ_METHODS.getOrDefault(fname, List.of());
        if (methodNames.isEmpty()) return;
        for (NamedRegion r : namedRegions(blanked, methodNames)) {
            String body = blanked.substring(r.start(), r.end());
            if (!body.contains(CHUNKS_TABLE_CALL)) continue;
            boolean hasFilter = body.contains(LIVE_CHUNKS_PREDICATE_CALL);
            String exemptMethod = hasFilter ? null : methodAt(exemptRegions, r.start());
            int line = lineOf(blanked, r.start());
            out.add(new Finding(fname, "raw_chunks", line,
                r.method() + "(): raw chunksTable(dim) read", hasFilter || exemptMethod != null, exemptMethod));
        }
    }

    /**
     * nexus-8j1zx: for each method named in {@link #TYPED_CHUNKS_READ_METHODS} for
     * *fname*, flag it as a typed-chunks read site if its body calls {@link
     * #TYPED_CHUNK_TABLE_ACCESS} (reads {@code nexus.chunks_<dim>} via the typed {@code
     * DimTables.ChunkTable} accessor — as opposed to the hand-built {@code chunksTable(dim)}
     * SQL text {@link #scanRawChunksSites} polices) and does NOT also call {@link
     * #LIVE_CHUNKS_CONDITION_CALL} anywhere in the same method body. Method-scoped
     * (WIDEN-style) rather than statement-scoped: the {@code DimTables.CHUNKS.get(dim)}
     * accessor assignment and the {@code liveChunksCondition(...)} call live in different
     * Java statements in every get-family method. A method matching the name but NOT calling
     * {@link #TYPED_CHUNK_TABLE_ACCESS} (e.g. a thin delegating overload) is silently
     * skipped — it is not itself a typed chunks-table reader.
     */
    static void scanTypedChunksSites(String fname, String blanked, List<NamedRegion> exemptRegions, List<Finding> out) {
        List<String> methodNames = TYPED_CHUNKS_READ_METHODS.getOrDefault(fname, List.of());
        if (methodNames.isEmpty()) return;
        for (NamedRegion r : namedRegions(blanked, methodNames)) {
            String body = blanked.substring(r.start(), r.end());
            if (!body.contains(TYPED_CHUNK_TABLE_ACCESS)) continue;
            boolean hasFilter = body.contains(LIVE_CHUNKS_CONDITION_CALL);
            String exemptMethod = hasFilter ? null : methodAt(exemptRegions, r.start());
            int line = lineOf(blanked, r.start());
            out.add(new Finding(fname, "typed_chunks", line,
                r.method() + "(): typed DimTables.ChunkTable read", hasFilter || exemptMethod != null, exemptMethod));
        }
    }

    // RETIRED (nexus-4okz4 increment 3): scanRawSqlSites / CATALOG_DOCUMENTS_RAW /
    // stringLiteralView, and the floor_stagingPromoteOpsRawSqlSites floor that
    // consumed their output, are DELETED — not adjusted, not zeroed-and-kept.
    //
    // What this sub-scan was FOR (preserved for the historical record):
    // StagingPromoteOps.java was, before increment 3, the ONE file this gate's
    // TARGET_FILES covers that carried raw string-SQL under a RawSqlGateTest
    // SANCTIONED_METHODS entry — its statements were built as Java String
    // concatenation (`"UPDATE nexus.catalog_documents d " + ...`), so the
    // primary token scan (scanDocAndChunkSites, which recognizes the JAVA
    // IDENTIFIER `CATALOG_DOCUMENTS` and jOOQ initiators like `.update(`) was
    // STRUCTURALLY BLIND to them: a raw SQL string literal contains the
    // lowercase SQL keyword `catalog_documents`, never the Java constant
    // token, and blank() (the primary scan's source view) erases string
    // literal CONTENTS entirely. scanRawSqlSites existed as the dedicated
    // counterpart: an INVERTED view (stringLiteralView — keep string
    // contents, blank code+comments) matched literally on `catalog_documents`
    // text, so those two raw statements (chunk_count_resynced's UPDATE,
    // unresolvedKnowledgeTitles's SELECT — both inside finalizeTenant, both
    // pre-existing TOMBSTONE-EXEMPT migration-leg statements with no
    // deleted_at filter by design) stayed visible to SOME scan instead of
    // falling through both mechanisms unseen.
    //
    // Why deletion, not a floor adjustment or a reworked DSL-form counter:
    // nexus-4okz4 increment 3 converted StagingPromoteOps.java to typed jOOQ
    // DSL end to end (RawSqlGateTest's SANCTIONED_METHODS entry for this file
    // is gone too — zero raw execute()/fetch() strings remain). The two
    // statements this sub-scan used to catch now read
    // `ctx.update(CATALOG_DOCUMENTS)...` / `ctx.selectDistinct(CATALOG_DOCUMENTS.TITLE)...`
    // — the Java IDENTIFIER `CATALOG_DOCUMENTS` and a `.update(`/`.selectDistinct(`
    // initiator, in the SAME statement — which is EXACTLY what scanDocAndChunkSites
    // (the primary scan, the same mechanism that already covers
    // CatalogRepository.java's and PgVectorRepository.java's 100%-typed-DSL
    // sites with no sub-scan of their own) natively recognizes. Verified, not
    // assumed: (a) noUnguardedTombstoneReadsOrWrites passes with ZERO
    // violations post-conversion — the two ex-raw sites AND the
    // manifest_promoted INSERT (whose subquery trips the `.select(` initiator
    // token) all now surface as docSites/chunkSites findings, each correctly
    // excused via the SAME pre-existing `StagingPromoteOps.java::finalizeTenant`
    // TOMBSTONE_EXEMPT entry (still consumed — see
    // test_realTree_zeroUnusedExemptEntries, which does not special-case
    // rawSqlSites and never needed to); (b) grepping the converted file for
    // "catalog_document" confirms every remaining occurrence is Java code
    // (imports, generated-Tables identifiers) or a comment, zero string
    // literals. A dedicated sub-scan whose precondition (raw SQL strings
    // existing in this file) is now permanently false is the exact
    // "un-falsifiable, always-vacuous" class the class javadoc's own
    // philosophy rejects — same disposition RawSqlGateTest gives a
    // SANCTIONED_METHODS entry once its raw form is deleted outright (see
    // that class's ChashSqlIdioms.java entry history): remove it, don't
    // enshrine a floor that can never again be anything but its own minimum.

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

        for (var f : List.of(result.docSites(), result.chunkSites(), result.setDeletedSites(),
                              result.rawChunksSites(), result.typedChunksSites())) {
            for (Finding fnd : f) {
                if (!fnd.excused()) violations.add(fnd);
                if (fnd.exemptMethod() != null) consumed.add(fnd.file() + "::" + fnd.exemptMethod());
            }
        }

        assertThat(violations)
            .as("unguarded CATALOG_DOCUMENTS/CATALOG_DOCUMENT_CHUNKS read or write "
                + "(missing DELETED_AT.isNull()/liveParentDoc()/liveDocument()/documentExists() "
                + "or an unguarded resurrection SET), a raw chunksTable(dim) read missing the "
                + "inline live_chunks predicate (liveChunksPredicate(...), nexus-3ck2g), or a "
                + "typed DimTables.ChunkTable read missing the typed live_chunks condition "
                + "(liveChunksCondition(...), nexus-8j1zx) — filter it, or add a named, "
                + "rationale-carrying TOMBSTONE_EXEMPT entry: %s", violations)
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

    // floor_rawChunksReadSites: RETIRED (nexus-zrcj7, 2026-09-03), not adjusted to
    // >= 0 — same disposition floor_stagingPromoteOpsRawSqlSites already established
    // in this file. Its corpus (searchWithTokens/hybridSearch reading
    // chunksTable(dim) as hand-built SQL text, RAW_CHUNKS_READ_METHODS above) is now
    // permanently empty by construction: both methods are retired onto generated
    // jOOQ function tables (plain_search_<dim>/text_gated_search_<dim>, vectors-
    // 009/010) whose own inlined SQL predicate carries the live_chunks/tombstone
    // filter directly. A floor whose corpus can never again be anything but its own
    // minimum is backlog padding, not a live regression guard.

    @Test
    void floor_typedChunksReadSites() throws IOException {
        // 5 on the final tree: get, getWhere, getEmbeddings, getAllMetadata (nexus-8j1zx)
        // + list (nexus-txcbo) — the five typed DimTables.ChunkTable reads found reading
        // nexus.chunks_<dim> with zero tombstone filtering (structurally invisible to
        // both scanDocAndChunkSites and scanRawChunksSites). Their 5-arg delegating
        // overloads (get/getWhere) are not themselves candidates (no DimTables.CHUNKS
        // access in their one-line bodies) so they do not inflate this count.
        var result = scan();
        assertThat(result.typedChunksSites().size())
            .as("typed DimTables.ChunkTable read sites walked — a collapse below this suggests "
                + "the method-name list in TYPED_CHUNKS_READ_METHODS stopped matching")
            .isGreaterThanOrEqualTo(5);
    }

    // floor_stagingPromoteOpsRawSqlSites: RETIRED (nexus-4okz4 increment 3),
    // not adjusted — see the retirement comment at the old scanRawSqlSites
    // call site (above ScanResult) for the full rationale. Its corpus
    // (raw-SQL string literals in StagingPromoteOps.java) is now permanently
    // empty by construction; the invariant it defended is subsumed by
    // scanDocAndChunkSites, covered by noUnguardedTombstoneReadsOrWrites and
    // floor_catalogDocumentsOrViewSites/floor_catalogDocumentChunksReadSites
    // above (which count the SAME statements natively once they moved from
    // raw string SQL to typed DSL).

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

    // ── Meta-tests: the raw-chunks scan (nexus-3ck2g E2) ─────────────────────

    // rawChunksScan_missingPredicate_isFlagged / rawChunksScan_presentPredicate_passes:
    // RETIRED (nexus-zrcj7, 2026-09-03). Both proved scanRawChunksSites's flag/excuse
    // behavior for a method NAMED in RAW_CHUNKS_READ_METHODS — a precondition that no
    // longer holds now that map is empty (see its own retirement comment above).
    // rawChunksScan_noChunksTableCall_isNotACandidate (below) still exercises the same
    // scan function and needs no change (it already asserted zero findings, which is
    // exactly what an unregistered/no-match method name produces either way).

    /** A method whose name matches but never reads the raw chunks table (no
     * {@code chunksTable(} call) is not a candidate at all — zero findings. */
    @Test
    void rawChunksScan_noChunksTableCall_isNotACandidate() {
        String synthetic = String.join("\n",
            "public final class PgVectorRepository {",
            "    private String hybridSearch(String tenant, String queryText) {",
            "        return \"delegates elsewhere\";",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        var out = new ArrayList<Finding>();
        scanRawChunksSites("PgVectorRepository.java", blanked, List.of(), out);
        assertThat(out).isEmpty();
    }

    /** A file not named in {@link #RAW_CHUNKS_READ_METHODS} is skipped entirely,
     * even if it happens to contain a matching method name. */
    @Test
    void rawChunksScan_fileNotRegistered_isSkipped() {
        String synthetic = String.join("\n",
            "public final class SomeOtherFile {",
            "    private String searchWithTokens(String tenant) {",
            "        String table = chunksTable(dim);",
            "        return table;",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        var out = new ArrayList<Finding>();
        scanRawChunksSites("SomeOtherFile.java", blanked, List.of(), out);
        assertThat(out).isEmpty();
    }

    // ── Meta-tests: the typed-chunks scan (nexus-8j1zx) ──────────────────────

    /**
     * The exact regression this check exists to catch: a method matching one of
     * {@link #TYPED_CHUNKS_READ_METHODS} that reads via {@code DimTables.CHUNKS.get(dim)}
     * but never calls {@code liveChunksCondition(...)} must be flagged, unexcused.
     */
    @Test
    void typedChunksScan_missingPredicate_isFlagged() {
        String synthetic = String.join("\n",
            "public final class PgVectorRepository {",
            "    public Map<String, Object> get(String tenant, String collection, List<String> ids) {",
            "        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);",
            "        var result = ctx.select(ch.chash()).from(ch.table())",
            "                        .where(ch.chash().in(ids)).fetch();",
            "        return Map.of();",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        var out = new ArrayList<Finding>();
        scanTypedChunksSites("PgVectorRepository.java", blanked, List.of(), out);
        assertThat(out).hasSize(1);
        assertThat(out.get(0).excused())
            .as("a DimTables.CHUNKS.get(dim) read with no liveChunksCondition(...) call "
                + "anywhere in the method must be flagged")
            .isFalse();
    }

    /** The fixed shape: the same method, now calling the typed predicate. */
    @Test
    void typedChunksScan_presentPredicate_passes() {
        String synthetic = String.join("\n",
            "public final class PgVectorRepository {",
            "    public Map<String, Object> get(String tenant, String collection, List<String> ids) {",
            "        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);",
            "        var result = ctx.select(ch.chash()).from(ch.table())",
            "                        .where(ch.chash().in(ids).and(liveChunksCondition(ctx, ch))).fetch();",
            "        return Map.of();",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        var out = new ArrayList<Finding>();
        scanTypedChunksSites("PgVectorRepository.java", blanked, List.of(), out);
        assertThat(out).hasSize(1);
        assertThat(out.get(0).excused()).isTrue();
    }

    /** A method whose name matches but never reads via the typed accessor (no
     * {@code DimTables.CHUNKS.get(dim)} call) is not a candidate at all — zero findings. */
    @Test
    void typedChunksScan_noTypedAccess_isNotACandidate() {
        String synthetic = String.join("\n",
            "public final class PgVectorRepository {",
            "    public Map<String, Object> getWhere(String tenant, String collection, Map<String, Object> where) {",
            "        return get(tenant, collection, List.of());",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        var out = new ArrayList<Finding>();
        scanTypedChunksSites("PgVectorRepository.java", blanked, List.of(), out);
        assertThat(out).isEmpty();
    }

    /** A file not named in {@link #TYPED_CHUNKS_READ_METHODS} is skipped entirely,
     * even if it happens to contain a matching method name. */
    @Test
    void typedChunksScan_fileNotRegistered_isSkipped() {
        String synthetic = String.join("\n",
            "public final class SomeOtherFile {",
            "    public Map<String, Object> get(String tenant, String collection, List<String> ids) {",
            "        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);",
            "        return Map.of();",
            "    }",
            "}");
        String blanked = RawSqlGateTest.blank(synthetic);
        var out = new ArrayList<Finding>();
        scanTypedChunksSites("SomeOtherFile.java", blanked, List.of(), out);
        assertThat(out).isEmpty();
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
            "    private static String exemptReadHelper(DSLContext ctx, String tenant, String docId) {",
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
        List<NamedRegion> exempt = namedRegions(blanked, List.of("exemptReadHelper"));
        var docSites = new ArrayList<Finding>();
        var chunkSites = new ArrayList<Finding>();
        scanDocAndChunkSites("CatalogRepository.java", blanked, exempt, List.of(), docSites, chunkSites);

        assertThat(docSites).hasSize(2);
        assertThat(docSites.stream().filter(f -> !f.excused()).count())
            .as("the nested class's own unfiltered read must NOT inherit exemptReadHelper's "
                + "exemption: %s", docSites)
            .isEqualTo(1);
    }

    @Test
    void attribution_packagePrivateMethodAfterExemptMethod_resetsExemption() {
        String synthetic = String.join("\n",
            "public final class CatalogRepository {",
            "    private static String exemptReadHelper(DSLContext ctx, String tenant, String docId) {",
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
        List<NamedRegion> exempt = namedRegions(blanked, List.of("exemptReadHelper"));
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
        for (var f : List.of(result.docSites(), result.chunkSites(), result.setDeletedSites(),
                              result.rawChunksSites(), result.typedChunksSites())) {
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
