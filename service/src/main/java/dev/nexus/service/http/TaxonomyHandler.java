package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TaxonomyRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * RDR-152 bead nexus-gmiaf.14 — Taxonomy HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/taxonomy/}):
 * <pre>
 *   GET   /v1/taxonomy/topics              all topics (collection= optional)
 *   GET   /v1/taxonomy/topics/root         root topics (parent_id IS NULL)
 *   GET   /v1/taxonomy/topics/children     child topics (parent_id= required)
 *   GET   /v1/taxonomy/topics/unreviewed   pending topics (collection=, limit= optional)
 *   GET   /v1/taxonomy/topics/by_id        single topic (id= required)
 *   GET   /v1/taxonomy/topics/resolve      resolve label→id (label=, collection= optional)
 *   GET   /v1/taxonomy/topics/collections  distinct collection names
 *   POST  /v1/taxonomy/topics/insert       insert new topic
 *   POST  /v1/taxonomy/topics/update_label update label
 *   POST  /v1/taxonomy/topics/rename       rename + mark accepted
 *   POST  /v1/taxonomy/topics/mark_reviewed update review_status
 *   POST  /v1/taxonomy/topics/update_doc_count update doc_count
 *   GET   /v1/taxonomy/topics/count_assignments count assignments for topic_id=
 *   POST  /v1/taxonomy/topics/delete       delete topic (returns collection)
 *   POST  /v1/taxonomy/topics/merge        merge source→target
 *   POST  /v1/taxonomy/assignments/assign  upsert assignment
 *   GET   /v1/taxonomy/assignments/docs    doc_ids for topic_id=
 *   POST  /v1/taxonomy/assignments/for_docs assignments for doc_ids list
 *   GET   /v1/taxonomy/assignments/by_label doc_ids for label=
 *   POST  /v1/taxonomy/assignments/purge_doc purge assignments for doc
 *   POST  /v1/taxonomy/purge_collection    purge all rows for collection=
 *   POST  /v1/taxonomy/rename_collection   rename collection
 *   POST  /v1/taxonomy/meta/record         record discover count
 *   GET   /v1/taxonomy/meta/last_count     last discover doc_count for collection=
 *   POST  /v1/taxonomy/links/upsert        upsert topic link
 *   POST  /v1/taxonomy/links/pairs         get link pairs for topic_id list
 *   GET   /v1/taxonomy/icf/source_count    count distinct source collections
 *   GET   /v1/taxonomy/icf/rows            ICF rows for n_effective=
 *   GET   /v1/taxonomy/top_topics          top topics for collection= &amp; top_n=
 *   GET   /v1/taxonomy/chunk_grounded      max similarity for doc_id= &amp; source_collection=
 *   GET   /v1/taxonomy/projection_counts   projection counts by collection
 *   POST  /v1/taxonomy/import/topic        fidelity ETL: topics row
 *   POST  /v1/taxonomy/import/assignment   fidelity ETL: assignments row
 *   POST  /v1/taxonomy/import/link         fidelity ETL: topic_links row
 *   POST  /v1/taxonomy/import/meta         fidelity ETL: taxonomy_meta row
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant} header.
 *
 * <p>FTS contract (Store 4): NO FTS endpoints — topics are found by exact label/collection.
 */
public final class TaxonomyHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(TaxonomyHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE   = new TypeReference<>() {};
    private static final TypeReference<List<Object>>        LIST_TYPE  = new TypeReference<>() {};

    private final TaxonomyRepository repo;

    public TaxonomyHandler(TaxonomyRepository repo) {
        this.repo = repo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = (String) exchange.getAttribute(AuthFilter.ATTR_TENANT);
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        String path   = exchange.getRequestURI().getPath();
        String op     = path.replaceFirst("^/v1/taxonomy", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                // Topics
                case "/topics"                    -> handleGetTopics(exchange, tenant, method);
                case "/topics/root"               -> handleGetRootTopics(exchange, tenant, method);
                case "/topics/children"           -> handleGetChildTopics(exchange, tenant, method);
                case "/topics/unreviewed"         -> handleGetUnreviewed(exchange, tenant, method);
                case "/topics/by_id"              -> handleGetById(exchange, tenant, method);
                case "/topics/resolve"            -> handleResolveLabel(exchange, tenant, method);
                case "/topics/collections"        -> handleGetCollections(exchange, tenant, method);
                case "/topics/insert"             -> handleInsertTopic(exchange, tenant, method);
                case "/topics/update_label"       -> handleUpdateLabel(exchange, tenant, method);
                case "/topics/rename"             -> handleRenameTopic(exchange, tenant, method);
                case "/topics/mark_reviewed"      -> handleMarkReviewed(exchange, tenant, method);
                case "/topics/update_doc_count"   -> handleUpdateDocCount(exchange, tenant, method);
                case "/topics/count_assignments"  -> handleCountAssignments(exchange, tenant, method);
                case "/topics/delete"             -> handleDeleteTopic(exchange, tenant, method);
                case "/topics/merge"              -> handleMergeTopics(exchange, tenant, method);
                // Assignments
                case "/assignments/assign"        -> handleAssign(exchange, tenant, method);
                case "/assignments/docs"          -> handleGetDocIds(exchange, tenant, method);
                case "/assignments/for_docs"      -> handleGetAssignmentsForDocs(exchange, tenant, method);
                case "/assignments/by_label"      -> handleGetDocsByLabel(exchange, tenant, method);
                case "/assignments/purge_doc"     -> handlePurgeDoc(exchange, tenant, method);
                // Collection ops
                case "/purge_collection"          -> handlePurgeCollection(exchange, tenant, method);
                case "/rename_collection"         -> handleRenameCollection(exchange, tenant, method);
                // Meta
                case "/meta/record"               -> handleRecordDiscoverCount(exchange, tenant, method);
                case "/meta/last_count"           -> handleLastDiscoverCount(exchange, tenant, method);
                // Links
                case "/links/upsert"              -> handleUpsertLink(exchange, tenant, method);
                case "/links/pairs"               -> handleGetLinkPairs(exchange, tenant, method);
                // ICF
                case "/icf/source_count"          -> handleSourceCount(exchange, tenant, method);
                case "/icf/rows"                  -> handleIcfRows(exchange, tenant, method);
                // Analytics
                case "/top_topics"                -> handleTopTopics(exchange, tenant, method);
                case "/chunk_grounded"            -> handleChunkGrounded(exchange, tenant, method);
                case "/projection_counts"         -> handleProjectionCounts(exchange, tenant, method);
                // ETL import
                case "/import/topic"              -> handleImportTopic(exchange, tenant, method);
                case "/import/assignment"         -> handleImportAssignment(exchange, tenant, method);
                case "/import/link"               -> handleImportLink(exchange, tenant, method);
                case "/import/meta"               -> handleImportMeta(exchange, tenant, method);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            log.debug("event=taxonomy_bad_request tenant={} op={} error={}", tenant, op, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=taxonomy_handler_error tenant={} op={}", tenant, op, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    // ── Topics handlers ────────────────────────────────────────────────────────

    private void handleGetTopics(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String collection = queryParam(ex, "collection");
        HttpUtil.send(ex, 200, json(repo.getAllTopics(tenant, collection)));
    }

    private void handleGetRootTopics(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        HttpUtil.send(ex, 200, json(repo.getRootTopics(tenant)));
    }

    private void handleGetChildTopics(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        long parentId = requireLongParam(ex, "parent_id");
        HttpUtil.send(ex, 200, json(repo.getChildTopics(tenant, parentId)));
    }

    private void handleGetUnreviewed(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String collection = queryParam(ex, "collection");
        int limit = optIntParam(ex, "limit", 100);
        HttpUtil.send(ex, 200, json(repo.getUnreviewedTopics(tenant, collection, limit)));
    }

    private void handleGetById(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        long id = requireLongParam(ex, "id");
        Optional<Map<String, Object>> row = repo.getTopicById(tenant, id);
        if (row.isEmpty()) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
        } else {
            HttpUtil.send(ex, 200, json(row.get()));
        }
    }

    private void handleResolveLabel(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String label      = requireQueryParam(ex, "label");
        String collection = queryParam(ex, "collection");
        Optional<Long> id = repo.resolveLabel(tenant, label, collection);
        if (id.isEmpty()) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
        } else {
            HttpUtil.send(ex, 200, json(Map.of("id", id.get())));
        }
    }

    private void handleGetCollections(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        HttpUtil.send(ex, 200, json(repo.getDistinctCollections(tenant)));
    }

    private void handleInsertTopic(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String label      = requireString(body, "label");
        Long   parentId   = optLong(body, "parent_id");
        String collection = requireString(body, "collection");
        int    docCount   = optIntDefault(body, "doc_count", 0);
        String createdAt  = optStringOrNull(body, "created_at");
        String terms      = optStringOrNull(body, "terms");
        long id = repo.insertTopic(tenant, label, parentId, collection, docCount, createdAt, terms);
        HttpUtil.send(ex, 200, json(Map.of("id", id)));
    }

    private void handleUpdateLabel(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long   topicId  = requireLong(body, "topic_id");
        String newLabel = requireString(body, "label");
        repo.updateTopicLabel(tenant, topicId, newLabel);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleRenameTopic(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long   topicId  = requireLong(body, "topic_id");
        String newLabel = requireString(body, "label");
        repo.renameTopic(tenant, topicId, newLabel);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleMarkReviewed(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long   topicId = requireLong(body, "topic_id");
        String status  = requireString(body, "status");
        repo.markTopicReviewed(tenant, topicId, status);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleUpdateDocCount(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long topicId  = requireLong(body, "topic_id");
        int  docCount = requireInt(body, "doc_count");
        repo.updateDocCount(tenant, topicId, docCount);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleCountAssignments(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        long topicId = requireLongParam(ex, "topic_id");
        int count = repo.countAssignments(tenant, topicId);
        HttpUtil.send(ex, 200, json(Map.of("count", count)));
    }

    private void handleDeleteTopic(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long topicId = requireLong(body, "topic_id");
        Optional<String> col = repo.deleteTopic(tenant, topicId);
        if (col.isEmpty()) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
        } else {
            HttpUtil.send(ex, 200, json(Map.of("collection", col.get())));
        }
    }

    private void handleMergeTopics(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long sourceId = requireLong(body, "source_id");
        long targetId = requireLong(body, "target_id");
        Optional<String> col = repo.mergeTopics(tenant, sourceId, targetId);
        if (col.isEmpty()) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found or same topic\"}");
        } else {
            HttpUtil.send(ex, 200, json(Map.of("collection", col.get())));
        }
    }

    // ── Assignments ────────────────────────────────────────────────────────────

    private void handleAssign(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String docId           = requireString(body, "doc_id");
        long   topicId         = requireLong(body, "topic_id");
        String assignedBy      = requireString(body, "assigned_by");
        Double similarity      = optDoubleOrNull(body, "similarity");
        String sourceCollection = optStringOrNull(body, "source_collection");
        String assignedAt      = optStringOrNull(body, "assigned_at");
        repo.assignTopic(tenant, docId, topicId, assignedBy, similarity, sourceCollection, assignedAt);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleGetDocIds(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        long topicId = requireLongParam(ex, "topic_id");
        int  limit   = optIntParam(ex, "limit", 0);
        HttpUtil.send(ex, 200, json(repo.getTopicDocIds(tenant, topicId, limit)));
    }

    @SuppressWarnings("unchecked")
    private void handleGetAssignmentsForDocs(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        Object raw = body.get("doc_ids");
        List<String> docIds = raw instanceof List<?> lst
            ? lst.stream().map(Object::toString).toList()
            : List.of();
        HttpUtil.send(ex, 200, json(repo.getAssignmentsForDocs(tenant, docIds)));
    }

    private void handleGetDocsByLabel(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String label = requireQueryParam(ex, "label");
        HttpUtil.send(ex, 200, json(repo.getDocIdsForLabel(tenant, label)));
    }

    private void handlePurgeDoc(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String project = requireString(body, "project");
        String title   = requireString(body, "title");
        int removed = repo.purgeAssignmentsForDoc(tenant, project, title);
        HttpUtil.send(ex, 200, json(Map.of("removed", removed)));
    }

    // ── Collection ops ─────────────────────────────────────────────────────────

    private void handlePurgeCollection(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String collection = requireString(body, "collection");
        HttpUtil.send(ex, 200, json(repo.purgeCollection(tenant, collection)));
    }

    private void handleRenameCollection(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String oldCol = requireString(body, "old_collection");
        String newCol = requireString(body, "new_collection");
        HttpUtil.send(ex, 200, json(repo.renameCollection(tenant, oldCol, newCol)));
    }

    // ── Meta ───────────────────────────────────────────────────────────────────

    private void handleRecordDiscoverCount(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String collection   = requireString(body, "collection");
        int    docCount     = requireInt(body, "doc_count");
        String discoveredAt = optStringOrNull(body, "discovered_at");
        repo.recordDiscoverCount(tenant, collection, docCount, discoveredAt);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleLastDiscoverCount(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String collection = requireQueryParam(ex, "collection");
        Optional<Integer> count = repo.getLastDiscoverDocCount(tenant, collection);
        if (count.isEmpty()) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
        } else {
            HttpUtil.send(ex, 200, json(Map.of("count", count.get())));
        }
    }

    // ── Links ──────────────────────────────────────────────────────────────────

    private void handleUpsertLink(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long   fromId    = requireLong(body, "from_topic_id");
        long   toId      = requireLong(body, "to_topic_id");
        int    linkCount = optIntDefault(body, "link_count", 1);
        String linkTypes = optStringOrNull(body, "link_types");
        repo.upsertTopicLink(tenant, fromId, toId, linkCount, linkTypes);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    @SuppressWarnings("unchecked")
    private void handleGetLinkPairs(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        Object raw = body.get("topic_ids");
        List<Long> ids = raw instanceof List<?> lst
            ? lst.stream().map(v -> ((Number) v).longValue()).toList()
            : List.of();
        HttpUtil.send(ex, 200, json(repo.getTopicLinkPairs(tenant, ids)));
    }

    // ── ICF ────────────────────────────────────────────────────────────────────

    private void handleSourceCount(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        int count = repo.countDistinctSourceCollections(tenant);
        HttpUtil.send(ex, 200, json(Map.of("count", count)));
    }

    private void handleIcfRows(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        int nEffective = optIntParam(ex, "n_effective", 1);
        HttpUtil.send(ex, 200, json(repo.computeIcfRows(tenant, nEffective)));
    }

    // ── Analytics ──────────────────────────────────────────────────────────────

    private void handleTopTopics(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String collection = requireQueryParam(ex, "collection");
        int    topN       = optIntParam(ex, "top_n", 10);
        HttpUtil.send(ex, 200, json(repo.topTopicsForCollection(tenant, collection, topN)));
    }

    private void handleChunkGrounded(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String docId            = requireQueryParam(ex, "doc_id");
        String sourceCollection = requireQueryParam(ex, "source_collection");
        Optional<Double> sim = repo.chunkGroundedIn(tenant, docId, sourceCollection);
        if (sim.isEmpty()) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
        } else {
            HttpUtil.send(ex, 200, json(Map.of("similarity", sim.get())));
        }
    }

    private void handleProjectionCounts(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        HttpUtil.send(ex, 200, json(repo.getProjectionCountsByCollection(tenant)));
    }

    // ── ETL import ─────────────────────────────────────────────────────────────

    private void handleImportTopic(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long   srcId        = requireLong(body, "id");
        String label        = requireString(body, "label");
        Long   parentId     = optLong(body, "parent_id");
        String collection   = requireString(body, "collection");
        String centroidHash = optStringOrNull(body, "centroid_hash");
        int    docCount     = optIntDefault(body, "doc_count", 0);
        String createdAt    = requireString(body, "created_at");
        String reviewStatus = requireString(body, "review_status");
        String terms        = optStringOrNull(body, "terms");
        long id = repo.importTopic(tenant, srcId, label, parentId, collection, centroidHash,
                                   docCount, createdAt, reviewStatus, terms);
        HttpUtil.send(ex, 200, json(Map.of("id", id)));
    }

    private void handleImportAssignment(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String docId            = requireString(body, "doc_id");
        long   topicId          = requireLong(body, "topic_id");
        String assignedBy       = requireString(body, "assigned_by");
        Double similarity       = optDoubleOrNull(body, "similarity");
        String assignedAt       = optStringOrNull(body, "assigned_at");
        String sourceCollection = optStringOrNull(body, "source_collection");
        repo.importAssignment(tenant, docId, topicId, assignedBy, similarity, assignedAt, sourceCollection);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleImportLink(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long   fromId    = requireLong(body, "from_topic_id");
        long   toId      = requireLong(body, "to_topic_id");
        int    linkCount = optIntDefault(body, "link_count", 1);
        String linkTypes = optStringOrNull(body, "link_types");
        repo.importTopicLink(tenant, fromId, toId, linkCount, linkTypes);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleImportMeta(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String collection            = requireString(body, "collection");
        int    lastDiscoverDocCount  = requireInt(body, "last_discover_doc_count");
        String lastDiscoverAt        = optStringOrNull(body, "last_discover_at");
        repo.importTaxonomyMeta(tenant, collection, lastDiscoverDocCount, lastDiscoverAt);
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private void requireMethod(HttpExchange ex, String actual, String expected) throws IOException {
        if (!actual.equals(expected)) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            throw new IllegalArgumentException("method not allowed: " + actual);
        }
    }

    private String json(Object obj) {
        try {
            return MAPPER.writeValueAsString(obj);
        } catch (Exception e) {
            log.error("event=json_serialize_error", e);
            return "{\"error\":\"serialization failed\"}";
        }
    }

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        try (InputStream is = ex.getRequestBody()) {
            byte[] bytes = is.readAllBytes();
            if (bytes.length == 0) return Map.of();
            return MAPPER.readValue(bytes, MAP_TYPE);
        }
    }

    private String queryParam(HttpExchange ex, String key) {
        String q = ex.getRequestURI().getQuery();
        if (q == null) return null;
        for (String p : q.split("&")) {
            String[] kv = p.split("=", 2);
            if (kv[0].equals(key)) return kv.length > 1 ? java.net.URLDecoder.decode(kv[1], java.nio.charset.StandardCharsets.UTF_8) : "";
        }
        return null;
    }

    private String requireQueryParam(HttpExchange ex, String key) throws IOException {
        String v = queryParam(ex, key);
        if (v == null || v.isBlank()) {
            HttpUtil.send(ex, 400, json(Map.of("error", "missing required query param: " + key)));
            throw new IllegalArgumentException("missing required query param: " + key);
        }
        return v;
    }

    private long requireLongParam(HttpExchange ex, String key) throws IOException {
        String v = requireQueryParam(ex, key);
        try { return Long.parseLong(v); }
        catch (NumberFormatException e) {
            HttpUtil.send(ex, 400, json(Map.of("error", "param '" + key + "' must be a long")));
            throw new IllegalArgumentException("param '" + key + "' must be a long");
        }
    }

    private int optIntParam(HttpExchange ex, String key, int def) {
        String v = queryParam(ex, key);
        if (v == null || v.isBlank()) return def;
        try { return Integer.parseInt(v); }
        catch (NumberFormatException e) { return def; }
    }

    private String requireString(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null || val.toString().isBlank())
            throw new IllegalArgumentException("missing required field: " + key);
        return val.toString();
    }

    private String optStringOrNull(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        String s = val.toString();
        return s.isBlank() ? null : s;
    }

    private long requireLong(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) throw new IllegalArgumentException("missing required field: " + key);
        if (val instanceof Number n) return n.longValue();
        try { return Long.parseLong(val.toString()); }
        catch (NumberFormatException e) { throw new IllegalArgumentException("field '" + key + "' must be a long"); }
    }

    private Long optLong(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (val instanceof Number n) return n.longValue();
        try { return Long.parseLong(val.toString()); }
        catch (NumberFormatException e) { return null; }
    }

    private int requireInt(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) throw new IllegalArgumentException("missing required field: " + key);
        if (val instanceof Number n) return n.intValue();
        try { return Integer.parseInt(val.toString()); }
        catch (NumberFormatException e) { throw new IllegalArgumentException("field '" + key + "' must be an int"); }
    }

    private int optIntDefault(Map<String, Object> body, String key, int def) {
        Object val = body.get(key);
        if (val == null) return def;
        if (val instanceof Number n) return n.intValue();
        try { return Integer.parseInt(val.toString()); }
        catch (NumberFormatException e) { return def; }
    }

    private Double optDoubleOrNull(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (val instanceof Number n) return n.doubleValue();
        try { return Double.parseDouble(val.toString()); }
        catch (NumberFormatException e) { return null; }
    }
}
