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
     */
    record Route(String method, String path, Where where, List<Slot> slots, String extras, Tier tier, String handler,
                 String readsId, Engine engine) {

        String label() {
            return method + " " + path;
        }
    }

    /** A collection field the handlers read that the by-id contract deliberately does not cover, and why. */
    record Excluded(String handler, String field, String why) { }

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
        return new Route("GET", path, Where.QUERY, List.of(slot), extras, tier, handler, method, Engine.STANDARD);
    }

    private static Route post(String path, List<Slot> slots, String extras, Tier tier, String handler, String method) {
        return new Route("POST", path, Where.BODY, slots, extras, tier, handler, method, Engine.STANDARD);
    }

    /** {@code route}, run against the {@link Engine#LOCAL_EMBED} engine. */
    private static Route onLocalEmbed(Route route) {
        return new Route(route.method(), route.path(), route.where(), route.slots(), route.extras(), route.tier(),
            route.handler(), route.readsId(), Engine.LOCAL_EMBED);
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
        r.add(post(t + "/centroids/upsert", nested("records", "collection", "collection_id"),
            "\"topic_id\":1,\"label\":\"p35a-centroid\",\"doc_count\":1,\"embedding\":" + EMBEDDING_384, Tier.GENERIC, TAXONOMY,
            "handleCentroidUpsert"));
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
        r.add(post(a + "/upsert", coll(), "\"source_path\":\"p35a.md\"", Tier.HEAVY, ASPECT, "handleUpsert"));
        r.add(post(a + "/highlights/upsert", coll(), "\"doc_id\":\"p35a-doc\",\"ingested_at\":\"2026-01-01T00:00:00Z\","
            + "\"highlights_md\":\"p35a\"", Tier.HEAVY, ASPECT, "handleHighlightUpsert"));

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
        // record_batch refuses an empty entries array before it reads anything; each entry carries its own target. HEAVY: the
        // repository may refuse an entry whose target is not a registered collection, which the label request may answer.
        r.add(post("/v1/remap/record_batch", one("source_collection", "source_collection_id"),
            "\"entries\":[{\"old_id\":\"p35a-old\",\"new_chash\":\"" + HEX + "\",\"target_collection\":\"{NEW}\","
                + "\"provenance\":\"p35a\"}]", Tier.HEAVY, REMAP, "handleRecordBatch"));
        // content_hash is {NEW}: a pipeline is keyed by its content hash alone (not by collection), so the label and the id
        // request must not share one or the second is refused as already running whatever the route does with the id.
        r.add(post("/v1/pipeline/create", coll(), "\"content_hash\":\"p35a-{NEW}\",\"pdf_path\":\"/p35a.pdf\"", Tier.HEAVY,
            PIPELINE, "handleCreate"));
        r.add(post("/v1/pipeline/delete_collection", coll(), "", Tier.GENERIC, PIPELINE, "handleDeleteCollection"));
        r.add(post("/v1/telemetry/relevance/log", coll(),
            "\"query\":\"p35a\",\"chunk_id\":\"" + HEX + "\",\"action\":\"click\"", Tier.GENERIC, TELEMETRY,
            "handleRelevanceLog"));
        r.add(get("/v1/telemetry/search/stats", coll(), "", Tier.GENERIC, TELEMETRY, "handleSearchStats"));
        r.add(post("/v1/telemetry/hook_failures/record", coll(), "\"hook_name\":\"p35a-hook\"", Tier.GENERIC, TELEMETRY,
            "handleHookFailureRecord"));
        ROUTES = List.copyOf(r);
    }

    /**
     * Collection fields the handlers read that the by-id contract does not test, with the reason. A reviewer who disagrees
     * moves a row from here into {@link #ROUTES}; the census test needs both lists to account for every field.
     */
    static final List<Excluded> EXCLUDED = List.of(
        new Excluded("CatalogHandler.java", "prefer_collection", "covered: /resolve_chash row (prefer_collection_id)"),
        new Excluded("AspectHandler.java", "collection", "covered by the /v1/aspects rows"),
        new Excluded("ChashHandler.java", "collection",
            "the two read routes are rows; the five write routes answer 410 Gone"),
        new Excluded("ChunkKeysAdminHandler.java", "collection",
            "operator sweep/constraint jobs (/v1/admin/chunk-keys): an optional filter on a migration job, not a registry "
                + "selector; ambiguous (report), not tested"),
        new Excluded("PipelineHandler.java", "collection",
            "create and delete_collection are rows; the ref routes read it as one optional part of a (content_hash, "
                + "collection, pdf_path) document key; ambiguous (report), not tested"),
        new Excluded("RemapHandler.java", "target_collection", "second slot of the clear_leg row; record_batch entries "
            + "carry it per entry"),
        new Excluded("TaxonomyHandler.java", "cross_collection", "a boolean mode flag, not a collection"),
        new Excluded("TaxonomyHandler.java", "source_collection_prefix", "a name prefix, not a collection"),
        new Excluded("TelemetryHandler.java", "collection", "relevance/log, search/stats, hook_failures/record are rows; "
            + "the /import and rename routes are ETL and rename-class"),
        new Excluded("ReembedHandler.java", "source_collection", "plan and start rows"),
        new Excluded("ReembedHandler.java", "target_collection", "plan and start rows"));

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
        // response and reason names, never read from a request
        "unknown_collection_id", "successor_collection_id");

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
