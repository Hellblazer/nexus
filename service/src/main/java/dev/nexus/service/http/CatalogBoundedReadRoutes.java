// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.CatalogDocReads;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CatalogSensitiveLocatorReads;
import dev.nexus.service.db.CatalogSensitiveReads;
import dev.nexus.service.db.CompletionTenantCountReads;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.CompletionTenantCounts;
import dev.nexus.service.db.ManifestFillableCount;
import dev.nexus.service.db.ManifestForeignCollectionReads;
import dev.nexus.service.db.UriSegmentCensusReads;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * nexus-f3yqd: the catalog's bounded read routes added in engine 0.1.188, split out of {@link CatalogHandler} (which
 * is at its file-size cap). Each is read-only, tenant-scoped, and runs under the section-label count's statement
 * bound ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}); a read that outlasts it answers 503
 * {@code {"error":..., "reason":"statement_timeout"}} with none of the answer. A client that gets a 404 is on an
 * engine before the route and one that gets a 503 is on an engine that could not answer in time; both must read
 * "cannot say", never "none".
 *
 * <p>RDR-241 Phase 2 adds three more (nexus-uwggq.2.3 to .5), each a new path so an old engine answers 404:
 * {@code GET /v1/catalog/docs/by-author}, {@code GET /v1/catalog/docs/by-meta} (both paged, {@code {count, limit, offset,
 * documents}} with the exact count and no {@code capped}) and {@code GET /v1/catalog/sensitive/locators} (the
 * {@code /sensitive} envelope). Their {@code /version} flags are {@code catalog_docs_by_author_supported},
 * {@code catalog_docs_by_meta_supported} and {@code catalog_sensitive_locators_supported}.
 */
final class CatalogBoundedReadRoutes {

    private static final Logger log = LoggerFactory.getLogger(CatalogBoundedReadRoutes.class);

    private final CatalogSensitiveReads sensitiveReads;
    private final CatalogSensitiveLocatorReads locatorReads;
    private final ManifestFillableCount fillableCount;
    private final ManifestForeignCollectionReads foreignReads;
    private final UriSegmentCensusReads uriSegmentReads;
    private final CompletionTenantCountReads completionCountReads;
    private final CatalogDocReads docReads;

    CatalogBoundedReadRoutes(CatalogRepository repo) {
        this.sensitiveReads = new CatalogSensitiveReads(repo);
        this.locatorReads = new CatalogSensitiveLocatorReads(repo);
        this.fillableCount = new ManifestFillableCount(repo);
        this.foreignReads = new ManifestForeignCollectionReads(repo);
        this.uriSegmentReads = new UriSegmentCensusReads(repo);
        this.completionCountReads = new CompletionTenantCountReads(repo);
        this.docReads = new CatalogDocReads(repo);
    }

    /**
     * GET /v1/catalog/sensitive?limit=N: {@code {count, limit, capped, documents:[{tumbler, physical_collection}]}}.
     *
     * <p>{@code count} is every live sensitive document of the tenant; {@code documents} lists at most {@code limit}
     * of them in tumbler order, and {@code capped} is true when more than {@code limit} exist. {@code limit} is 1 to
     * {@link CatalogSensitiveReads#MAX_LIMIT} (default {@link CatalogSensitiveReads#DEFAULT_LIMIT}); anything else is
     * a 400. A new path rather than a parameter on {@code /list}: an old engine would ignore the parameter and answer
     * a 200 that looks like "no sensitive documents" (the {@code /docs/collection-counts-all} rule, nexus-8tnz2).
     */
    void sensitive(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Integer limit = limitParam(exchange, CatalogSensitiveReads.DEFAULT_LIMIT, CatalogSensitiveReads.MAX_LIMIT);
        if (limit == null) return;
        CatalogSensitiveReads.Page page;
        try {
            page = sensitiveReads.list(tenant, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_sensitive_list_timeout tenant={} limit={}", tenant, limit);
            timeout(exchange, "sensitive list exceeded its time bound");
            return;
        }
        List<Map<String, Object>> docs = new ArrayList<>(page.documents().size());
        for (var d : page.documents()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("tumbler", d.tumbler());
            m.put("physical_collection", d.physicalCollection());
            docs.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", page.count());
        body.put("limit", page.limit());
        body.put("capped", page.capped());
        body.put("documents", docs);
        send(exchange, 200, body);
    }

    /**
     * GET /v1/catalog/manifest/fillable?kind=labels|spans&limit=N:
     * {@code {kind, fillable, limit, capped, rule:"title rule 2", stamp:"strict", edge}}.
     *
     * <p>The strict, bounded count of the manifest rows the two upgrade rungs would fill ({@link ManifestFillableCount}
     * has the predicate). {@code kind} is required and exact ({@code labels} or {@code spans}); {@code limit} is 1 to
     * {@link ManifestFillableCount#MAX_LIMIT} (default {@link ManifestFillableCount#DEFAULT_LIMIT}); anything else is a
     * 400. The scan stops at {@code limit} candidate rows (rows that pass every guard but the edges below, strict or not);
     * {@code capped} is true when it did, and then BOTH {@code fillable} and {@code edge} are lower bounds ("at least
     * these") and a client MUST read capped as pending whatever the numbers say (capped with {@code fillable} 0 and
     * {@code edge} equal to {@code limit} included). Only an uncapped answer is exact.
     * The older {@code /manifest/section_label_fillable} stays for old clients; it counts a chunk stamp it cannot read,
     * this route does not ({@code stamp:"strict"}).
     *
     * <p>{@code edge} is informational: the count (cut at {@code limit} too) of rows that pass every guard of
     * {@code fillable} EXCEPT an edge where the SQL form cannot say what the Python rung says: a chunk stamp the strict
     * form rejects but {@code fromisoformat} may read, or a label or page value that is not a string, null or integer
     * (including falsy non-strings Python turns into the empty label) or does not fit INTEGER. Every such row is one the
     * strict {@code fillable} leaves out, so {@code fillable == 0} alone can be a false "converged". A client reads
     * {@code fillable == 0 && edge > 0} (uncapped) as "scan once to see" (the scan's completion record then ends it) and
     * uncapped {@code fillable == 0 && edge == 0} as converged; {@code fillable > 0} is pending whatever {@code edge} says.
     */
    void fillable(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String rawKind = queryParam(exchange, "kind");
        ManifestFillableCount.Kind kind = rawKind == null ? null : ManifestFillableCount.Kind.parse(rawKind);
        if (kind == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"kind is required and must be labels or spans\"}");
            return;
        }
        Integer limit = limitParam(exchange, ManifestFillableCount.DEFAULT_LIMIT, ManifestFillableCount.MAX_LIMIT);
        if (limit == null) return;
        ManifestFillableCount.Result result;
        try {
            result = fillableCount.count(tenant, kind, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_manifest_fillable_timeout tenant={} kind={} limit={}", tenant, kind.wire(), limit);
            timeout(exchange, "manifest fillable count exceeded its time bound");
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("kind", result.kind().wire());
        body.put("fillable", result.fillable());
        body.put("limit", result.limit());
        body.put("capped", result.capped());
        body.put("rule", ManifestFillableCount.RULE);
        body.put("stamp", ManifestFillableCount.STAMP);
        body.put("edge", result.edge());
        send(exchange, 200, body);
    }

    /**
     * GET /v1/catalog/manifest/foreign_collection?limit=N (GH #1611):
     * {@code {count, limit, capped, documents:[{tumbler, physical_collection, collections, chunks}]}}.
     *
     * <p>The tenant's live documents whose manifest names chunks outside the document's own
     * {@code physical_collection} ({@link ManifestForeignCollectionReads} has the predicate and the plan note):
     * {@code collections} are the other collections named, {@code chunks} the manifest rows naming them.
     * {@code count} is every such document; {@code documents} lists at most {@code limit} in tumbler order and
     * {@code capped} is true when more exist. {@code limit} is 1 to {@link ManifestForeignCollectionReads#MAX_LIMIT}
     * (default {@link ManifestForeignCollectionReads#DEFAULT_LIMIT}); anything else is a 400. A new path, so an old
     * engine answers 404, which a client reads as "cannot say".
     */
    void foreignCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Integer limit = limitParam(exchange, ManifestForeignCollectionReads.DEFAULT_LIMIT, ManifestForeignCollectionReads.MAX_LIMIT);
        if (limit == null) return;
        ManifestForeignCollectionReads.Page page;
        try {
            page = foreignReads.list(tenant, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_manifest_foreign_collection_timeout tenant={} limit={}", tenant, limit);
            timeout(exchange, "manifest foreign-collection read exceeded its time bound");
            return;
        }
        List<Map<String, Object>> docs = new ArrayList<>(page.documents().size());
        for (var d : page.documents()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("tumbler", d.tumbler());
            m.put("physical_collection", d.physicalCollection());
            m.put("collections", d.collections());
            m.put("chunks", d.chunks());
            docs.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", page.count());
        body.put("limit", page.limit());
        body.put("capped", page.capped());
        body.put("documents", docs);
        send(exchange, 200, body);
    }

    /**
     * GET /v1/catalog/uri_segment_census?limit=N (RDR-229 P1.2, nexus-p2m1x.2, engine 0.1.200):
     * {@code {count, limit, capped, groups:[{table, scheme, segment, collection, content_type, rows}]}}.
     *
     * <p>The tenant's {@code chroma://} and {@code nx-note://} source-URI rows grouped by table
     * ({@code catalog_documents}, {@code document_aspects}, {@code document_highlights}), scheme ({@code chroma} or
     * {@code nx-note}), segment and collection, each with its row count and its collection's registry content type
     * ({@link UriSegmentCensusReads} has the statement). {@code count} is every group; {@code groups} lists at most
     * {@code limit} of them (1 to {@link UriSegmentCensusReads#MAX_LIMIT}, default the maximum; anything else is a 400);
     * {@code capped} is true when more exist. A new path, so an old engine answers 404, which a client reads as
     * "cannot say", never "none". A statement timeout answers 503.
     */
    void uriSegmentCensus(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Integer limit = limitParam(exchange, UriSegmentCensusReads.DEFAULT_LIMIT, UriSegmentCensusReads.MAX_LIMIT);
        if (limit == null) return;
        UriSegmentCensusReads.Page page;
        try {
            page = uriSegmentReads.list(tenant, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_uri_segment_census_timeout tenant={} limit={}", tenant, limit);
            timeout(exchange, "uri segment census exceeded its time bound");
            return;
        }
        List<Map<String, Object>> groups = new ArrayList<>(page.groups().size());
        for (var g : page.groups()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("table", g.table());
            m.put("scheme", g.scheme());
            m.put("segment", g.segment());
            m.put("collection", g.collection());
            m.put("content_type", g.contentType());
            m.put("rows", g.rows());
            groups.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", page.count());
        body.put("limit", page.limit());
        body.put("capped", page.capped());
        body.put("groups", groups);
        send(exchange, 200, body);
    }

    /**
     * GET /v1/catalog/rdr229_completion_counts (RDR-229 P3.1, nexus-p2m1x.17, review N2): the CALLER'S tenant's counts for the doctor,
     * which the unauthenticated {@code /v1/status} {@code rdr229_completion} field does not carry.
     * {@code {registry_rows, live_collections, superseded_by_waiting, dangling_tombstones:{total, by_content_type, self_id:{total,
     * by_content_type}}, tables:[{table, orphans, unstamped}]}}: the tenant's registry rows and live collections, the registry rows that
     * hold the superseded_by CHECK back, its tombstones that name no successor and those the stamp resolved to their own id (by content
     * type), and, for each table whose CHECK is NOT VALID, the tenant's orphan and unstamped rows (tables with none are not listed).
     * Never another tenant's count and never a total ({@link CompletionTenantCounts}). No parameters. A new path, so an old engine
     * answers 404, which a client reads as "cannot say". A statement timeout answers 503.
     */
    void rdr229CompletionCounts(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        CompletionTenantCounts.Counts counts;
        try {
            counts = completionCountReads.read(tenant);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_rdr229_completion_counts_timeout tenant={}", tenant);
            timeout(exchange, "rdr229 completion counts exceeded their time bound");
            return;
        }
        CompletionCheck.Dangling d = counts.dangling();
        Map<String, Object> dangling = new LinkedHashMap<>();
        dangling.put("total", d.total());
        dangling.put("by_content_type", d.byContentType());
        Map<String, Object> selfId = new LinkedHashMap<>();
        selfId.put("total", d.selfIdTotal());
        selfId.put("by_content_type", d.selfIdByContentType());
        dangling.put("self_id", selfId);
        List<Map<String, Object>> tables = new ArrayList<>(counts.tables().size());
        for (var t : counts.tables()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("table", t.table());
            m.put("orphans", t.orphans());
            m.put("unstamped", t.unstamped());
            tables.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("registry_rows", counts.registryRows());
        body.put("live_collections", counts.liveCollections());
        body.put("superseded_by_waiting", counts.supersededByWaiting());
        body.put("dangling_tombstones", dangling);
        body.put("tables", tables);
        send(exchange, 200, body);
    }

    /**
     * GET /v1/catalog/sensitive/locators?limit=N (nexus-uwggq.2.5): {@code {count, limit, capped, documents:[{tumbler,
     * source_uri, file_path, alias_of, physical_collection, sensitive}]}}, the envelope of {@code /sensitive}. {@code sensitive}
     * is the row's own flag (an alias row reads false unless it is itself flagged), so {@code _restrict} takes the rows raw.
     *
     * <p>The tenant's live sensitive documents and the live documents whose {@code alias_of} names a live sensitive one
     * ({@link CatalogSensitiveLocatorReads} has the statement), in tumbler order; {@code limit} is 1 to 10,000 (default
     * 1,000), anything else a 400; {@code capped} is true when more than {@code limit} rows exist.
     */
    void sensitiveLocators(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        if (rejectBadQuery(exchange)) return;
        Integer limit = limitParam(exchange, CatalogSensitiveLocatorReads.DEFAULT_LIMIT, CatalogSensitiveLocatorReads.MAX_LIMIT);
        if (limit == null) return;
        CatalogSensitiveLocatorReads.Page page;
        try {
            page = locatorReads.list(tenant, limit);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_sensitive_locators_timeout tenant={} limit={}", tenant, limit);
            timeout(exchange, "sensitive locators read exceeded its time bound");
            return;
        }
        List<Map<String, Object>> docs = new ArrayList<>(page.documents().size());
        for (var d : page.documents()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("tumbler", d.tumbler());
            m.put("source_uri", d.sourceUri());
            m.put("file_path", d.filePath());
            m.put("alias_of", d.aliasOf());
            m.put("physical_collection", d.physicalCollection());
            m.put("sensitive", d.sensitive());
            docs.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", page.count());
        body.put("limit", page.limit());
        body.put("capped", page.capped());
        body.put("documents", docs);
        send(exchange, 200, body);
    }

    /**
     * GET /v1/catalog/docs/by-author?author=TEXT&limit=N&offset=M (nexus-uwggq.2.3): {@code {count, limit, offset,
     * documents}}. {@code author} is required and non-empty, {@code limit} 1 to 1,000 (default 1,000), {@code offset}
     * 0 or more; anything else is a 400. The rows are the live documents whose author contains the text,
     * case-insensitively ({@link CatalogDocReads} has the rule), in tumbler order; {@code count} is the exact number of
     * live matches, independent of the page, and there is no {@code capped}: a caller pages with {@code offset}.
     */
    void byAuthor(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        if (rejectBadQuery(exchange)) return;
        String author = queryParam(exchange, "author");
        if (author == null || author.isEmpty()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"author is required and must not be empty\"}");
            return;
        }
        Integer limit = limitParam(exchange, CatalogDocReads.DEFAULT_LIMIT, CatalogDocReads.MAX_LIMIT);
        if (limit == null) return;
        Integer offset = offsetParam(exchange);
        if (offset == null) return;
        CatalogDocReads.Page page;
        try {
            page = docReads.byAuthor(tenant, author, limit, offset);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_docs_by_author_timeout tenant={} limit={} offset={}", tenant, limit, offset);
            timeout(exchange, "author read exceeded its time bound");
            return;
        }
        sendPage(exchange, page);
    }

    /**
     * GET /v1/catalog/docs/by-scheme?scheme=S&limit=N&offset=M (nexus-uwggq.1.5): {@code {count, limit, offset, documents}}
     * as {@code /docs/by-author}. The live documents whose {@code urlparse(source_uri).scheme} is {@code S} lowercased, which
     * is what the client's reference-only check keeps today; {@link CatalogDocReads} cites the lines of CPython's urlsplit
     * the statement mirrors. {@code scheme} is required and must be an RFC 3986 scheme (a letter, then letters, digits and
     * {@code + - .}; a literal {@code +} in a query decodes to a space, so a client sends {@code %2B}), else 400.
     */
    void byScheme(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        if (rejectBadQuery(exchange)) return;
        String scheme = queryParam(exchange, "scheme");
        if (scheme == null || !CatalogDocReads.SCHEME_GRAMMAR.matcher(scheme).matches()) {
            badRequest(exchange, "scheme is required and must be an RFC 3986 scheme (a letter, then letters, digits, + - .)");
            return;
        }
        Integer limit = limitParam(exchange, CatalogDocReads.DEFAULT_LIMIT, CatalogDocReads.MAX_LIMIT);
        if (limit == null) return;
        Integer offset = offsetParam(exchange);
        if (offset == null) return;
        CatalogDocReads.Page page;
        try {
            page = docReads.byScheme(tenant, scheme, limit, offset);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_docs_by_scheme_timeout tenant={} limit={} offset={}", tenant, limit, offset);
            timeout(exchange, "scheme read exceeded its time bound");
            return;
        }
        sendPage(exchange, page);
    }

    private static final Pattern BOUND_TEXT = Pattern.compile("-?[0-9]+");
    private static final Set<String> BY_META_PLAIN = Set.of("content_type", "limit", "offset", "sensitive");

    /**
     * GET /v1/catalog/docs/by-meta?content_type=&meta.KEY=VALUE&meta_min.KEY=N&meta_default.KEY=TEXT&limit=&offset=
     * (nexus-uwggq.2.4): {@code {count, limit, offset, documents}} as {@code /docs/by-author}.
     *
     * <p>At least one {@code meta.}, {@code meta_min.} or {@code sensitive=true} parameter is required (a 400 otherwise), so
     * the route can never serve as an unfiltered listing; {@code content_type} is optional and exact. {@link CatalogDocReads}
     * has the reading rules. {@code sensitive=true} (the only value; any other is a 400) keeps only the live documents whose
     * {@code sensitive} flag is set, and is itself a filter clause: {@code sensitive=true} alone, or with {@code content_type},
     * pages every live sensitive document however many there are (past the 10,000 cap of {@code /sensitive}). A
     * {@code meta_min} value that is not an integer (at most 131,072 characters, the digit
     * limit of PostgreSQL's numeric), an empty key, a {@code meta_default.KEY} with no
     * {@code meta.KEY} beside it, a repeated {@code meta_default.KEY}, and any parameter this route does not name are
     * each a 400: a misspelt clause must never widen the answer.
     */
    void byMeta(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        if (rejectBadQuery(exchange)) return;
        String contentType = null;
        boolean sensitiveOnly = false;
        List<String[]> eq = new ArrayList<>();
        List<CatalogDocReads.MetaMin> bounds = new ArrayList<>();
        Map<String, String> defaults = new LinkedHashMap<>();
        String raw = exchange.getRequestURI().getRawQuery();
        for (String part : raw == null || raw.isEmpty() ? new String[0] : raw.split("&")) {
            String[] kv = part.split("=", 2);
            String key = decode(kv[0]);
            String value = kv.length == 2 ? decode(kv[1]) : "";
            if (BY_META_PLAIN.contains(key)) {
                if (key.equals("content_type")) contentType = value.isEmpty() ? null : value;
                if (key.equals("sensitive")) {
                    if (!"true".equals(value)) { badRequest(exchange, "sensitive takes only the value true"); return; }
                    sensitiveOnly = true;
                }
                continue;   // limit and offset are read by their helpers
            }
            String family = key.startsWith("meta_min.") ? "meta_min." : key.startsWith("meta_default.") ? "meta_default."
                : key.startsWith("meta.") ? "meta." : null;
            if (family == null) { badRequest(exchange, "unknown parameter " + key); return; }
            String name = key.substring(family.length());
            if (name.isEmpty()) { badRequest(exchange, family + " needs a key"); return; }
            switch (family) {
                case "meta." -> eq.add(new String[]{name, value});
                case "meta_min." -> {
                    if (value.length() > CatalogDocReads.MAX_BOUND_DIGITS || !BOUND_TEXT.matcher(value).matches()) {
                        badRequest(exchange, "meta_min." + name + " must be an integer");
                        return;
                    }
                    bounds.add(new CatalogDocReads.MetaMin(name, new BigInteger(value)));
                }
                default -> {
                    if (defaults.put(name, value) != null) { badRequest(exchange, "meta_default." + name + " is repeated"); return; }
                }
            }
        }
        Set<String> eqKeys = new HashSet<>();
        for (String[] e : eq) eqKeys.add(e[0]);
        for (String k : defaults.keySet()) {
            if (!eqKeys.contains(k)) { badRequest(exchange, "meta_default." + k + " needs a meta." + k + " beside it"); return; }
        }
        if (eq.isEmpty() && bounds.isEmpty() && !sensitiveOnly) {
            badRequest(exchange, "at least one meta., meta_min. or sensitive=true parameter is required");
            return;
        }
        Integer limit = limitParam(exchange, CatalogDocReads.DEFAULT_LIMIT, CatalogDocReads.MAX_LIMIT);
        if (limit == null) return;
        Integer offset = offsetParam(exchange);
        if (offset == null) return;
        List<CatalogDocReads.MetaEq> eqs = new ArrayList<>(eq.size());
        for (String[] e : eq) eqs.add(new CatalogDocReads.MetaEq(e[0], e[1], defaults.get(e[0])));
        CatalogDocReads.Page page;
        try {
            page = docReads.byMeta(tenant, new CatalogDocReads.MetaFilter(contentType, eqs, bounds, sensitiveOnly), limit, offset);
        } catch (RuntimeException e) {
            if (!CatalogHandler.hasSqlState(e, CatalogRepository.QUERY_CANCELED)) throw e;
            log.warn("event=catalog_docs_by_meta_timeout tenant={} limit={} offset={}", tenant, limit, offset);
            timeout(exchange, "metadata read exceeded its time bound");
            return;
        }
        sendPage(exchange, page);
    }

    private static void sendPage(HttpExchange exchange, CatalogDocReads.Page page) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", page.count());
        body.put("limit", page.limit());
        body.put("offset", page.offset());
        body.put("documents", page.documents());
        send(exchange, 200, body);
    }

    private static void badRequest(HttpExchange exchange, String message) throws IOException {
        HttpUtil.send(exchange, 400, "{\"error\":" + HttpUtil.jsonString(message) + "}");
    }

    /**
     * Answers 400 (and returns true) for a query string the three Phase 2 routes cannot read: a parameter holding a NUL
     * byte ({@code %00}), which PostgreSQL text cannot hold and would otherwise surface as a 500, or a malformed percent
     * escape, which the decoder rejects.
     */
    private static boolean rejectBadQuery(HttpExchange exchange) throws IOException {
        String raw = exchange.getRequestURI().getRawQuery();
        if (raw == null) return false;
        try {
            if (decode(raw).indexOf('\0') >= 0) { badRequest(exchange, "a parameter holds a NUL byte"); return true; }
        } catch (IllegalArgumentException e) {
            badRequest(exchange, "a parameter is not valid percent-encoding");
            return true;
        }
        return false;
    }

    private static String decode(String s) {
        return java.net.URLDecoder.decode(s, java.nio.charset.StandardCharsets.UTF_8);
    }

    /** The {@code offset} query parameter (default 0); null after answering a 400 for a negative or non-integer one. */
    static Integer offsetParam(HttpExchange exchange) throws IOException {
        String raw = queryParam(exchange, "offset");
        if (raw == null || raw.isBlank()) return 0;
        int offset;
        try {
            offset = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            HttpUtil.send(exchange, 400, "{\"error\":\"offset must be an integer, 0 or more\"}");
            return null;
        }
        if (offset < 0) {
            HttpUtil.send(exchange, 400, "{\"error\":\"offset must be 0 or more\"}");
            return null;
        }
        return offset;
    }

    // ── shared ───────────────────────────────────────────────────────────────

    /** The {@code limit} query parameter, {@code def} when absent; null after answering a 400 for a bad one. */
    static Integer limitParam(HttpExchange exchange, int def, int max) throws IOException {
        String raw = queryParam(exchange, "limit");
        if (raw == null || raw.isBlank()) return def;
        int limit;
        try {
            limit = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            HttpUtil.send(exchange, 400, "{\"error\":\"limit must be an integer (1.." + max + ")\"}");
            return null;
        }
        if (limit < 1 || limit > max) {
            HttpUtil.send(exchange, 400, "{\"error\":\"limit must be 1.." + max + "\"}");
            return null;
        }
        return limit;
    }

    static String queryParam(HttpExchange exchange, String key) {
        String query = exchange.getRequestURI().getRawQuery();
        if (query == null) return null;
        for (String part : query.split("&")) {
            String[] kv = part.split("=", 2);
            if (kv.length == 2 && kv[0].equals(key)) {
                return java.net.URLDecoder.decode(kv[1], java.nio.charset.StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    static void timeout(HttpExchange exchange, String message) throws IOException {
        HttpUtil.send(exchange, 503, "{\"error\":" + HttpUtil.jsonString(message) + ",\"reason\":\"statement_timeout\"}");
    }

    static void send(HttpExchange exchange, int status, Map<String, Object> body) throws IOException {
        try {
            HttpUtil.send(exchange, status, CatalogHandler.MAPPER.writeValueAsString(body));
        } catch (JsonProcessingException e) {
            throw new IOException(e);
        }
    }
}
