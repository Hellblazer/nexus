// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Task 8 (sibling of {@link CatalogHandlerManifestEnvelopeTest}): a
 * parametrized-in-spirit conformance gate over EVERY route in {@link
 * dev.nexus.service.http.CatalogHandler}'s routing switch, not just the two
 * {@code CatalogHandlerManifestEnvelopeTest} already covers by live HTTP test
 * ({@code docs_for_chashes}, {@code get_many}).
 *
 * <p>Static-source-scan gate (same family as {@code RawSqlGateTest} / {@code
 * TombstoneFilterGateTest}), not a live-HTTP-integration gate: spinning up 65
 * Testcontainers-backed endpoint round-trips per route is out of proportion
 * to what this class needs to prove, and duplicates existing per-route unit/
 * integration coverage. What THIS gate proves is different and cheaper to
 * check statically: every route has been CONSCIOUSLY classified, and the
 * classification's guard (an emitted {@code count} field / an enforced
 * {@link dev.nexus.service.http.CatalogHandler#MAX_BATCH_DOC_IDS} cap) is
 * actually present in the handler's source — or sits on a named,
 * rationale-carrying exemption.
 *
 * <p>Three checks, in order:
 * <ol>
 *   <li>{@link #everySwitchRouteIsClassified()} — the case-label set parsed
 *       live out of {@code CatalogHandler}'s {@code switch (op)} block must
 *       equal (never subset/superset) {@link #ROUTES}'s route-key set. A
 *       brand-new route with no {@link RouteSpec} entry fails THIS check
 *       first, before the guard checks below even run — the mechanism behind
 *       "a new route added without guards must fail the gate."</li>
 *   <li>{@link #collectionReturningRoutesEmitCountOrAreExempt()} — every
 *       route marked {@code collectionReturning} must have its handler
 *       method's body contain a literal {@code "count"} JSON-key token, or
 *       carry a {@code countExempt} rationale.</li>
 *   <li>{@link #idListAcceptingRoutesEnforceCapOrAreExempt()} — every route
 *       marked {@code idListAccepting} must have its handler method's body
 *       reference {@code MAX_BATCH_DOC_IDS}, or carry a {@code capExempt}
 *       rationale.</li>
 * </ol>
 *
 * <p>Classification definitions, applied uniformly across all 65 routes on
 * the final (post-{@code by_doc_id}-removal) tree:
 * <ul>
 *   <li><b>collectionReturning</b> — the response body's TOP-LEVEL JSON value
 *       is (or contains, as a named field) a JSON ARRAY representing a SET of
 *       items whose length could vary/be paged. A {@code Map<id, ...>}
 *       response keyed by an ALREADY-CAPPED input id list does not count
 *       (its size is bounded by the capped request, not a server-side page)
 *       — e.g. {@code /docs/chunk-counts}, {@code /resolve_many}'s {@code
 *       entries} map.</li>
 *   <li><b>idListAccepting</b> — the request body carries a JSON list of bare
 *       document/tumbler/chash/relation IDENTIFIERS that flows into a
 *       bind-parameter-bounded SQL clause (an {@code IN (...)} or a
 *       VALUES-row batch keyed 1 row per identifier). A list of FULL row
 *       objects for a single document ({@code /manifest/write}'s {@code
 *       rows}, the {@code /import/*} batch bodies) is a different class —
 *       bounded by that one document's/that one ETL batch's natural size,
 *       not an open-ended caller-supplied identifier list — and is NOT
 *       classified {@code idListAccepting} here.</li>
 * </ul>
 *
 * <p>Two real gaps found during this gate's authorship, FIXED (not
 * exempted) in {@code CatalogHandler} before this table was written:
 * {@code /docs/chunk-counts} and {@code /links/from-batch} had NO {@code
 * MAX_BATCH_DOC_IDS} guard at all (every sibling batch endpoint did) — both
 * now enforce it. {@code /traverse}'s {@code seeds} list had the same gap;
 * also fixed. See the git diff for the added guards.
 *
 * <p>One genuine gap found and left as a DOCUMENTED exemption rather than
 * fixed in this pass: {@code /traverse}'s response ({@code nodes}/{@code
 * edges}) carries no count/truncation signal even though {@link
 * dev.nexus.service.db.CatalogRepository#graphBFS} already computes {@code
 * atOrOverCap} (the 500-node {@code MAX_GRAPH_NODES} cap) — see the {@code
 * countExempt} rationale on that route's {@link RouteSpec} for why closing it
 * is out of scope here (it changes the wire contract, not just adds a
 * missing check).
 */
class CatalogHandlerEnvelopeConformanceGateTest {

    private static final Path HANDLER_FILE = Path.of(
        "src", "main", "java", "dev", "nexus", "service", "http", "CatalogHandler.java");

    // ── Rationale buckets (reused across routes of the same shape) ─────────

    private static final String POSITIONAL =
        "positionally aligned 1:1 with the caller's own input list; the caller already knows "
        + "the expected length, so an echoed count adds no reconciliation value (contrast the "
        + "ir6eh/ocf52/b9puj precedent trio, whose truncation risk is a page of a LARGER "
        + "server-side set, not a transform of the caller's own list)";

    private static final String REPORT_ONLY =
        "reports which inputs succeeded, never a downstream destructive-decision input "
        + "(contrast the ir6eh/ocf52/b9puj precedent trio, which gate a GC/orphan-sweep "
        + "deletion on exact reconciliation)";

    private static final String ADMIN_SCALE =
        "admin/config-scale result set (one row per owner/collection/root, not per-document) "
        + "with no established truncation-risk consumer -- not a page of a large, growing, "
        + "per-document collection";

    private static final String UNBOUNDED_NOT_PAGED =
        "unbounded query with no repo-side page/limit cap -- not truncated by construction, so "
        + "there is nothing to reconcile a count against; add one if this ever gains pagination";

    private static final String SMALL_FIXED_CARDINALITY =
        "grouped by a small, fixed-cardinality dimension (content_type / distinct collection "
        + "name), not one row per document -- not a truncation-risk surface";

    private static final String RESOLVE_BOUNDED =
        "the file_path+collection branch returns at most one document, the title branch caps "
        + "at 10 via searchDocuments, and the source_uri branch is <=1 by the catalog-016 "
        + "partial unique index. HONEST EXCEPTION (review 2026-08-01): the BARE file_path "
        + "branch is unbounded and unscoped by owner -- file_path has no uniqueness "
        + "constraint, so multi-owner same-path rows return an uncapped array with no "
        + "count/truncation signal. Tracked as nexus-oii5r; this exemption converts to a "
        + "count requirement (or the branch gains a bound) when that bead lands";

    private static final String WHITELISTED_NOT_BIND_RISK =
        "the relation-name list is filtered against a small, fixed server-side whitelist "
        + "(VERIFY_RELATIONS/COUNT_ONLY_RELATIONS) before any query executes; an oversized list "
        + "costs client-side String comparisons, never a SQL bind-parameter risk -- the concern "
        + "MAX_BATCH_DOC_IDS exists for";

    private static final String GRAPH_TRUNCATION_GENUINE_GAP =
        "GENUINE GAP, deliberately left exempt rather than silently masked: graphBFS "
        + "(CatalogRepository) already computes atOrOverCap (the 500-node MAX_GRAPH_NODES cap) "
        + "but never surfaces it in the response -- a caller cannot detect the silent "
        + "truncation. Not fixed in this pass because it changes the wire contract (a field "
        + "client code must start handling), not just adds a missing check. Follow-up: surface "
        + "atOrOverCap as e.g. nodes_truncated in graphBFS's returned map.";

    // ── Route table (rationale-as-data, pinned classification) ─────────────

    record RouteSpec(String route, String handler, boolean collectionReturning,
                      boolean idListAccepting, String countExempt, String capExempt) {}

    private static RouteSpec neither(String route, String handler) {
        return new RouteSpec(route, handler, false, false, null, null);
    }

    private static RouteSpec collectionOk(String route, String handler) {
        return new RouteSpec(route, handler, true, false, null, null);
    }

    private static RouteSpec collectionExempt(String route, String handler, String rationale) {
        return new RouteSpec(route, handler, true, false, rationale, null);
    }

    private static RouteSpec idListOk(String route, String handler) {
        return new RouteSpec(route, handler, false, true, null, null);
    }

    private static RouteSpec idListExempt(String route, String handler, String rationale) {
        return new RouteSpec(route, handler, false, true, null, rationale);
    }

    private static RouteSpec both(String route, String handler, String countExempt) {
        return new RouteSpec(route, handler, true, true, countExempt, null);
    }

    /** Enumerated honestly from the live switch (see {@link #everySwitchRouteIsClassified}),
     * post {@code by_doc_id}-removal. 65 routes on the final tree. */
    private static final List<RouteSpec> ROUTES = List.of(
        // ── Documents ─────────────────────────────────────────────────────
        neither("/register", "handleRegister"),
        neither("/show", "handleShow"),
        collectionOk("/list", "handleList"),
        collectionOk("/search", "handleSearch"),
        neither("/update", "handleUpdate"),
        both("/update_many", "handleUpdateMany", POSITIONAL),
        neither("/delete", "handleDelete"),
        both("/delete_many", "handleDeleteMany", REPORT_ONLY),
        collectionExempt("/resolve", "handleResolve", RESOLVE_BOUNDED),
        neither("/stats", "handleStats"),

        // ── Links ─────────────────────────────────────────────────────────
        neither("/link", "handleLink"),
        neither("/unlink", "handleUnlink"),
        collectionExempt("/links", "handleLinks", UNBOUNDED_NOT_PAGED),
        collectionOk("/link_query", "handleLinkQuery"),
        both("/traverse", "handleTraverse", GRAPH_TRUNCATION_GENUINE_GAP),

        // ── Manifest ──────────────────────────────────────────────────────
        neither("/manifest/write", "handleManifestWrite"),
        neither("/manifest/append", "handleManifestAppend"),
        idListOk("/manifest/write_many", "handleManifestWriteMany"),
        collectionOk("/manifest/get", "handleManifestGet"),
        both("/manifest/get_many", "handleManifestGetMany", null),
        neither("/manifest/purge", "handleManifestPurge"),
        collectionOk("/manifest/chashes", "handleManifestChashes"),
        both("/manifest/docs_for_chashes", "handleDocsForChashes", null),
        neither("/manifest/resync", "handleManifestResync"),
        neither("/manifest/backfill", "handleManifestBackfill"),
        collectionOk("/manifest/orphans", "handleManifestOrphans"),
        collectionOk("/links/orphaned", "handleLinksOrphaned"),

        // ── Owners ────────────────────────────────────────────────────────
        neither("/owners/upsert", "handleOwnerUpsert"),
        collectionExempt("/owners/list", "handleOwnerList", ADMIN_SCALE),
        neither("/owners/by_repo", "handleOwnerByRepo"),
        collectionExempt("/owners/by_name", "handleOwnerByName", ADMIN_SCALE),
        neither("/owners/head_hash", "handleOwnerHeadHash"),
        neither("/owners/show", "handleOwnerShow"),
        collectionExempt("/owners/by_type", "handleOwnerByType", ADMIN_SCALE),

        // ── Collections ───────────────────────────────────────────────────
        neither("/collections/upsert", "handleCollectionUpsert"),
        collectionExempt("/collections/list", "handleCollectionList", ADMIN_SCALE),
        neither("/collections/get", "handleCollectionGet"),
        neither("/collections/supersede", "handleCollectionSupersede"),
        neither("/collections/rename", "handleCollectionRename"),
        neither("/collections/delete", "handleCollectionDelete"),
        neither("/collections/for_tuple", "handleCollectionForTuple"),
        neither("/collections/health", "handleCollectionHealth"),

        // ── ETL imports (rows are whole objects for one batch, not an
        // open-ended identifier list -- see class javadoc's idListAccepting
        // definition) ─────────────────────────────────────────────────────
        neither("/import/owner", "handleImportOwner"),
        neither("/import/document", "handleImportDocument"),
        neither("/import/link", "handleImportLink"),
        neither("/import/chunk", "handleImportChunk"),
        neither("/import/collection", "handleImportCollection"),

        // ── Coverage analytics ───────────────────────────────────────────
        collectionExempt("/coverage", "handleCoverage", SMALL_FIXED_CARDINALITY),

        // ── Analytics queries (nexus-xnz0o CLI port helpers) ─────────────
        collectionExempt("/docs/distinct-collections", "handleDocsDistinctCollections", ADMIN_SCALE),
        neither("/docs/collection-counts", "handleDocsCollectionCounts"),
        collectionExempt("/docs/orphaned", "handleDocsOrphaned", UNBOUNDED_NOT_PAGED),
        collectionExempt("/docs/absolute-paths", "handleDocsAbsolutePaths", UNBOUNDED_NOT_PAGED),
        collectionExempt("/owners/all-with-roots", "handleOwnersWithRoots", ADMIN_SCALE),
        neither("/collections/owner-root", "handleCollectionOwnerRoot"),

        // ── Scoring hot-path batch endpoints (nexus-qnp5s) -- FIXED during
        // this gate's authorship: neither had a MAX_BATCH_DOC_IDS guard
        // before now (see class javadoc) ──────────────────────────────────
        idListOk("/docs/chunk-counts", "handleDocChunkCounts"),
        idListOk("/links/from-batch", "handleLinksFromBatch"),

        // ── Batch resolve ─────────────────────────────────────────────────
        idListOk("/resolve_many", "handleResolveMany"),

        // ── Span / chash resolution ───────────────────────────────────────
        neither("/resolve_span", "handleResolveSpan"),
        neither("/resolve_chash", "handleResolveChash"),
        neither("/resolve_chunk", "handleResolveChunk"),

        // ── Server-side tumbler assignment ────────────────────────────────
        neither("/doc/register", "handleDocRegister"),
        both("/doc/register_many", "handleRegisterMany", POSITIONAL),

        // ── GC audit ──────────────────────────────────────────────────────
        neither("/gc_audit/record", "handleGcAuditRecord"),
        collectionExempt("/gc_audit/list", "handleGcAuditList",
            "already paginated via limit/offset (default 100) -- a NEW truncation concept "
            + "beyond the existing pagination contract; no established downstream "
            + "destructive-decision consumer"),

        // ── Migration count verification ──────────────────────────────────
        idListExempt("/verify/relation-counts", "handleRelationCounts", WHITELISTED_NOT_BIND_RISK)
    );

    // ── Source scanning (self-contained -- see class javadoc: no cross-
    // package reuse of RawSqlGateTest's package-private helpers) ───────────

    /** Blank out comments only (keep string literals AND code) -- the
     * opposite tradeoff from RawSqlGateTest.blank(), because the {@code
     * "count"} / {@code MAX_BATCH_DOC_IDS} tokens THIS gate searches for live
     * inside string literals and real code, and must not be confused with a
     * mention in a javadoc comment (the same comment/string non-excusal
     * discipline as TombstoneFilterGateTest, applied in the other
     * direction: comments must not COUNT either). */
    static String blankComments(String src) {
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
                i++;
                while (i < n && out[i] != q) {
                    if (src.charAt(i) == '\\' && i + 1 < n) i++;
                    i++;
                }
                i++;
            } else {
                i++;
            }
        }
        return new String(out);
    }

    /** Brace-depth region [start,end) of a method whose declaration line
     * contains {@code name(} -- same algorithm family as RawSqlGateTest's
     * sanctionedRegions, reimplemented locally (package-private visibility
     * blocks cross-package reuse; see class javadoc). */
    static int[] methodBody(String commentBlanked, String name) {
        Matcher m = Pattern.compile("\\b" + Pattern.quote(name) + "\\s*\\(").matcher(commentBlanked);
        while (m.find()) {
            int before = m.start() - 1;
            if (before >= 0 && (commentBlanked.charAt(before) == '.'
                    || Character.isJavaIdentifierPart(commentBlanked.charAt(before)))) {
                continue;
            }
            int i = commentBlanked.indexOf('(', m.start());
            int depth = 0;
            while (i < commentBlanked.length()) {
                char c = commentBlanked.charAt(i);
                if (c == '(') depth++;
                else if (c == ')' && --depth == 0) break;
                i++;
            }
            if (i >= commentBlanked.length()) continue;
            int j = i + 1;
            // skip "throws XyzException" etc between the signature and '{'
            while (j < commentBlanked.length() && commentBlanked.charAt(j) != '{'
                    && commentBlanked.charAt(j) != ';') {
                j++;
            }
            if (j >= commentBlanked.length() || commentBlanked.charAt(j) != '{') continue;
            int braces = 0;
            int k = j;
            while (k < commentBlanked.length()) {
                char c = commentBlanked.charAt(k);
                if (c == '{') braces++;
                else if (c == '}' && --braces == 0) break;
                k++;
            }
            return new int[] {j, Math.min(k + 1, commentBlanked.length())};
        }
        return null;
    }

    private static final Pattern CASE_LABEL =
        Pattern.compile("case\\s+\"([^\"]+)\"\\s*->\\s*(\\w+)\\(");

    /** Route -> handler-method pairs, parsed live from the switch. */
    static java.util.LinkedHashMap<String, String> liveSwitchRoutes(String src) throws IOException {
        // Scope to the switch (op) block so a route-shaped string elsewhere
        // in the file (a comment, the class javadoc's route table) can never
        // masquerade as a live case label.
        int switchStart = src.indexOf("switch (op) {");
        assertThat(switchStart).as("switch (op) { block not found -- CatalogHandler restructured?").isGreaterThan(-1);
        int defaultIdx = src.indexOf("default ->", switchStart);
        assertThat(defaultIdx).as("default -> arm not found in the switch").isGreaterThan(-1);
        String switchBody = src.substring(switchStart, defaultIdx);
        var out = new java.util.LinkedHashMap<String, String>();
        Matcher m = CASE_LABEL.matcher(switchBody);
        while (m.find()) out.put(m.group(1), m.group(2));
        return out;
    }

    // ── Gate 1: exhaustiveness (the "new route fails the gate" mechanism) ──

    @Test
    void everySwitchRouteIsClassified() throws IOException {
        String src = Files.readString(HANDLER_FILE);
        var live = liveSwitchRoutes(src);

        Set<String> liveRoutes = new LinkedHashSet<>(live.keySet());
        Set<String> classifiedRoutes = new LinkedHashSet<>();
        for (var r : ROUTES) classifiedRoutes.add(r.route());

        assertThat(classifiedRoutes)
            .as("every route in CatalogHandler's switch must have a RouteSpec entry -- a "
                + "route present in the switch but missing here (added-without-classification) "
                + "or present here but removed from the switch (stale entry) both fail: "
                + "live-only=%s classified-only=%s",
                diff(liveRoutes, classifiedRoutes), diff(classifiedRoutes, liveRoutes))
            .isEqualTo(liveRoutes);

        // Handler-name cross-check: catches a route whose case label is
        // unchanged but was repointed to a different handler method without
        // this table being updated to match (a silent re-classification).
        for (var r : ROUTES) {
            assertThat(live.get(r.route()))
                .as("route %s: RouteSpec names handler %s but the live switch dispatches to %s",
                    r.route(), r.handler(), live.get(r.route()))
                .isEqualTo(r.handler());
        }
    }

    private static Set<String> diff(Set<String> a, Set<String> b) {
        var d = new LinkedHashSet<>(a);
        d.removeAll(b);
        return d;
    }

    // ── Gate 2: collection-returning routes emit count or are exempt ───────

    @Test
    void collectionReturningRoutesEmitCountOrAreExempt() throws IOException {
        String src = Files.readString(HANDLER_FILE);
        String blanked = blankComments(src);
        var violations = new ArrayList<String>();

        for (var r : ROUTES) {
            if (!r.collectionReturning()) continue;
            if (r.countExempt() != null) continue;  // exemption checked for non-null rationale below
            int[] region = methodBody(blanked, r.handler());
            assertThat(region)
                .as("route %s: handler method %s not found in CatalogHandler.java", r.route(), r.handler())
                .isNotNull();
            String body = blanked.substring(region[0], region[1]);
            boolean hasCount = body.contains("\"count\"") || body.contains("\\\"count\\\"");
            if (!hasCount) {
                violations.add(r.route() + " (" + r.handler()
                    + "): collection-returning with no \"count\" token in its handler body -- "
                    + "emit one, or add a countExempt rationale to its RouteSpec");
            }
        }
        assertThat(violations)
            .as("collection-returning routes missing count and missing an exemption: %s", violations)
            .isEmpty();
    }

    /** Every {@code countExempt}/{@code capExempt} rationale must be
     * non-blank (rationale-as-data, not a bare boolean escape hatch). */
    @Test
    void everyExemptionCarriesANonBlankRationale() {
        for (var r : ROUTES) {
            if (r.countExempt() != null) {
                assertThat(r.countExempt().isBlank())
                    .as("route %s: countExempt rationale must not be blank", r.route()).isFalse();
            }
            if (r.capExempt() != null) {
                assertThat(r.capExempt().isBlank())
                    .as("route %s: capExempt rationale must not be blank", r.route()).isFalse();
            }
        }
    }

    // ── Gate 3: id-list-accepting routes enforce the batch cap or are exempt ──

    @Test
    void idListAcceptingRoutesEnforceCapOrAreExempt() throws IOException {
        String src = Files.readString(HANDLER_FILE);
        String blanked = blankComments(src);
        var violations = new ArrayList<String>();

        for (var r : ROUTES) {
            if (!r.idListAccepting()) continue;
            if (r.capExempt() != null) continue;
            int[] region = methodBody(blanked, r.handler());
            assertThat(region)
                .as("route %s: handler method %s not found in CatalogHandler.java", r.route(), r.handler())
                .isNotNull();
            String body = blanked.substring(region[0], region[1]);
            if (!body.contains("MAX_BATCH_DOC_IDS")) {
                violations.add(r.route() + " (" + r.handler()
                    + "): id-list-accepting with no MAX_BATCH_DOC_IDS guard in its handler body "
                    + "-- enforce the cap, or add a capExempt rationale to its RouteSpec");
            }
        }
        assertThat(violations)
            .as("id-list-accepting routes missing the batch cap and missing an exemption: %s", violations)
            .isEmpty();
    }

    // ── Non-vacuity floors ───────────────────────────────────────────────

    @Test
    void floor_totalRouteCount() {
        assertThat(ROUTES.size())
            .as("total classified routes -- re-counted on the final (post by_doc_id-removal) tree")
            .isGreaterThanOrEqualTo(65);
    }

    @Test
    void floor_collectionReturningRoutes() {
        long n = ROUTES.stream().filter(RouteSpec::collectionReturning).count();
        assertThat(n).isGreaterThanOrEqualTo(20);
    }

    @Test
    void floor_idListAcceptingRoutes() {
        long n = ROUTES.stream().filter(RouteSpec::idListAccepting).count();
        assertThat(n).isGreaterThanOrEqualTo(10);
    }

    // ── Meta-test: a new, unclassified route must fail the gate ────────────

    /**
     * Direct test of the exhaustiveness mechanism itself: a synthetic switch
     * source with one MORE case than the classified table must produce a
     * non-empty live-only diff (proving {@link #everySwitchRouteIsClassified}
     * would fail on a real new-route addition), and a synthetic switch with
     * one FEWER case must produce a non-empty classified-only diff (a stale
     * entry is caught too, not just a missing one).
     */
    @Test
    void newRouteWithoutClassification_wouldFailTheGate() {
        Set<String> classified = new LinkedHashSet<>();
        for (var r : ROUTES) classified.add(r.route());

        Set<String> liveWithNewRoute = new LinkedHashSet<>(classified);
        liveWithNewRoute.add("/brand_new_unclassified_route");
        assertThat(diff(liveWithNewRoute, classified))
            .as("a route present in the switch but absent from ROUTES must show up as a "
                + "live-only diff -- this is what makes an unclassified new route fail "
                + "everySwitchRouteIsClassified")
            .containsExactly("/brand_new_unclassified_route");

        Set<String> liveMissingOne = new LinkedHashSet<>(classified);
        liveMissingOne.remove("/stats");
        assertThat(diff(classified, liveMissingOne))
            .as("a RouteSpec entry whose route was removed from the switch must show up as a "
                + "classified-only diff (a stale entry, caught too)")
            .containsExactly("/stats");
    }

    // ── Meta-test: comment-only non-excusal (the token must be real code) ──

    @Test
    void countTokenInCommentAlone_doesNotExcuseAMissingRealOne() {
        String synthetic = String.join("\n",
            "public final class Synthetic {",
            "    /** Response: {\"documents\": [...], \"count\": N} -- or so this comment claims. */",
            "    private void handleFake(Object exchange, String tenant, String method) {",
            "        var docs = List.of();",
            "        send(exchange, 200, mapper.writeValueAsString(Map.of(\"documents\", docs)));",
            "    }",
            "}");
        String blanked = blankComments(synthetic);
        int[] region = methodBody(blanked, "handleFake");
        assertThat(region).isNotNull();
        String body = blanked.substring(region[0], region[1]);
        assertThat(body.contains("\"count\"") || body.contains("\\\"count\\\""))
            .as("a comment CLAIMING the response carries count must not satisfy the real check "
                + "-- blankComments erases the comment, leaving only the actual (count-less) code")
            .isFalse();
    }
}
