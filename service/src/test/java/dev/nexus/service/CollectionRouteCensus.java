// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.5a write half (nexus-p2m1x.23): the census of every engine HTTP route that names a collection as a
 * request parameter, one row per route. {@link CollectionRoutesByIdTest} runs each row; {@link CollectionRouteCensusTest}
 * pins that no handler reads a collection field this table does not account for.
 *
 * <p>RDR line 2662 (P3.5a): "{@code CollectionRegistry} keyed by id with a label index; every collection-taking route
 * accepts {@code collection_id} and a label; responses carry both." Item 6 (RDR line ~1190): "Every engine route that takes
 * a collection accepts {@code collection_id}; a label is still accepted at the route boundary and resolved to the id once,
 * by {@code CollectionRegistry}".
 *
 * <p><b>The id spelling is not in the RDR</b>, so it is fixed here, once, as a convention the rows follow (the first bullet
 * of the report's ambiguity list): the id twin of a single-valued field {@code collection}, {@code name}, {@code old_name}
 * is {@code collection_id}; of a list field {@code collections} it is {@code collection_ids}; of every other field {@code X}
 * it is {@code X_id} ({@code source_collection_id}, {@code quarantine_collection_id}, {@code superseded_by_id}). In a query
 * string the id is the decimal number, in a body a JSON integer. Each row names both spellings in its {@link Slot}s, so the
 * implementation can change a spelling in one place.
 *
 * <p><b>Row tiers.</b> {@link Tier#GENERIC}: a request the handler can answer from an empty collection. {@link Tier#HEAVY}: a
 * handler whose own preconditions (a document, a chunk, an embedder round trip, a quarantine sibling, a running job) the
 * fixture does not build; the label request is allowed to refuse for those, and the row still proves the selector is
 * resolved, by the unknown-id and the both-given tests, which only need the route to read its collection first (the
 * ambiguity list names that as an assumption). {@link Tier#RENAME} and {@link Tier#DELETE} are the two routes that
 * {@link CollectionRenameRefusalTest} and {@link CollectionDeleteByIdTest} test in depth; their rows here carry only the
 * accept-both check, and RENAME is run only while gate 3 does not hold (from gate 3 it is refused, which is the other test).
 */
final class CollectionRouteCensus {

    private CollectionRouteCensus() { }

    enum Where { QUERY, BODY }

    enum Tier { GENERIC, HEAVY, RENAME, DELETE }

    /**
     * The engine a row runs against. {@link #STANDARD} is the shared fixture's engine (no embedder, not local). {@link
     * #LOCAL_EMBED} is the variant built like {@code ReembedRoutesTest}'s: an {@code EmbedderRouter}, {@code
     * NX_ENGINE_LOCAL=1} and a recording re-embed executor. The embed and re-embed routes answer 503 before they read their
     * collection on the standard engine, so no check about the selector can reach them there. Only those three rows use it
     * ({@link CollectionRouteCensusTest} pins that); the standard engine's mode is not changed for the other rows.
     */
    enum Engine { STANDARD, LOCAL_EMBED }

    /**
     * One collection-valued request field. {@code list}: the field carries an array of collections ({@code collections}).
     * A slot after the first is a second collection the route also takes (a supersede's successor, a re-home's target, a
     * quarantine sibling); the fixture registers one collection per slot, per side. {@code nestedIn}: the name of the request
     * array whose every element carries the collection field ("" for a top-level field). The route's {@code extras} then
     * belong inside the element, beside the selector, and a nested row has exactly one slot.
     */
    record Slot(String labelField, String idField, boolean list, String nestedIn) {

        /** True when the field travels inside each element of the request array {@link #nestedIn()}, not at the top level. */
        boolean nested() {
            return !nestedIn.isEmpty();
        }
    }

    /**
     * @param method   HTTP method
     * @param path     full path
     * @param where    where the collection fields travel
     * @param slots    the collection fields, in request order; the id probe sends every slot by id
     * @param extras   the rest of the request that makes it valid: for QUERY an {@code &k=v} tail, for BODY a JSON fragment
     *                 without braces ("" when none). {@code {NEW}} becomes a fresh label (rename).
     * @param tier     see {@link Tier}
     * @param handler  the handler source file under {@code service/src/main/java/dev/nexus/service/http}
     * @param readsId  the name of the handler method that reads the fields (for the report; the line is in the report)
     * @param engine   the engine the route is run against, see {@link Engine}
     * @param resolver who resolves the id, see {@link Resolver}
     * @param elementExtras for a nested slot: the request fields beside the selector inside each element of the array ("" when none)
     * @param repo     the repository methods that read the field from the request map, {@code File.java#method,method} ("" when the
     *                 handler reads it); the census accounts for their read sites by this
     */
    record Route(String method, String path, Where where, List<Slot> slots, String extras, Tier tier, String handler,
                 String readsId, Engine engine, Resolver resolver, String elementExtras, String repo) {

        String label() {
            return method + " " + path;
        }
    }

    /**
     * Who resolves the collection a row names: {@link #BOUNDARY}, {@code CollectionSelectorFilter}, which replaces the id by the
     * label before the handler runs (the route is a row of {@code CollectionSelectors}, and the table test holds the two equal); or
     * {@link #WRITE}, the repository, which resolves the id inside the transaction that writes ({@code DocumentCollectionRef}), so
     * the id reaches the write as the resolved registry row and is never turned back into a label and looked up by name. The
     * document routes are the second kind (RDR-229 P3.5a, linda's ruling D1), and the route table has no row for them on purpose.
     */
    enum Resolver { BOUNDARY, WRITE }

    /**
     * A collection field the handlers read that the by-id contract deliberately does not cover, by site. A site is a method: the
     * census accounts for every read of a collection-named request field by the method that makes it (a row's handler method, a
     * helper it calls, or this list), so a route that reads one in a method nobody listed fails the census test.
     *
     * @param file          the source file, under {@code service/src/main/java/dev/nexus/service} (a handler under {@code http/}, anything
     *                      else a repository or helper)
     * @param method        the method that reads the field
     * @param field         the field
     * @param why           why the route takes no collection reference the by-id contract could select, or what it takes instead
     * @param evidenceFile  a file whose source must still contain {@code evidence}, so the reason cannot go stale unseen
     * @param evidence      the code fragment that makes the reason true
     */
    record Excluded(String file, String method, String field, String why, String evidenceFile, String evidence) { }

    /** A method that reads a collection field on behalf of rows: it is accounted for by them, and each must call it. */
    record Helper(String file, String method, List<String> usedBy) { }

    private static final String HEX = "0000000000000000000000000000000000000000000000000000000000000001";

    private static final String CATALOG = "CatalogHandler.java";
    private static final String VECTOR = "VectorHandler.java";
    private static final String REEMBED = "ReembedHandler.java";
    private static final String TAXONOMY = "TaxonomyHandler.java";
    private static final String ASPECT = "AspectHandler.java";
    private static final String REMAP = "RemapHandler.java";
    private static final String PIPELINE = "PipelineHandler.java";
    private static final String TELEMETRY = "TelemetryHandler.java";
    private static final String CHASH = "ChashHandler.java";

    /** The repository files whose methods read a collection field from the request map ({@link Route#repo()}). */
    private static final String CATALOG_REPO = "CatalogRepository.java";
    private static final String ASPECT_REPO = "AspectRepository.java";

    /** A 384-wide vector, the width of the fixture's minilm collections, for a centroid the engine will accept. */
    private static final String EMBEDDING_384 = "[" + String.join(",", java.util.Collections.nCopies(384, "0.1")) + "]";

    private static Slot one(String labelField, String idField) {
        return new Slot(labelField, idField, false, "");
    }

    /** A single-valued collection field inside each element of the request array {@code array} (RDR convention: {@code collection_id}). */
    private static Slot nested(String array, String labelField, String idField) {
        return new Slot(labelField, idField, false, array);
    }

    private static Slot coll() {
        return one("collection", "collection_id");
    }

    private static Slot name() {
        return one("name", "collection_id");
    }

    private static Slot many() {
        return new Slot("collections", "collection_ids", true, "");
    }

    private static Route get(String path, Slot slot, String extras, Tier tier, String handler, String method) {
        return new Route("GET", path, Where.QUERY, List.of(slot), extras, tier, handler, method, Engine.STANDARD,
            Resolver.BOUNDARY, "", "");
    }

    private static Route post(String path, List<Slot> slots, String extras, Tier tier, String handler, String method) {
        return new Route("POST", path, Where.BODY, slots, extras, tier, handler, method, Engine.STANDARD,
            Resolver.BOUNDARY, "", "");
    }

    /** {@code route}, run against the {@link Engine#LOCAL_EMBED} engine. */
    private static Route onLocalEmbed(Route route) {
        return new Route(route.method(), route.path(), route.where(), route.slots(), route.extras(), route.tier(),
            route.handler(), route.readsId(), Engine.LOCAL_EMBED, route.resolver(), route.elementExtras(), route.repo());
    }

    /** {@code route}, whose request fields beside a nested selector are {@code elementExtras}. */
    private static Route withElement(Route route, String elementExtras) {
        return new Route(route.method(), route.path(), route.where(), route.slots(), route.extras(), route.tier(),
            route.handler(), route.readsId(), route.engine(), route.resolver(), elementExtras, route.repo());
    }

    /** {@code route}, whose field a repository method reads from the request map: {@code File.java#method,method}. */
    private static Route withRepo(Route route, String repo) {
        return new Route(route.method(), route.path(), route.where(), route.slots(), route.extras(), route.tier(),
            route.handler(), route.readsId(), route.engine(), route.resolver(), route.elementExtras(), repo);
    }

    private static Route post(String path, Slot slot, String extras, Tier tier, String handler, String method) {
        return post(path, List.of(slot), extras, tier, handler, method);
    }

    /** Every route, in dispatch order per handler. */
    static final List<Route> ROUTES;

    static {
        List<Route> r = new ArrayList<>();
        // ---- /v1/catalog (CatalogHandler) ------------------------------------------------------------------------
        String c = "/v1/catalog";
        r.add(get(c + "/collections/get", name(), "", Tier.GENERIC, CATALOG, "handleCollectionGet"));
        r.add(get(c + "/collections/health", coll(), "", Tier.GENERIC, CATALOG, "handleCollectionHealth"));
        r.add(get(c + "/collections/owner-root", name(), "", Tier.GENERIC, CATALOG, "handleCollectionOwnerRoot"));
        r.add(post(c + "/collections/set_aspects_enabled", name(), "\"aspects_enabled\":true", Tier.GENERIC, CATALOG,
            "handleCollectionSetAspectsEnabled"));
        r.add(post(c + "/collections/supersede",
            List.of(name(), one("superseded_by", "superseded_by_id")), "", Tier.GENERIC, CATALOG,
            "handleCollectionSupersede"));
        // The fields are "source" and "target", so the convention's X_id twin is source_id / target_id (not source_collection_id).
        r.add(post(c + "/collections/rehome",
            List.of(one("source", "source_id"), one("target", "target_id")), "", Tier.HEAVY,
            CATALOG, "handleCollectionRehome"));
        r.add(post(c + "/collections/rehome/status",
            List.of(one("source", "source_id"), one("target", "target_id")), "", Tier.HEAVY,
            CATALOG, "handleCollectionRehomeStatus"));
        r.add(post(c + "/collections/rename", one("old_name", "collection_id"), "\"new_name\":\"{NEW}\"", Tier.RENAME,
            CATALOG, "handleCollectionRename"));
        r.add(post(c + "/collections/delete", name(), "", Tier.DELETE, CATALOG, "handleCollectionDelete"));
        r.add(get(c + "/list", coll(), "&limit=5", Tier.GENERIC, CATALOG, "handleList"));
        r.add(get(c + "/resolve", coll(), "&file_path=p35a-none.txt", Tier.GENERIC, CATALOG, "handleResolve"));
        r.add(get(c + "/manifest/chashes", coll(), "", Tier.GENERIC, CATALOG, "handleManifestChashes"));
        r.add(get(c + "/resolve_span", coll(), "&span_chash=" + HEX, Tier.GENERIC, CATALOG, "handleResolveSpan"));
        r.add(get(c + "/resolve_chash", one("prefer_collection", "prefer_collection_id"), "&chash=" + HEX, Tier.GENERIC,
            CATALOG, "handleResolveChash"));
        r.add(get(c + "/gc_audit/list", coll(), "", Tier.GENERIC, CATALOG, "handleGcAuditList"));
        // The audit row records the label (the column is the collection's name as the caller reported it); the repository reads
        // it from the request map (CatalogRepository.recordGcAudit), so the census names that method as the reader (SB3).
        r.add(withRepo(post(c + "/gc_audit/record", coll(), "\"operation\":\"p35a-op\"", Tier.GENERIC, CATALOG,
            "handleGcAuditRecord"), CATALOG_REPO + "#recordGcAudit"));
        r.add(post(c + "/index-run/begin", coll(), "\"doc_id\":\"1.1.1\"", Tier.HEAVY, CATALOG, "handleIndexRunBegin"));
        r.add(post(c + "/index-run/begin-many", coll(), "\"docs\":[]", Tier.HEAVY, CATALOG, "handleIndexRunBeginMany"));
        r.add(post(c + "/manifest/write", coll(), "\"doc_id\":\"1.1.1\",\"rows\":[]", Tier.HEAVY, CATALOG,
            "handleManifestWrite"));
        r.add(post(c + "/manifest/append", coll(), "\"doc_id\":\"1.1.1\",\"rows\":[]", Tier.HEAVY, CATALOG,
            "handleManifestAppend"));
        r.add(post(c + "/manifest/append_many", coll(), "\"docs\":[]", Tier.HEAVY, CATALOG, "handleManifestAppendMany"));
        r.add(post(c + "/manifest/write_many", coll(), "\"docs\":[]", Tier.HEAVY, CATALOG, "handleManifestWriteMany"));
        r.add(post(c + "/import/chunk", coll(), "\"doc_id\":\"1.1.1\",\"rows\":[]", Tier.HEAVY, CATALOG,
            "handleImportChunk"));

        // ---- /v1/vectors (VectorHandler, ReembedHandler) ----------------------------------------------------------
        String v = "/v1/vectors";
        r.add(post(v + "/upsert-chunks", coll(), "\"ids\":[\"" + HEX + "\"],\"documents\":[\"p35a\"]", Tier.HEAVY, VECTOR,
            "handleUpsertChunks"));
        r.add(post(v + "/search", many(), "\"query\":\"p35a\"", Tier.GENERIC, VECTOR, "handleSearch"));
        r.add(post(v + "/query", many(), "\"query\":\"p35a\"", Tier.GENERIC, VECTOR, "handleSearch"));
        r.add(post(v + "/search-per-collection", many(), "\"query\":\"p35a\",\"limit\":5,\"per_collection_k\":5",
            Tier.GENERIC, VECTOR, "handleSearchPerCollection"));
        r.add(post(v + "/hybrid-search", many(), "\"query\":\"p35a\"", Tier.GENERIC, VECTOR, "handleHybridSearch"));
        r.add(post(v + "/search-metadata-scoped", many(), "\"query\":\"p35a\"", Tier.GENERIC, VECTOR,
            "handleSearchMetadataScoped"));
        r.add(post(v + "/search-topic-scoped", coll(), "\"query\":\"p35a\",\"topic\":\"p35a-topic\"", Tier.HEAVY, VECTOR,
            "handleSearchTopicScoped"));
        r.add(post(v + "/search-graph-hop", many(), "\"query\":\"p35a\",\"seeds\":[\"1.1.1\"]", Tier.HEAVY, VECTOR,
            "handleSearchGraphHop"));
        r.add(post(v + "/search-aspect-scoped", many(), "\"query\":\"p35a\"", Tier.GENERIC, VECTOR,
            "handleSearchAspectScoped"));
        r.add(post(v + "/get", coll(), "", Tier.GENERIC, VECTOR, "handleGet"));
        r.add(post(v + "/get-all-metadata", coll(), "", Tier.GENERIC, VECTOR, "handleGetAllMetadata"));
        r.add(post(v + "/store-get", coll(), "", Tier.GENERIC, VECTOR, "handleStoreGet"));
        r.add(post(v + "/get-embeddings", coll(), "", Tier.GENERIC, VECTOR, "handleGetEmbeddings"));
        r.add(post(v + "/store-list", coll(), "", Tier.GENERIC, VECTOR, "handleStoreList"));
        r.add(post(v + "/store-delete", coll(), "\"ids\":[\"" + HEX + "\"]", Tier.GENERIC, VECTOR, "handleStoreDelete"));
        r.add(post(v + "/update-metadata", coll(), "\"ids\":[\"" + HEX + "\"],\"metadatas\":[{}]", Tier.GENERIC, VECTOR,
            "handleUpdateMetadata"));
        r.add(get(v + "/count", coll(), "", Tier.GENERIC, VECTOR, "handleCount"));
        r.add(onLocalEmbed(post(v + "/embed", coll(), "\"texts\":[\"p35a\"]", Tier.HEAVY, VECTOR, "handleEmbed")));
        r.add(post(v + "/gc/quarantine-orphans",
            List.of(coll(), one("quarantine_collection", "quarantine_collection_id")),
            "\"quarantined_at\":\"2026-01-01T00:00:00Z\"", Tier.HEAVY, VECTOR, "handleGcQuarantineOrphans"));
        r.add(post(v + "/gc/restore-rereferenced",
            List.of(one("origin_collection", "origin_collection_id"),
                one("quarantine_collection", "quarantine_collection_id")), "", Tier.HEAVY, VECTOR,
            "handleGcRestoreRereferenced"));
        r.add(post(v + "/gc/expire-quarantine",
            List.of(one("origin_collection", "origin_collection_id"),
                one("quarantine_collection", "quarantine_collection_id")),
            "\"cutoff\":\"2026-01-01T00:00:00Z\"", Tier.HEAVY, VECTOR, "handleGcExpireQuarantine"));
        r.add(post(v + "/gc/quarantine-restore", one("origin_collection", "origin_collection_id"),
            "\"chashes\":[\"" + HEX + "\"]", Tier.HEAVY, VECTOR, "handleGcQuarantineRestore"));
        r.add(post(v + "/manifest-less-census", coll(), "", Tier.GENERIC, VECTOR, "handleManifestLessCensus"));
        r.add(post(v + "/reapable", coll(), "", Tier.GENERIC, VECTOR, "handleReapable"));
        r.add(onLocalEmbed(post(v + "/reembed/plan",
            List.of(one("source_collection", "source_collection_id"), one("target_collection", "target_collection_id")),
            "", Tier.HEAVY, REEMBED, "plan")));
        r.add(onLocalEmbed(post(v + "/reembed/jobs",
            List.of(one("source_collection", "source_collection_id"), one("target_collection", "target_collection_id")),
            "", Tier.HEAVY, REEMBED, "start")));

        // ---- /v1/taxonomy (TaxonomyHandler) -----------------------------------------------------------------------
        String t = "/v1/taxonomy";
        r.add(get(t + "/topics", coll(), "", Tier.GENERIC, TAXONOMY, "handleGetTopics"));
        r.add(get(t + "/topics/unreviewed", coll(), "", Tier.GENERIC, TAXONOMY, "handleGetUnreviewed"));
        r.add(get(t + "/topics/resolve", coll(), "&label=p35a-topic", Tier.GENERIC, TAXONOMY, "handleResolveLabel"));
        r.add(post(t + "/topics/insert", coll(), "\"label\":\"p35a-topic\",\"doc_count\":1", Tier.GENERIC, TAXONOMY,
            "handleInsertTopic"));
        r.add(post(t + "/assignments/assign", one("source_collection", "source_collection_id"),
            "\"doc_id\":\"" + HEX + "\",\"topic_id\":1,\"assigned_by\":\"p35a\"", Tier.HEAVY, TAXONOMY, "handleAssign"));
        // The handlers refuse an empty chashes array (400) before they read the collection, and the repository returns an
        // empty answer for an empty list without resolving the collection, so the request carries one valid chash.
        // cross_collection:false keeps assign_from_chashes off the cross pass (which widens the coverage check).
        r.add(post(t + "/assignments/assign_from_chashes", coll(), "\"chashes\":[\"" + HEX + "\"],\"cross_collection\":false",
            Tier.GENERIC, TAXONOMY, "handleAssignFromChashes"));
        r.add(post(t + "/assignments/cross-preview", coll(), "\"chashes\":[\"" + HEX + "\"]", Tier.GENERIC, TAXONOMY,
            "handleCrossPreview"));
        r.add(post(t + "/assignments/unassigned", coll(), "", Tier.GENERIC, TAXONOMY, "handleUnassignedChashes"));
        r.add(post(t + "/purge_collection", coll(), "", Tier.GENERIC, TAXONOMY, "handlePurgeCollection"));
        r.add(post(t + "/meta/record", coll(), "\"doc_count\":1", Tier.GENERIC, TAXONOMY, "handleRecordDiscoverCount"));
        r.add(get(t + "/meta/last_count", coll(), "", Tier.GENERIC, TAXONOMY, "handleLastDiscoverCount"));
        r.add(post(t + "/meta/last_discover_batch", many(), "", Tier.GENERIC, TAXONOMY, "handleLastDiscoverBatch"));
        r.add(get(t + "/top_topics", coll(), "", Tier.GENERIC, TAXONOMY, "handleTopTopics"));
        r.add(get(t + "/chunk_grounded", one("source_collection", "source_collection_id"), "&doc_id=" + HEX,
            Tier.GENERIC, TAXONOMY, "handleChunkGrounded"));
        r.add(get(t + "/audit", coll(), "", Tier.GENERIC, TAXONOMY, "handleAuditCollection"));
        r.add(post(t + "/topics/persist_split", one("collection_name", "collection_id"), "\"topic_id\":1", Tier.HEAVY,
            TAXONOMY, "handlePersistSplit"));
        r.add(get(t + "/rebuild/old_state", coll(), "", Tier.GENERIC, TAXONOMY, "handleReadRebuildOldState"));
        r.add(post(t + "/topics/persist_rebuild", coll(), "", Tier.GENERIC, TAXONOMY, "handlePersistRebuild"));
        r.add(post(t + "/topics/persist_discovered", coll(), "\"specs\":[]", Tier.GENERIC, TAXONOMY,
            "handlePersistDiscovered"));
        // The one nested selector: the handler reads "collection" from each element of "records", so the request carries
        // the label or the id inside the record, and the extras (topic_id, a 384-wide embedding) are the record's other
        // fields (taxonomy_centroids.label is NOT NULL, hygiene-001). GENERIC: the label request must write (the test also counts the centroid, so a no-op cannot pass).
        r.add(withElement(post(t + "/centroids/upsert", nested("records", "collection", "collection_id"), "", Tier.GENERIC,
            TAXONOMY, "handleCentroidUpsert"),
            "\"topic_id\":1,\"label\":\"p35a-centroid\",\"doc_count\":1,\"embedding\":" + EMBEDDING_384));
        r.add(post(t + "/centroids/query", coll(), "\"embedding\":[0.1]", Tier.HEAVY, TAXONOMY, "handleCentroidQuery"));
        r.add(get(t + "/centroids/count", coll(), "", Tier.GENERIC, TAXONOMY, "handleCentroidCount"));
        r.add(get(t + "/centroids/by_collection", coll(), "", Tier.GENERIC, TAXONOMY, "handleCentroidByCollection"));
        r.add(get(t + "/centroids/foreign", coll(), "", Tier.GENERIC, TAXONOMY, "handleCentroidForeign"));
        r.add(post(t + "/centroids/delete", coll(), "", Tier.GENERIC, TAXONOMY, "handleCentroidDelete"));
        r.add(post(t + "/centroids/purge", coll(), "", Tier.GENERIC, TAXONOMY, "handleCentroidPurge"));

        // ---- /v1/aspects (AspectHandler) --------------------------------------------------------------------------
        String a = "/v1/aspects";
        r.add(get(a + "/get", coll(), "&source_path=p35a.md", Tier.GENERIC, ASPECT, "handleGet"));
        r.add(get(a + "/list_by_collection", coll(), "", Tier.GENERIC, ASPECT, "handleListByCollection"));
        r.add(post(a + "/delete", coll(), "\"source_path\":\"p35a.md\"", Tier.GENERIC, ASPECT, "handleDeleteAspect"));
        r.add(post(a + "/salient_sentences/set_by_key", coll(), "\"source_path\":\"p35a.md\"", Tier.GENERIC, ASPECT,
            "handleSetSalientByKey"));
        r.add(withRepo(post(a + "/upsert", coll(), "\"source_path\":\"p35a.md\"", Tier.HEAVY, ASPECT, "handleUpsert"),
            ASPECT_REPO + "#validateUpsert,upsertAspect"));
        r.add(withRepo(post(a + "/highlights/upsert", coll(), "\"doc_id\":\"p35a-doc\",\"ingested_at\":\"2026-01-01T00:00:00Z\","
            + "\"highlights_md\":\"p35a\"", Tier.HEAVY, ASPECT, "handleHighlightUpsert"), ASPECT_REPO + "#upsertHighlight"));

        // ---- /v1/chash (the two read routes; the five write routes answer 410 Gone), /v1/remap, /v1/pipeline,
        // ---- /v1/telemetry ------------------------------------------------------------------------------------------
        r.add(get("/v1/chash/count_for_collection", coll(), "", Tier.GENERIC, CHASH, "handleCountForCollection"));
        r.add(get("/v1/chash/registered_chashes", coll(), "", Tier.GENERIC, CHASH, "handleRegisteredChashes"));
        r.add(get("/v1/remap/entries", one("source_collection", "source_collection_id"), "", Tier.GENERIC, REMAP,
            "handleEntries"));
        r.add(get("/v1/remap/count", one("source_collection", "source_collection_id"), "", Tier.GENERIC, REMAP,
            "handleCount"));
        r.add(post("/v1/remap/clear_leg",
            List.of(one("source_collection", "source_collection_id"), one("target_collection", "target_collection_id")),
            "", Tier.GENERIC, REMAP, "handleClearLeg"));
        // record_batch refuses an empty entries array before it reads anything. Each entry carries its own target, which the
        // repository resolves through the registry (RemapRepository.recordBatch), so it is a second, nested slot beside the
        // top-level source; the entry's other fields are the element extras. HEAVY: the entry's chash and ledger rules.
        r.add(withElement(post("/v1/remap/record_batch",
            List.of(one("source_collection", "source_collection_id"),
                nested("entries", "target_collection", "target_collection_id")), "", Tier.HEAVY, REMAP, "handleRecordBatch"),
            "\"old_id\":\"p35a-old\",\"new_chash\":\"" + HEX + "\",\"provenance\":\"p35a\""));
        // content_hash is {NEW}: a pipeline is keyed by its content hash alone (not by collection), so the label and the id
        // request must not share one or the second is refused as already running whatever the route does with the id.
        r.add(post("/v1/pipeline/create", coll(), "\"content_hash\":\"p35a-{NEW}\",\"pdf_path\":\"/p35a.pdf\"", Tier.HEAVY,
            PIPELINE, "handleCreate"));
        r.add(post("/v1/pipeline/delete_collection", coll(), "", Tier.GENERIC, PIPELINE, "handleDeleteCollection"));
        // The ref routes address a pipeline by (content_hash, collection, pdf_path); the collection narrows the lookup, optionally
        // (PipelineHandler.refFromBody, refFromQuery and optionalRefFromQuery read it). It is a collection reference like create's, so
        // each ref route is a row: a client that created its pipeline by id names it by id. /pages and /chunks answer both a GET (query)
        // and a POST (body), one row each. HEAVY: no pipeline row exists for the content hash, so the label request is refused.
        String pl = "/v1/pipeline";
        String hash = "p35a-hash";
        r.add(get(pl + "/state", coll(), "&content_hash=" + hash, Tier.HEAVY, PIPELINE, "handleState"));
        r.add(post(pl + "/progress", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE, "handleProgress"));
        r.add(post(pl + "/extraction_meta", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE,
            "handleExtractionMeta"));
        r.add(post(pl + "/complete", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE, "handleComplete"));
        r.add(post(pl + "/fail", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE, "handleFail"));
        r.add(get(pl + "/pages", coll(), "&content_hash=" + hash, Tier.HEAVY, PIPELINE, "handlePages"));
        r.add(post(pl + "/pages", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE, "handlePages"));
        r.add(get(pl + "/chunks", coll(), "&content_hash=" + hash, Tier.HEAVY, PIPELINE, "handleChunks"));
        r.add(post(pl + "/chunks", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE, "handleChunks"));
        r.add(post(pl + "/mark_uploaded", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE,
            "handleMarkUploaded"));
        r.add(post(pl + "/reset_uploaded", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE,
            "handleResetUploaded"));
        r.add(get(pl + "/counts", coll(), "&content_hash=" + hash, Tier.HEAVY, PIPELINE, "handleCounts"));
        r.add(post(pl + "/clear_wal", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE, "handleClearWal"));
        r.add(post(pl + "/delete", coll(), "\"content_hash\":\"" + hash + "\"", Tier.HEAVY, PIPELINE, "handleDelete"));
        r.add(post("/v1/telemetry/relevance/log", coll(),
            "\"query\":\"p35a\",\"chunk_id\":\"" + HEX + "\",\"action\":\"click\"", Tier.GENERIC, TELEMETRY,
            "handleRelevanceLog"));
        r.add(get("/v1/telemetry/search/stats", coll(), "", Tier.GENERIC, TELEMETRY, "handleSearchStats"));
        r.add(post("/v1/telemetry/hook_failures/record", coll(), "\"hook_name\":\"p35a-hook\"", Tier.GENERIC, TELEMETRY,
            "handleHookFailureRecord"));
        ROUTES = List.copyOf(r);
    }

    // ---- the document routes (RDR-229 P3.5a, linda's ruling D1) -----------------------------------------------------------

    /**
     * Request tokens the document routes' {@code extras} and {@code elementExtras} use, expanded per call by
     * {@code DocumentRoutesByCollectionIdTest}: {@code {SEQ}} a number no other call has used, {@code {TITLE}} a title no other
     * call has used (the test finds the stored row by it), and {@code {DOC}} the tumbler of a document the test registered first,
     * in a collection of its own (the update routes move it).
     */
    static final String TOKEN_SEQ = "{SEQ}";
    static final String TOKEN_TITLE = "{TITLE}";
    static final String TOKEN_DOC = "{DOC}";

    private static Slot physical() {
        return one("physical_collection", "physical_collection_id");
    }

    private static Route document(String path, String extras, String handlerMethod, String repo) {
        return new Route("POST", "/v1/catalog" + path, Where.BODY, List.of(physical()), extras, Tier.GENERIC, CATALOG,
            handlerMethod, Engine.STANDARD, Resolver.WRITE, "", CATALOG_REPO + "#" + repo);
    }

    /** A document route whose request carries an array of documents, each naming its collection beside {@code elementExtras}. */
    private static Route documents(String path, String outer, String array, String elementExtras, String handlerMethod,
                                   String repo) {
        return new Route("POST", "/v1/catalog" + path, Where.BODY,
            List.of(new Slot("physical_collection", "physical_collection_id", false, array)), outer, Tier.GENERIC, CATALOG,
            handlerMethod, Engine.STANDARD, Resolver.WRITE, elementExtras, CATALOG_REPO + "#" + repo);
    }

    /**
     * The routes that write or move a document, each naming the document's collection by {@code physical_collection} or
     * {@code physical_collection_id}. Resolved by {@code DocumentCollectionRef} inside the write's transaction, not at the route
     * boundary ({@link Resolver#WRITE}), so they are not rows of the route table.
     */
    static final List<Route> DOCUMENT_ROUTES;

    static {
        String fields = "\"title\":\"" + TOKEN_TITLE + "\",\"content_type\":\"knowledge\",\"corpus\":\"knowledge\"";
        List<Route> d = new ArrayList<>();
        d.add(document("/register", "\"tumbler\":\"p35a." + TOKEN_SEQ + ".1\"," + fields, "handleRegister", "upsertDocument"));
        d.add(document("/doc/register", "\"owner_prefix\":\"p35a." + TOKEN_SEQ + "\"," + fields + ",\"file_path\":\""
            + TOKEN_TITLE + ".md\"", "handleDocRegister", "registerDocumentWithOutcome"));
        d.add(documents("/doc/register_many", "\"owner_prefix\":\"p35a." + TOKEN_SEQ + "\"", "docs",
            fields + ",\"file_path\":\"" + TOKEN_TITLE + ".md\"", "handleRegisterMany", "registerDocumentManyWithOutcome"));
        // doImportDocument is the single-row twin; no handler calls its public entry point, importDocument, but it has the same
        // normalization and reads the same field, so the census names it.
        d.add(document("/import/document", "\"tumbler\":\"p35a." + TOKEN_SEQ + ".1\"," + fields, "handleImportDocument",
            "importDocumentsBatch,doImportDocument"));
        d.add(document("/doc/resolve_or_register", "\"owner_name\":\"p35a-owner\",\"title\":\"" + TOKEN_TITLE
            + "\",\"source_uri\":\"https://p35a.example/" + TOKEN_TITLE + "\",\"content_type\":\"knowledge\"",
            "handleResolveOrRegister", "resolveOrRegister"));
        d.add(document("/update", "\"tumbler\":\"" + TOKEN_DOC + "\",\"title\":\"" + TOKEN_TITLE + "\"", "handleUpdate",
            "updateDocument,buildUpdateDocumentQuery,moveTarget"));
        d.add(documents("/update_many", "", "updates", "\"tumbler\":\"" + TOKEN_DOC + "\",\"title\":\"" + TOKEN_TITLE + "\"",
            "handleUpdateMany", "updateDocumentsMany,buildUpdateDocumentQuery,moveTarget"));
        DOCUMENT_ROUTES = List.copyOf(d);
    }

    /** Every route of the census: the route table's rows, then the document routes. */
    static List<Route> allRoutes() {
        List<Route> all = new ArrayList<>(ROUTES);
        all.addAll(DOCUMENT_ROUTES);
        return all;
    }

    /**
     * The methods that read a collection field on behalf of rows, so the rows that call them account for the sites. The census
     * test requires every named row to call the helper.
     */
    static final List<Helper> HELPERS = List.of(
        new Helper(CATALOG, "requireCollection", List.of("handleManifestWrite", "handleManifestAppend",
            "handleManifestAppendMany", "handleManifestWriteMany", "handleImportChunk")),
        new Helper(PIPELINE, "refFromBody", List.of("handleProgress", "handleExtractionMeta", "handleComplete", "handleFail",
            "handlePages", "handleChunks", "handleMarkUploaded", "handleResetUploaded", "handleClearWal", "handleDelete")),
        new Helper(PIPELINE, "optionalRefFromQuery", List.of("handleCounts")));

    /**
     * Collection fields read in a method that is neither a row's nor a helper's, with the reason the by-id contract does not cover
     * them. A reviewer who disagrees moves the method into {@link #ROUTES}; the census test needs every read site accounted for
     * by a row, a helper or this list, and each entry's evidence to still be in the source it names.
     */
    static final List<Excluded> EXCLUDED = List.of(
        new Excluded("CollectionSelectorFilter.java", "collectDocumentIds", "physical_collection_id",
            "the consumption census's own read of a document write's body, with the census on only (tests): it names each id for "
                + "the request-level report and resolves nothing; the document routes resolve the id in the write's own "
                + "transaction (CollectionRegistry.requireIdForWrite)",
            "CollectionSelectorFilter.java", "collectDocumentIds(MAPPER.readTree(in), named)"),
        new Excluded("ChunkKeysAdminHandler.java", "handleSweep", "collection",
            "an operator sweep of the chunk-key migration. The body names the tenant swept (a root credential may name any), and the "
                + "route boundary resolves an id in the CALLER's tenant, so an id here would be resolved in the wrong tenant: a "
                + "collection id is one tenant's registry numbering. The field only narrows which chunk rows the sweep reads, by "
                + "label; it selects no registry row",
            "ChunkKeysAdminHandler.java", "namedTenant(body)"),
        new Excluded("ChunkKeysAdminHandler.java", "resume", "collection",
            "the sweep's own resume token: a {collection, chash} cursor the previous call returned, read back verbatim, not a "
                + "request's collection",
            "ChunkKeysAdminHandler.java", "resume.cursor must be {collection, chash}"),
        new Excluded("ChunkKeysAdminHandler.java", "resume", "collection_id",
            "the same resume token's collection id (RDR-229 P3.5c): from gate 3 the token carries the cursor's collection_id because "
                + "the signature covers it; it is read back verbatim and checked against the signed token, not resolved as a request's "
                + "collection",
            "ChunkKeysAdminHandler.java", "resume.cursor.collection_id must be a non-negative integer"),
        new Excluded("TaxonomyHandler.java", "handlePruneProjection", "source_collection_prefix",
            "a name prefix matched against the projection's source collections: it names no single registry row, so no id can "
                + "stand for it",
            "TaxonomyHandler.java", "source_collection_prefix"),
        new Excluded("TaxonomyHandler.java", "handleImportTopic", "collection", etl("taxonomy topic"),
            "TaxonomyHandler.java", "handleImportTopic"),
        new Excluded("TaxonomyHandler.java", "handleImportAssignment", "source_collection", etl("taxonomy assignment"),
            "TaxonomyHandler.java", "handleImportAssignment"),
        new Excluded("TaxonomyHandler.java", "handleImportMeta", "collection", etl("taxonomy meta"),
            "TaxonomyHandler.java", "handleImportMeta"),
        new Excluded("TelemetryHandler.java", "handleImport", "collection", etl("telemetry"),
            "TelemetryRepository.java", "CollectionIds.idOf(tenant, str(collection))"),
        new Excluded("AspectRepository.java", "importAspectsBatch", "collection", etl("aspect"),
            "AspectRepository.java", "importAspectsBatch"),
        new Excluded("AspectRepository.java", "doImportAspect", "collection", etl("aspect"),
            "AspectRepository.java", "doImportAspect"),
        new Excluded("AspectRepository.java", "importHighlightsBatch", "collection", etl("highlight"),
            "AspectRepository.java", "importHighlightsBatch"),
        new Excluded("AspectRepository.java", "doImportHighlight", "collection", etl("highlight"),
            "AspectRepository.java", "doImportHighlight"),
        new Excluded("GraphScopeFilter.java", "parse", "collections",
            "the traversal's scope filter ({collections: [...]} or {subtree}): a set of labels, which stay labels on the wire. What they "
                + "match is a document's stored name before gate 3 (GraphScopeFilter.admits, a binary search of its "
                + "physical_collection), and from gate 3 the registry id: graphBFS resolves each label with GraphScopeFilter.idsOf "
                + "(CollectionRegistry, ordered by labelResolutionOrder) and matches physical_collection_id, so a document keeps "
                + "matching its collection's NEW label after a one-row rename (TraverseScopeAfterOneRowRenameTest). A label no row "
                + "has names no collection. It selects no registry row by id, so no id spelling is taken",
            "CatalogRepository.java", "scope.idsOf(ctx, tenant)"),
        new Excluded("TopicRoute.java", "parse", "collections",
            "the topic route's own subset of the REQUEST's collections: each name must be in the request's list and is compared, as a "
                + "label, with the request's own collection strings (PgVectorRepository: topicRoute.routes(col), col an element of the "
                + "request's list). It reads no stored name column, and the request's collections field is a row, resolved from "
                + "collection_ids before the route is parsed, so after a rename the list and the route name the same current label; "
                + "it names no registry row of its own",
            "PgVectorRepository.java", "topicRoute.routes(col)"),
        new Excluded("TargetSelection.java", "fromMap", "collection_id",
            "reads the list route's OUTPUT row (CatalogRepository.listCollections' map), not a request",
            "TargetSelection.java", "public static CollectionRow fromMap"));

    private static String etl(String what) {
        return "an ETL import of " + what + " rows exported from another install (the SQLite and cross-install migration paths). "
            + "An export carries labels, since a collection id is one install's registry numbering and means nothing in another; "
            + "the route stores what the export says and takes no id";
    }

    /**
     * Files that resolve a collection reference through the registry ({@code CollectionRegistry}, {@code CollectionIds},
     * {@code CatalogIdStamps}, {@code DocumentCollectionRef}), and how many call sites each has, so a repository that starts to
     * resolve a collection from a request it did not before fails the census test whatever it names the field. The text says which
     * routes the sites serve; a count that moves means a site was added or removed: say which route it serves, then change the count.
     */
    record Resolving(int sites, String serves) { }

    static final java.util.Map<String, Resolving> RESOLVING = java.util.Map.ofEntries(
        java.util.Map.entry("db/AspectRepository.java", new Resolving(12, "the /v1/aspects rows and the aspect ETL; the aspect upsert's write stamps the registry row's current label (requireWritable); the batch imports read their collections in id order (CollectionRegistry.lockAllInIdOrder, not counted here)")),
        java.util.Map.entry("db/CatalogIdStamps.java", new Resolving(1, "the catalog writers' memoized label lookup")),
        java.util.Map.entry("db/CatalogMoves.java", new Resolving(3, "/v1/catalog/documents/move, the owners move; the registry rows of the documents collections are read before the document rows are locked (lockCollectionsOfDocuments)")),
        java.util.Map.entry("db/CatalogRepository.java", new Resolving(29, "the document routes (DocumentCollectionRef), the collection routes (set_aspects_enabled reads its row in its write) and the catalog writers, and registryFirst, the manifest writers' registry check after their advisory locks; the re-home reads both ends FOR KEY SHARE (requireAllForWrite, then each locked row) and scopes by those rows")),
        java.util.Map.entry("db/ChashRepository.java", new Resolving(3, "/v1/chash rename_collection and the chash read routes")),
        java.util.Map.entry("db/CollectionIds.java", new Resolving(1, "the write transaction's registry check: CollectionIds.require reads the row FOR KEY SHARE through CollectionRegistry.requireForWrite")),
        java.util.Map.entry("db/CollectionIdReads.java", new Resolving(1, "the id-keyed reads' label-to-id subselect: the id the request named, else the live row's")),
        java.util.Map.entry("db/CollectionKey.java", new Resolving(6, "the taxonomy, aspect, pipeline and remap rows' id-keyed predicates")),
        java.util.Map.entry("db/CollectionRehomeScope.java", new Resolving(1, "the /v1/catalog/collections/rehome routes: the target label's registry id")),
        java.util.Map.entry("db/CollectionTombstones.java", new Resolving(3, "the collection delete and supersede routes; repointTombstones reads the successor row by id (P3.7's one-row rename, no route yet)")),
        java.util.Map.entry("db/CombinedWriteService.java", new Resolving(6, "the manifest write, append and write_many routes; the metadata refresh transaction's registry check (requireWritable) is the sixth")),
        java.util.Map.entry("db/DepartedWriterSpans.java", new Resolving(1, "the manifest write routes' departed-writer fill")),
        java.util.Map.entry("db/DocumentCollectionRef.java", new Resolving(6, "the document routes: the one resolver of physical_collection_id; a document request's id is read FOR KEY SHARE in the write's transaction (requireIdForWrite); a request by label takes the key share first too (requireForWriteIfPresent); a batch reads its labels in one statement in id order")),
        java.util.Map.entry("db/GraphScopeFilter.java", new Resolving(1, "the graph traversal's scope: each collection label of a graph request resolved to its registry id (CollectionRegistry.shared().resolveLabel; a read, no lock)")),
        java.util.Map.entry("db/GcCollectionReads.java", new Resolving(3, "the four /v1/vectors/gc writes: the origin and a by-id sibling, read in the write's transaction; the origin again when the restore function refused it")),
        java.util.Map.entry("db/ManifestReplace.java", new Resolving(3, "the manifest write routes; an empty manifest reads the registry row too (requireForWriteIfPresent)")),
        java.util.Map.entry("db/PipelineRepository.java", new Resolving(2, "the /v1/pipeline create and delete_collection rows")),
        java.util.Map.entry("db/QuarantineOrigin.java", new Resolving(6, "the /v1/vectors/gc quarantine routes")),
        java.util.Map.entry("db/ReembedJobRepository.java", new Resolving(6, "the /v1/vectors/reembed rows; the job start reads both ends in id order")),
        java.util.Map.entry("db/RemapRepository.java", new Resolving(4, "the /v1/remap rows; record_batch and clear_leg read every collection of the call FOR KEY SHARE at once, in id order (requireAllForWrite)")),
        java.util.Map.entry("db/RequestedCollectionRead.java", new Resolving(1, "the by-id read of the aspect delete, salient-sentences write, pipeline delete and remap leg clear")),
        java.util.Map.entry("db/RestoreIdFill.java", new Resolving(1, "the gc quarantine restore route")),
        java.util.Map.entry("db/TaxonomyCoverage.java", new Resolving(1, "the taxonomy coverage read of the vector search routes")),
        java.util.Map.entry("db/TaxonomyRepository.java", new Resolving(29, "the /v1/taxonomy rows and the taxonomy ETL; assign_from_chashes reads the row in each of its two transactions (the own pass and the cross pass), and files under its current label; assign_many reads its source collections at once (requireAllForWrite); mergeTopics reads the topic's collection FOR KEY SHARE first; the assign writers stamp the source collection's current label (labelNow); the readers readRebuildOldState and getLastDiscoverDocCount read the registry row (unlocked) to match the collection by id")),
        java.util.Map.entry("db/TaxonomyCollectionLock.java", new Resolving(1, "the taxonomy persist routes (split, rebuild, discovered): the advisory lock keyed by id, then the registry check")),
        java.util.Map.entry("db/TelemetryRepository.java", new Resolving(5, "the /v1/telemetry rows, the rename route and the telemetry ETL")),
        java.util.Map.entry("db/TupleRepository.java", new Resolving(3, "the tuple space's collection-keyed tuples; no collection route")),
        java.util.Map.entry("http/CollectionSelectorFilter.java", new Resolving(1, "the route boundary: the one resolver of collection_id")),
        java.util.Map.entry("vectors/CollectionSelector.java", new Resolving(2, "the vector routes' id-keyed predicates: the id the request named, else the live row's")),
        java.util.Map.entry("vectors/ChunkKeySweep.java", new Resolving(1, "the chunk-key sweep (excluded: ChunkKeysAdminHandler#handleSweep)")),
        java.util.Map.entry("vectors/EmbedderRouter.java", new Resolving(1, "the embed route and the vector writes' model dispatch")),
        java.util.Map.entry("vectors/PgVectorRepository.java", new Resolving(19, "the /v1/vectors rows; resolveNeedEmbedIdx's metadata transaction reads the registry row (requireWritable) after the sweep gate")),
        java.util.Map.entry("vectors/TaxonomyCentroidRepository.java", new Resolving(2, "the /v1/taxonomy centroid rows; the centroid upsert stamps the row's own id and current label")));

    /**
     * A per-store {@code rename_collection} route: it rewrites the collection name in one store's tables ({@code {old, new}}
     * body, labels only; the by-id contract does not extend to it). linda's ruling 3 (2026-11-22): the rename refusal lives in
     * the shared rename primitive, so every entry point that rewrites a collection name gets it. All five are on the public
     * HTTP server (each handler is registered by {@code NexusService} behind the auth filter) and the Python T2 facade calls
     * each one over HTTP ({@code nexus.db.t2.rename_collection_cascade}), so none is internal-only: each gets the refusal, and
     * {@link CollectionRenameRefusalTest} runs it. {@link CollectionRouteCensusTest} pins that these are every
     * {@code rename_collection} route the handlers dispatch.
     */
    record StoreRename(String path, String handler, String readsId) { }

    /** The five per-store {@code rename_collection} routes; the catalog rename is the {@link Tier#RENAME} row of {@link #ROUTES}. */
    static final List<StoreRename> STORE_RENAMES = List.of(
        new StoreRename("/v1/chash/rename_collection", CHASH, "handleRenameCollection"),
        new StoreRename("/v1/taxonomy/rename_collection", TAXONOMY, "handleRenameCollection"),
        new StoreRename("/v1/telemetry/rename_collection", TELEMETRY, "handleRenameCollection"),
        new StoreRename("/v1/aspects/rename_collection", ASPECT, "handleRenameCollection"),
        new StoreRename("/v1/aspects/highlights/rename_collection", ASPECT, "handleHighlightRenameCollection"));

    /**
     * Request fields that carry a collection id by the convention (the first bullet of the class javadoc) in a request, plus the
     * response-only and reason names the engine writes with an id in them. The spelling scan allows exactly these (and every
     * slot's {@link Slot#idField()}); a literal with a collection-id shape outside the set is a second spelling.
     */
    static final java.util.Set<String> ID_NAMES_OTHER_THAN_SLOTS = java.util.Set.of(
        "collection_id", "collection_ids",
        // response and reason names, never read from a request. Each is a name the engine WRITES; none is a request parameter.
        "unknown_collection_id",
        // CollectionSelectorFilter: the 500 collection_id_mismatch body's field naming the id the registry answered with
        "resolved_collection_id",
        // CollectionSupersededBody: the 409 collection_superseded body's field naming the live successor's id
        "successor_collection_id",
        // HttpUtil.COLLECTION_ID_MISMATCH_REASON: the "reason" string of that 500 body (and its log event), a value, not a field
        "collection_id_mismatch");

    /** The id a probe uses for a collection that does not exist: far above any identity value a test run reaches. */
    static final long UNKNOWN_ID = 987_654_321_012L;

    /** The path prefix each handler file serves, for the census test's dispatch check. */
    static final Map<String, String> PREFIX = Map.ofEntries(
        Map.entry("CatalogHandler.java", "/v1/catalog"),
        Map.entry("VectorHandler.java", "/v1/vectors"),
        Map.entry("ReembedHandler.java", "/v1/vectors/reembed"),
        Map.entry("TaxonomyHandler.java", "/v1/taxonomy"),
        Map.entry("AspectHandler.java", "/v1/aspects"),
        Map.entry("RemapHandler.java", "/v1/remap"),
        Map.entry("PipelineHandler.java", "/v1/pipeline"),
        Map.entry("ChashHandler.java", "/v1/chash"),
        Map.entry("TelemetryHandler.java", "/v1/telemetry"));
}
