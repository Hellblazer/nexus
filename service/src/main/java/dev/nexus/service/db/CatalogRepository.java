// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_INDEX;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_1024;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_384;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_768;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.HOOK_FAILURES;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS_1024;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS_384;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS_768;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.SelectField;
import org.jooq.Table;
import org.jooq.UpdateSetMoreStep;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-152 bead nexus-gmiaf.18 — Catalog store repository.
 *
 * <p>Mirrors CatalogStore (SQLite) for the Postgres service tier.
 * Tables: catalog_owners, catalog_documents, catalog_links,
 * catalog_document_chunks, catalog_collections, catalog_meta.
 *
 * <p>FTS: catalog_documents.fts_vector is a GENERATED ALWAYS STORED tsvector:
 * title=english (stemmed) || author/corpus/file_path=simple (exact).
 * Search uses OR'd tsquery: plainto_tsquery('english',q) OR plainto_tsquery('simple',q)
 * so PG >= FTS5 (superset per the OPTION B intentional-upgrade decision).
 *
 * <p>All methods route through TenantScope.withTenant for RLS.
 */
public final class CatalogRepository {

    private static final Logger log = LoggerFactory.getLogger(CatalogRepository.class);

    /**
     * RDR-159 P-1a: the fixed set of schema-qualified relations the migration
     * count-verification may count. Mirrors {@code nexus.migration.orchestrator
     * ._VERIFY_TABLES} on the Python side. A relation not in this set is never
     * counted (whitelist guard against arbitrary relation names).
     */
    private static final Set<String> VERIFY_RELATIONS = Set.of(
        "nexus.memory",
        "nexus.plans",
        "nexus.topics",
        "nexus.topic_assignments",
        "nexus.topic_links",
        "nexus.hook_failures",
        "nexus.nx_answer_runs",
        "nexus.chash_index",
        "nexus.catalog_documents",
        "nexus.catalog_links"
    );

    static final ObjectMapper MAPPER = new ObjectMapper()
        .configure(com.fasterxml.jackson.databind.DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    // ── Table references ───────────────────────────────────────────────────────

    static final Table<?> T_OWNERS  = DSL.table(DSL.name("nexus", "catalog_owners"));
    static final Table<?> T_DOCS    = DSL.table(DSL.name("nexus", "catalog_documents"));
    static final Table<?> T_LINKS   = DSL.table(DSL.name("nexus", "catalog_links"));
    static final Table<?> T_CHUNKS  = DSL.table(DSL.name("nexus", "catalog_document_chunks"));
    static final Table<?> T_COLLS   = DSL.table(DSL.name("nexus", "catalog_collections"));
    static final Table<?> T_META    = DSL.table(DSL.name("nexus", "catalog_meta"));

    // ── Owners fields ──────────────────────────────────────────────────────────

    static final Field<String> F_OWN_TENANT = DSL.field(DSL.name("catalog_owners","tenant_id"), String.class);
    static final Field<String> F_OWN_PREFIX = DSL.field(DSL.name("catalog_owners","tumbler_prefix"), String.class);
    static final Field<String> F_OWN_NAME   = DSL.field(DSL.name("catalog_owners","name"), String.class);
    static final Field<String> F_OWN_TYPE   = DSL.field(DSL.name("catalog_owners","owner_type"), String.class);
    static final Field<String> F_OWN_REPO   = DSL.field(DSL.name("catalog_owners","repo_hash"), String.class);
    static final Field<String> F_OWN_DESC   = DSL.field(DSL.name("catalog_owners","description"), String.class);
    static final Field<String> F_OWN_ROOT   = DSL.field(DSL.name("catalog_owners","repo_root"), String.class);
    static final Field<String> F_OWN_HEAD   = DSL.field(DSL.name("catalog_owners","head_hash"), String.class);
    static final Field<Long>   F_OWN_SEQ    = DSL.field(DSL.name("catalog_owners","next_seq"), Long.class);

    // ── Documents fields ───────────────────────────────────────────────────────

    static final Field<String>  F_DOC_TENANT  = DSL.field(DSL.name("catalog_documents","tenant_id"), String.class);
    static final Field<String>  F_DOC_TUMBLER = DSL.field(DSL.name("catalog_documents","tumbler"), String.class);
    static final Field<String>  F_DOC_TITLE   = DSL.field(DSL.name("catalog_documents","title"), String.class);
    static final Field<String>  F_DOC_AUTHOR  = DSL.field(DSL.name("catalog_documents","author"), String.class);
    static final Field<Integer> F_DOC_YEAR    = DSL.field(DSL.name("catalog_documents","year"), Integer.class);
    static final Field<String>  F_DOC_CTYPE   = DSL.field(DSL.name("catalog_documents","content_type"), String.class);
    static final Field<String>  F_DOC_FPATH   = DSL.field(DSL.name("catalog_documents","file_path"), String.class);
    static final Field<String>  F_DOC_CORPUS  = DSL.field(DSL.name("catalog_documents","corpus"), String.class);
    static final Field<String>  F_DOC_PCOLL   = DSL.field(DSL.name("catalog_documents","physical_collection"), String.class);
    static final Field<Integer> F_DOC_CHUNKS  = DSL.field(DSL.name("catalog_documents","chunk_count"), Integer.class);
    static final Field<String>  F_DOC_HEAD    = DSL.field(DSL.name("catalog_documents","head_hash"), String.class);
    static final Field<String>  F_DOC_IDXAT   = DSL.field(DSL.name("catalog_documents","indexed_at"), String.class);
    static final Field<String>  F_DOC_META    = DSL.field(DSL.name("catalog_documents","metadata"), String.class);
    static final Field<Double>  F_DOC_SMTIME  = DSL.field(DSL.name("catalog_documents","source_mtime"), Double.class);
    static final Field<String>  F_DOC_ALIAS   = DSL.field(DSL.name("catalog_documents","alias_of"), String.class);
    static final Field<String>  F_DOC_URI     = DSL.field(DSL.name("catalog_documents","source_uri"), String.class);
    static final Field<Integer> F_DOC_BIBY    = DSL.field(DSL.name("catalog_documents","bib_year"), Integer.class);
    static final Field<String>  F_DOC_BIAU    = DSL.field(DSL.name("catalog_documents","bib_authors"), String.class);
    static final Field<String>  F_DOC_BIVE    = DSL.field(DSL.name("catalog_documents","bib_venue"), String.class);
    static final Field<Integer> F_DOC_BICC    = DSL.field(DSL.name("catalog_documents","bib_citation_count"), Integer.class);
    static final Field<String>  F_DOC_BIS2    = DSL.field(DSL.name("catalog_documents","bib_semantic_scholar_id"), String.class);
    static final Field<String>  F_DOC_BIOA    = DSL.field(DSL.name("catalog_documents","bib_openalex_id"), String.class);
    static final Field<String>  F_DOC_BIDOI   = DSL.field(DSL.name("catalog_documents","bib_doi"), String.class);
    static final Field<String>  F_DOC_BIAT    = DSL.field(DSL.name("catalog_documents","bib_enriched_at"), String.class);
    static final Field<java.time.OffsetDateTime> F_DOC_DELETED_AT =
        DSL.field(DSL.name("catalog_documents","deleted_at"), java.time.OffsetDateTime.class);

    // ── Links fields ───────────────────────────────────────────────────────────

    static final Field<String> F_LNK_TENANT = DSL.field(DSL.name("catalog_links","tenant_id"), String.class);
    static final Field<Long>   F_LNK_ID     = DSL.field(DSL.name("catalog_links","id"), Long.class);
    static final Field<String> F_LNK_FROM   = DSL.field(DSL.name("catalog_links","from_tumbler"), String.class);
    static final Field<String> F_LNK_TO     = DSL.field(DSL.name("catalog_links","to_tumbler"), String.class);
    static final Field<String> F_LNK_TYPE   = DSL.field(DSL.name("catalog_links","link_type"), String.class);
    static final Field<String> F_LNK_FSPAN  = DSL.field(DSL.name("catalog_links","from_span"), String.class);
    static final Field<String> F_LNK_TSPAN  = DSL.field(DSL.name("catalog_links","to_span"), String.class);
    static final Field<String> F_LNK_CRTBY  = DSL.field(DSL.name("catalog_links","created_by"), String.class);
    static final Field<String> F_LNK_CRTAT  = DSL.field(DSL.name("catalog_links","created_at"), String.class);
    static final Field<String> F_LNK_META   = DSL.field(DSL.name("catalog_links","metadata"), String.class);

    // ── Chunks fields ──────────────────────────────────────────────────────────

    static final Field<String>  F_CHK_TENANT = DSL.field(DSL.name("catalog_document_chunks","tenant_id"), String.class);
    static final Field<String>  F_CHK_DOC    = DSL.field(DSL.name("catalog_document_chunks","doc_id"), String.class);
    static final Field<Integer> F_CHK_POS    = DSL.field(DSL.name("catalog_document_chunks","position"), Integer.class);
    static final Field<String>  F_CHK_CHASH  = DSL.field(DSL.name("catalog_document_chunks","chash"), String.class);
    static final Field<Integer> F_CHK_IDX    = DSL.field(DSL.name("catalog_document_chunks","chunk_index"), Integer.class);
    static final Field<Integer> F_CHK_LST    = DSL.field(DSL.name("catalog_document_chunks","line_start"), Integer.class);
    static final Field<Integer> F_CHK_LEN    = DSL.field(DSL.name("catalog_document_chunks","line_end"), Integer.class);
    static final Field<Integer> F_CHK_CST    = DSL.field(DSL.name("catalog_document_chunks","char_start"), Integer.class);
    static final Field<Integer> F_CHK_CEN    = DSL.field(DSL.name("catalog_document_chunks","char_end"), Integer.class);

    // ── Collections fields ─────────────────────────────────────────────────────

    static final Field<String>  F_COL_TENANT = DSL.field(DSL.name("catalog_collections","tenant_id"), String.class);
    static final Field<String>  F_COL_NAME   = DSL.field(DSL.name("catalog_collections","name"), String.class);
    static final Field<String>  F_COL_CTYPE  = DSL.field(DSL.name("catalog_collections","content_type"), String.class);
    static final Field<String>  F_COL_OWNER  = DSL.field(DSL.name("catalog_collections","owner_id"), String.class);
    static final Field<String>  F_COL_EMBD   = DSL.field(DSL.name("catalog_collections","embedding_model"), String.class);
    static final Field<String>  F_COL_MVER   = DSL.field(DSL.name("catalog_collections","model_version"), String.class);
    static final Field<String>  F_COL_DNAME  = DSL.field(DSL.name("catalog_collections","display_name"), String.class);
    static final Field<Integer> F_COL_LEGCY  = DSL.field(DSL.name("catalog_collections","legacy_grandfathered"), Integer.class);
    static final Field<String>  F_COL_SUPBY  = DSL.field(DSL.name("catalog_collections","superseded_by"), String.class);
    static final Field<String>  F_COL_SUPAT  = DSL.field(DSL.name("catalog_collections","superseded_at"), String.class);
    static final Field<String>  F_COL_CRTAT  = DSL.field(DSL.name("catalog_collections","created_at"), String.class);

    // ── Meta fields ────────────────────────────────────────────────────────────

    static final Field<String> F_META_TENANT = DSL.field(DSL.name("catalog_meta","tenant_id"), String.class);
    static final Field<String> F_META_KEY    = DSL.field(DSL.name("catalog_meta","key"), String.class);
    static final Field<String> F_META_VAL    = DSL.field(DSL.name("catalog_meta","value"), String.class);

    // ── EXCLUDED field helpers (avoids the set() overload ambiguity) ───────────

    // These must use the unqualified column name for EXCLUDED pseudo-table references
    private static final Field<String>  EX_OWN_NAME   = DSL.field("EXCLUDED.name",         String.class);
    private static final Field<String>  EX_OWN_TYPE   = DSL.field("EXCLUDED.owner_type",   String.class);
    private static final Field<String>  EX_OWN_REPO   = DSL.field("EXCLUDED.repo_hash",    String.class);
    private static final Field<String>  EX_OWN_DESC   = DSL.field("EXCLUDED.description",  String.class);
    private static final Field<String>  EX_OWN_ROOT   = DSL.field("EXCLUDED.repo_root",    String.class);
    private static final Field<String>  EX_OWN_HEAD   = DSL.field("EXCLUDED.head_hash",    String.class);
    // GREATEST for next_seq on owner ETL import: never downgrade a live-advanced sequence
    // counter on re-import. A faithful migration must carry next_seq from the source so the
    // first post-cutover registerDocument does not collide with an already-imported tumbler.
    private static final Field<Long>    EX_OWN_SEQ_GREATEST =
        DSL.field("GREATEST(catalog_owners.next_seq, EXCLUDED.next_seq)", Long.class);

    private static final Field<String>  EX_DOC_TITLE  = DSL.field("EXCLUDED.title",        String.class);
    private static final Field<String>  EX_DOC_AUTHOR = DSL.field("EXCLUDED.author",       String.class);
    private static final Field<Integer> EX_DOC_YEAR   = DSL.field("EXCLUDED.year",         Integer.class);
    private static final Field<String>  EX_DOC_CTYPE  = DSL.field("EXCLUDED.content_type", String.class);
    private static final Field<String>  EX_DOC_FPATH  = DSL.field("EXCLUDED.file_path",    String.class);
    private static final Field<String>  EX_DOC_CORPUS = DSL.field("EXCLUDED.corpus",       String.class);
    private static final Field<String>  EX_DOC_PCOLL  = DSL.field("EXCLUDED.physical_collection", String.class);
    private static final Field<Integer> EX_DOC_CHUNKS = DSL.field("EXCLUDED.chunk_count",  Integer.class);
    private static final Field<String>  EX_DOC_HEAD   = DSL.field("EXCLUDED.head_hash",    String.class);
    private static final Field<String>  EX_DOC_IDXAT  = DSL.field("EXCLUDED.indexed_at",   String.class);
    private static final Field<String>  EX_DOC_META   = DSL.field("EXCLUDED.metadata",     String.class);
    private static final Field<Double>  EX_DOC_SMTIME = DSL.field("EXCLUDED.source_mtime", Double.class);
    private static final Field<String>  EX_DOC_ALIAS  = DSL.field("EXCLUDED.alias_of",     String.class);
    private static final Field<String>  EX_DOC_URI    = DSL.field("EXCLUDED.source_uri",   String.class);
    private static final Field<Integer> EX_DOC_BIBY   = DSL.field("EXCLUDED.bib_year",     Integer.class);
    private static final Field<String>  EX_DOC_BIAU   = DSL.field("EXCLUDED.bib_authors",  String.class);
    private static final Field<String>  EX_DOC_BIVE   = DSL.field("EXCLUDED.bib_venue",    String.class);
    private static final Field<Integer> EX_DOC_BICC   = DSL.field("EXCLUDED.bib_citation_count", Integer.class);
    private static final Field<String>  EX_DOC_BIS2   = DSL.field("EXCLUDED.bib_semantic_scholar_id", String.class);
    private static final Field<String>  EX_DOC_BIOA   = DSL.field("EXCLUDED.bib_openalex_id", String.class);
    private static final Field<String>  EX_DOC_BIDOI  = DSL.field("EXCLUDED.bib_doi",      String.class);
    private static final Field<String>  EX_DOC_BIAT   = DSL.field("EXCLUDED.bib_enriched_at", String.class);
    // GREATEST for source_mtime ETL
    private static final Field<Double>  EX_DOC_SMTIME_GREATEST =
        DSL.field("GREATEST(catalog_documents.source_mtime, EXCLUDED.source_mtime)", Double.class);

    private static final Field<String>  EX_LNK_FSPAN  = DSL.field("EXCLUDED.from_span",   String.class);
    private static final Field<String>  EX_LNK_TSPAN  = DSL.field("EXCLUDED.to_span",     String.class);
    private static final Field<String>  EX_LNK_CRTBY  = DSL.field("EXCLUDED.created_by",  String.class);
    private static final Field<String>  EX_LNK_META   = DSL.field("EXCLUDED.metadata",    String.class);

    private static final Field<String>  EX_COL_CTYPE  = DSL.field("EXCLUDED.content_type", String.class);
    private static final Field<String>  EX_COL_OWNER  = DSL.field("EXCLUDED.owner_id",    String.class);
    private static final Field<String>  EX_COL_EMBD   = DSL.field("EXCLUDED.embedding_model", String.class);
    private static final Field<String>  EX_COL_MVER   = DSL.field("EXCLUDED.model_version", String.class);
    private static final Field<String>  EX_COL_DNAME  = DSL.field("EXCLUDED.display_name", String.class);
    private static final Field<Integer> EX_COL_LEGCY  = DSL.field("EXCLUDED.legacy_grandfathered", Integer.class);
    private static final Field<String>  EX_COL_SUPBY  = DSL.field("EXCLUDED.superseded_by",  String.class);
    private static final Field<String>  EX_COL_SUPAT  = DSL.field("EXCLUDED.superseded_at",  String.class);
    private static final Field<String>  EX_COL_CRTAT  = DSL.field("EXCLUDED.created_at",     String.class);

    private static final Field<String>  EX_META_VAL   = DSL.field("EXCLUDED.value",       String.class);
    private static final Field<String>  EX_CHK_CHASH  = DSL.field("EXCLUDED.chash",       String.class);
    private static final Field<Integer> EX_CHK_IDX   = DSL.field("EXCLUDED.chunk_index",  Integer.class);
    private static final Field<Integer> EX_CHK_LST   = DSL.field("EXCLUDED.line_start",   Integer.class);
    private static final Field<Integer> EX_CHK_LEN   = DSL.field("EXCLUDED.line_end",     Integer.class);
    private static final Field<Integer> EX_CHK_CST   = DSL.field("EXCLUDED.char_start",   Integer.class);
    private static final Field<Integer> EX_CHK_CEN   = DSL.field("EXCLUDED.char_end",     Integer.class);

    private final TenantScope tenantScope;

    public CatalogRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ══════════════════════════════════════════════════════════════════════════
    // OWNERS
    // ══════════════════════════════════════════════════════════════════════════

    /** Upsert an owner row. ON CONFLICT update all mutable fields. */
    public void upsertOwner(String tenant, Map<String, Object> o) {
        // nexus-45ykb: the wildcard sentinel '*' can never be a registered owner. Enforce
        // it independently here (not merely transitively via AuthFilter) so the invariant
        // holds even if a future internal/admin path reaches this repository outside the
        // request filter — consistent with TokenStore.rejectWildcard at the mint surface.
        if (TenantConstants.isWildcard(tenant)) {
            throw new IllegalArgumentException(
                "tenant '*' is a reserved sentinel and cannot own catalog entries");
        }
        tenantScope.withTenant(tenant, ctx -> {
            // nexus-0cy4b: tumbler_prefix is NOT NULL. The SQLite catalog
            // (Catalog.register_owner) assigns the owner prefix server-side; the
            // HTTP client sends none and expects the same here. Mirror it: reuse
            // the existing owner's prefix for this repo (idempotent), else
            // allocate 1.{MAX+1}. An explicit prefix (ETL/import) is honoured.
            String prefix = s(o, "tumbler_prefix");
            if (prefix == null || prefix.isBlank()) {
                String repoHash = s(o, "repo_hash");
                if (repoHash != null && !repoHash.isBlank()) {
                    prefix = ctx.select(F_OWN_PREFIX)
                                .from(T_OWNERS)
                                .where(F_OWN_REPO.eq(repoHash))
                                .limit(1)
                                .fetchOne(F_OWN_PREFIX);
                }
                if (prefix == null || prefix.isBlank()) {
                    // Next owner number: MAX(int after the first dot) + 1 over
                    // '1.%' owners. RLS scopes this to the tenant.
                    Integer maxNum = ctx.select(
                            DSL.coalesce(
                                DSL.max(DSL.field(
                                    "CAST(split_part(tumbler_prefix, '.', 2) AS INTEGER)",
                                    Integer.class)),
                                DSL.inline(0)))
                        .from(T_OWNERS)
                        .where(F_OWN_PREFIX.like("1.%"))
                        .fetchOne(0, Integer.class);
                    prefix = "1." + ((maxNum == null ? 0 : maxNum) + 1);
                }
            }
            ctx.insertInto(T_OWNERS,
                    F_OWN_TENANT, F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE,
                    F_OWN_REPO, F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD)
               .values(tenant,
                       prefix, s(o,"name"), s(o,"owner_type"),
                       s(o,"repo_hash"), s(o,"description"), nne(s(o,"repo_root")),
                       s(o,"head_hash"))
               .onConflict(F_OWN_TENANT, F_OWN_PREFIX)
               .doUpdate()
               .set(F_OWN_NAME, EX_OWN_NAME)
               .set(F_OWN_TYPE, EX_OWN_TYPE)
               .set(F_OWN_REPO, EX_OWN_REPO)
               .set(F_OWN_DESC, EX_OWN_DESC)
               .set(F_OWN_ROOT, EX_OWN_ROOT)
               .set(F_OWN_HEAD, EX_OWN_HEAD)
               .execute();
            return null;
        });
    }

    /** Return all owners for tenant as list of maps. */
    public List<Map<String, Object>> listOwners(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE, F_OWN_REPO,
                       F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD)
               .from(T_OWNERS)
               .fetch()
               .map(r -> ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7()))
        );
    }

    /** Find owner by repo_hash. Returns null if not found. */
    public Map<String, Object> ownerByRepoHash(String tenant, String repoHash) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE, F_OWN_REPO,
                               F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD)
                       .from(T_OWNERS)
                       .where(F_OWN_REPO.eq(repoHash))
                       .fetchOne();
            return r != null ? ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7()) : null;
        });
    }

    /** Find owners by name. */
    public List<Map<String, Object>> ownersByName(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE, F_OWN_REPO,
                       F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD)
               .from(T_OWNERS)
               .where(F_OWN_NAME.eq(name))
               .fetch()
               .map(r -> ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7()))
        );
    }

    /** Update head_hash for an owner. */
    public int setOwnerHeadHash(String tenant, String tumblerPrefix, String headHash) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(T_OWNERS)
               .set(F_OWN_HEAD, headHash)
               .where(F_OWN_PREFIX.eq(tumblerPrefix))
               .execute()
        );
    }

    // ══════════════════════════════════════════════════════════════════════════
    // DOCUMENTS
    // ══════════════════════════════════════════════════════════════════════════

    /** Upsert a document. ON CONFLICT (tenant_id, tumbler) update all mutable fields. */
    public void upsertDocument(String tenant, Map<String, Object> d) {
        String metaJson = jsonOrNull(d.get("metadata"));
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_DOCS,
                    F_DOC_TENANT, F_DOC_TUMBLER, F_DOC_TITLE, F_DOC_AUTHOR, F_DOC_YEAR,
                    F_DOC_CTYPE, F_DOC_FPATH, F_DOC_CORPUS, F_DOC_PCOLL, F_DOC_CHUNKS,
                    F_DOC_HEAD, F_DOC_IDXAT, F_DOC_META, F_DOC_SMTIME, F_DOC_ALIAS, F_DOC_URI,
                    F_DOC_BIBY, F_DOC_BIAU, F_DOC_BIVE, F_DOC_BICC,
                    F_DOC_BIS2, F_DOC_BIOA, F_DOC_BIDOI, F_DOC_BIAT)
               .values(tenant, s(d,"tumbler"), s(d,"title"), s(d,"author"), i(d,"year"),
                       nne(s(d,"content_type")), nne(s(d,"file_path")), nne(s(d,"corpus")),
                       nne(s(d,"physical_collection")), ni(i(d,"chunk_count"), 0),
                       nne(s(d,"head_hash")), nne(s(d,"indexed_at")),
                       jsonbVal(metaJson),
                       nd(dbl(d,"source_mtime")), nne(s(d,"alias_of")), nne(s(d,"source_uri")),
                       ni(i(d,"bib_year"), 0), nne(s(d,"bib_authors")),
                       nne(s(d,"bib_venue")), ni(i(d,"bib_citation_count"), 0),
                       nne(s(d,"bib_semantic_scholar_id")), nne(s(d,"bib_openalex_id")),
                       nne(s(d,"bib_doi")), nne(s(d,"bib_enriched_at")))
               .onConflict(F_DOC_TENANT, F_DOC_TUMBLER)
               .doUpdate()
               .set(F_DOC_TITLE,  EX_DOC_TITLE)
               .set(F_DOC_AUTHOR, EX_DOC_AUTHOR)
               .set(F_DOC_YEAR,   EX_DOC_YEAR)
               .set(F_DOC_CTYPE,  EX_DOC_CTYPE)
               .set(F_DOC_FPATH,  EX_DOC_FPATH)
               .set(F_DOC_CORPUS, EX_DOC_CORPUS)
               .set(F_DOC_PCOLL,  EX_DOC_PCOLL)
               .set(F_DOC_CHUNKS, EX_DOC_CHUNKS)
               .set(F_DOC_HEAD,   EX_DOC_HEAD)
               .set(F_DOC_IDXAT,  EX_DOC_IDXAT)
               .set(F_DOC_META,   EX_DOC_META)
               .set(F_DOC_SMTIME, EX_DOC_SMTIME)
               .set(F_DOC_ALIAS,  EX_DOC_ALIAS)
               .set(F_DOC_URI,    EX_DOC_URI)
               .set(F_DOC_BIBY,   EX_DOC_BIBY)
               .set(F_DOC_BIAU,   EX_DOC_BIAU)
               .set(F_DOC_BIVE,   EX_DOC_BIVE)
               .set(F_DOC_BICC,   EX_DOC_BICC)
               .set(F_DOC_BIS2,   EX_DOC_BIS2)
               .set(F_DOC_BIOA,   EX_DOC_BIOA)
               .set(F_DOC_BIDOI,  EX_DOC_BIDOI)
               .set(F_DOC_BIAT,   EX_DOC_BIAT)
               .execute();
            return null;
        });
    }

    /**
     * Atomically claim the next sequence number for an owner and register a document.
     *
     * <p>Uses SELECT ... FOR UPDATE on catalog_owners to claim next_seq atomically,
     * increments it, then inserts the document with tumbler = ownerPrefix + "." + seq.
     * Returns the assigned tumbler string.
     *
     * <p>If the owner does not exist, one is created with next_seq=1 and tumbler derived
     * from the owner_prefix directly (the owner should have been registered first).
     */
    public String registerDocument(String tenant, String ownerPrefix, Map<String, Object> fields) {
        if (TenantConstants.isWildcard(tenant)) {
            throw new IllegalArgumentException(
                "tenant '*' is a reserved sentinel and cannot own catalog entries");
        }
        return tenantScope.withTenant(tenant, ctx -> {
            // Ensure owner row exists (idempotent upsert with minimal fields)
            ctx.insertInto(T_OWNERS, F_OWN_TENANT, F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE,
                           F_OWN_REPO, F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD, F_OWN_SEQ)
               .values(tenant, ownerPrefix,
                       s(fields, "owner_name", ownerPrefix),
                       s(fields, "owner_type", "repo"),
                       null, null, "", null, 0L)
               .onConflict(F_OWN_TENANT, F_OWN_PREFIX)
               .doNothing()
               .execute();

            // Idempotency check BEFORE claiming a sequence number — avoids permanent seq gaps
            // on re-registration of existing documents.
            // Idempotency check: only match LIVE (non-tombstoned) docs.
            // A tombstoned source_uri re-registration allocates a NEW tumbler;
            // the trash entry is left untouched (users can restore or purge it separately).
            String srcUri = s(fields, "source_uri", "");
            if (!srcUri.isEmpty()) {
                var existing = ctx.select(F_DOC_TUMBLER).from(T_DOCS)
                                  .where(F_DOC_TENANT.eq(tenant)
                                         .and(F_DOC_URI.eq(srcUri))
                                         .and(F_DOC_DELETED_AT.isNull()))
                                  .fetchOne();
                if (existing != null) return existing.value1();
            }
            String filePath = s(fields, "file_path", "");
            if (!filePath.isEmpty()) {
                var existing = ctx.select(F_DOC_TUMBLER).from(T_DOCS)
                                  .where(F_DOC_TENANT.eq(tenant)
                                         .and(F_DOC_FPATH.eq(filePath))
                                         .and(F_DOC_TUMBLER.startsWith(ownerPrefix + "."))
                                         .and(F_DOC_DELETED_AT.isNull()))
                                  .fetchOne();
                if (existing != null) return existing.value1();
            }

            // No existing document — atomically claim the next sequence number
            long seq = ctx.select(F_OWN_SEQ).from(T_OWNERS)
                          .where(F_OWN_TENANT.eq(tenant).and(F_OWN_PREFIX.eq(ownerPrefix)))
                          .forUpdate()
                          .fetchOne(F_OWN_SEQ);

            ctx.update(T_OWNERS)
               .set(F_OWN_SEQ, seq + 1)
               .where(F_OWN_TENANT.eq(tenant).and(F_OWN_PREFIX.eq(ownerPrefix)))
               .execute();

            String tumbler = ownerPrefix + "." + (seq + 1);

            // Insert document
            String metaJson = jsonOrNull(fields.get("meta"));
            ctx.insertInto(T_DOCS,
                    F_DOC_TENANT, F_DOC_TUMBLER, F_DOC_TITLE, F_DOC_AUTHOR, F_DOC_YEAR,
                    F_DOC_CTYPE, F_DOC_FPATH, F_DOC_CORPUS, F_DOC_PCOLL, F_DOC_CHUNKS,
                    F_DOC_HEAD, F_DOC_IDXAT, F_DOC_META, F_DOC_SMTIME, F_DOC_ALIAS, F_DOC_URI,
                    F_DOC_BIBY, F_DOC_BIAU, F_DOC_BIVE, F_DOC_BICC,
                    F_DOC_BIS2, F_DOC_BIOA, F_DOC_BIDOI, F_DOC_BIAT)
               .values(tenant, tumbler,
                       s(fields, "title", ""),
                       nne(s(fields, "author", null)),
                       ni(i(fields,"year"), 0),
                       nne(s(fields,"content_type", "")),
                       nne(s(fields,"file_path", "")),
                       nne(s(fields,"corpus", "")),
                       nne(s(fields,"physical_collection", "")),
                       ni(i(fields,"chunk_count"), 0),
                       nne(s(fields,"head_hash", "")),
                       nne(s(fields,"indexed_at", "")),
                       jsonbVal(metaJson),
                       nd(dbl(fields,"source_mtime")),
                       nne(s(fields,"alias_of", "")),
                       nne(s(fields,"source_uri", "")),
                       ni(i(fields,"bib_year"), 0),
                       nne(s(fields,"bib_authors", "")),
                       nne(s(fields,"bib_venue", "")),
                       ni(i(fields,"bib_citation_count"), 0),
                       nne(s(fields,"bib_semantic_scholar_id", "")),
                       nne(s(fields,"bib_openalex_id", "")),
                       nne(s(fields,"bib_doi", "")),
                       nne(s(fields,"bib_enriched_at", "")))
               .execute();

            return tumbler;
        });
    }

    /** Fetch a document by tumbler. Returns null if not found. */
    public Map<String, Object> getDocument(String tenant, String tumbler) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(documentFields())
                       .from(T_DOCS)
                       .where(F_DOC_TUMBLER.eq(tumbler).and(F_DOC_DELETED_AT.isNull()))
                       .fetchOne();
            return r != null ? docRowFromRecord(r.intoMap()) : null;
        });
    }

    /**
     * Update mutable document fields. Only non-null fields in the map are updated.
     * Refuses to update tombstoned documents (returns 0).
     * Silently strips {@code deleted_at} from the input map — callers must use
     * {@code document_trash} / {@code document_restore} to manage the tombstone column.
     */
    public int updateDocument(String tenant, String tumbler, Map<String, Object> fields) {
        if (fields.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            var step = ctx.update(T_DOCS);
            UpdateSetMoreStep<?> more = null;
            for (var e : fields.entrySet()) {
                if (e.getValue() == null) continue;
                // Strip deleted_at — must not be settable via updateDocument
                if ("deleted_at".equals(e.getKey())) continue;
                @SuppressWarnings("unchecked")
                Field<Object> f = (Field<Object>) DSL.field(DSL.name("catalog_documents", e.getKey()));
                more = (more == null) ? step.set(f, e.getValue()) : more.set(f, e.getValue());
            }
            if (more == null) return 0;
            // AND deleted_at IS NULL: refuse to update tombstoned documents
            return more.where(F_DOC_TENANT.eq(tenant)
                              .and(F_DOC_TUMBLER.eq(tumbler))
                              .and(F_DOC_DELETED_AT.isNull()))
                       .execute();
        });
    }

    /**
     * Tombstone a document by tumbler (RDR-156 P1.2 soft delete).
     * Sets deleted_at = NOW() (PG server clock, same clock as purge_trash) instead of
     * physically deleting, so fk-001 CASCADE chains (manifest, aspects, highlights, queue)
     * do NOT fire. AND deleted_at IS NULL: idempotent — double-tombstone does not reset
     * the purge age clock.
     * Returns 1 if tombstoned, 0 if not found or already tombstoned.
     */
    public int deleteDocument(String tenant, String tumbler) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(T_DOCS)
               .set(F_DOC_DELETED_AT, DSL.currentOffsetDateTime())
               .where(F_DOC_TUMBLER.eq(tumbler).and(F_DOC_DELETED_AT.isNull()))
               .execute()
        );
    }

    /**
     * FTS search over title/author/corpus/file_path using OR'd tsquery.
     * Optionally filter by content_type. Returns up to limit results.
     */
    public List<Map<String, Object>> searchDocuments(String tenant, String query,
                                                      String contentType, int limit) {
        if (query == null || query.isBlank()) return List.of();
        return tenantScope.withTenant(tenant, ctx -> {
            Condition ftsMatch = DSL.condition(
                "fts_vector @@ plainto_tsquery('english', {0}) OR fts_vector @@ plainto_tsquery('simple', {0})",
                DSL.val(query));
            Condition where = ftsMatch;
            if (contentType != null && !contentType.isBlank()) {
                where = where.and(F_DOC_CTYPE.eq(contentType));
            }
            return ctx.select(documentFields())
                      .from(T_DOCS)
                      .where(where)
                      .limit(limit <= 0 ? 200 : limit)
                      .fetch()
                      .map(r -> docRowFromRecord(r.intoMap()));
        });
    }

    /** Return all documents for this tenant (paginated). */
    public List<Map<String, Object>> listDocuments(String tenant, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields())
               .from(T_DOCS)
               .orderBy(F_DOC_TUMBLER)
               .limit(limit <= 0 ? 200 : limit)
               .offset(offset)
               .fetch()
               .map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Count all documents for this tenant. */
    public long countDocuments(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectCount().from(T_DOCS).fetchOne(0, Long.class)
        );
    }

    /**
     * RDR-159 P-1a (nexus-0wz93): tenant-scoped row counts for the fixed set
     * of migration-verify relations.
     *
     * <p>Backs the {@code nexus.migration} count verification — a safe
     * replacement for the legacy admin-psql shell-out (RDR-152 bars a direct
     * Python PG connection). Each count runs under the request tenant's RLS
     * GUC via {@link TenantScope}, so the result reflects exactly the tenant's
     * migrated rows.
     *
     * <p>Relation names are whitelisted against {@link #VERIFY_RELATIONS}: an
     * unrecognised relation is silently omitted from the result (never a SQL
     * passthrough — the names are not user-authored beyond this fixed set).
     * The caller treats a missing relation as INDETERMINATE, never a pass.
     */
    public Map<String, Long> relationCounts(String tenant, List<String> relations) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, Long> out = new LinkedHashMap<>();
            for (String rel : relations) {
                if (rel == null || !VERIFY_RELATIONS.contains(rel)) {
                    continue;  // whitelist guard — no arbitrary relation counts
                }
                String[] parts = rel.split("\\.", 2);
                Table<?> table = parts.length == 2
                    ? DSL.table(DSL.name(parts[0], parts[1]))
                    : DSL.table(DSL.name(parts[0]));
                Long count = ctx.selectCount().from(table).fetchOne(0, Long.class);
                out.put(rel, count != null ? count : 0L);
            }
            return out;
        });
    }

    /** RDR-159 dim → chunks_&lt;dim&gt; routing; the stored functions accept only these. */
    private static final Set<Integer> MANIFEST_DIMS = Set.of(384, 768, 1024);

    /**
     * RDR-159 P-1b (nexus-avjdd): idempotent collection-stamping backfill.
     *
     * <p>Invokes the {@code nexus.manifest_backfill()} stored function
     * (catalog-004) under the request tenant's RLS GUC, stamping
     * {@code catalog_document_chunks.collection} from the owning doc's
     * {@code physical_collection} where NULL. Returns the number of rows
     * stamped. MUST run BEFORE {@link #manifestOrphans} — rows with a NULL
     * collection are pre-backfill state, not orphans.
     */
    public long manifestBackfill(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rec = ctx.fetchOne("select nexus.manifest_backfill()");
            return rec != null ? rec.get(0, Long.class) : 0L;
        });
    }

    /**
     * RDR-159 P-1b (nexus-avjdd): manifest rows with NO corresponding chunk row
     * in {@code chunks_<dim>} — the exact count PLUS a capped sample, computed in
     * ONE transaction (one RLS-stamped snapshot) so the count and the sample are
     * mutually consistent (CRITICAL: a two-call count-then-sample could diverge
     * under a concurrent write).
     *
     * <p>Invokes the {@code nexus.manifest_orphans(dim)} stored function
     * (catalog-004) under the request tenant's RLS GUC. Because the function is
     * SECURITY INVOKER and the service role is NOBYPASSRLS, FORCE RLS on the
     * base tables (catalog_document_chunks / catalog_documents / chunks_&lt;dim&gt;)
     * scopes the result to the request tenant — the {@code tenant} argument is
     * load-bearing, not advisory. Tombstone-aware (excludes soft-deleted docs).
     *
     * <p>Returns {@code {"count": <long>, "orphans": <List<Map>>}}. {@code count}
     * is exact; {@code orphans} is capped at {@code limit} (> 0). {@code dim}
     * must be 384/768/1024 (validated here so an unsupported dim is a clean
     * IllegalArgumentException → 400, not a PL/pgSQL RAISE → 500).
     *
     * <p>Call protocol: run {@link #manifestBackfill} FIRST — pre-backfill rows
     * (collection IS NULL) are silently excluded by the function, so an orphan
     * check on an un-backfilled manifest reads a false-clean zero.
     */
    public Map<String, Object> manifestOrphanReport(String tenant, int dim, int limit) {
        requireSupportedDim(dim);
        if (limit <= 0) {
            throw new IllegalArgumentException(
                "limit must be > 0 (the sample is bounded; use count for the gate)");
        }
        return tenantScope.withTenant(tenant, ctx -> {
            Long count = ctx.fetchOne(
                "select count(*) from nexus.manifest_orphans(?)", dim
            ).get(0, Long.class);
            var sample = ctx.fetch(
                "select * from nexus.manifest_orphans(?) limit ?", dim, limit
            ).map(org.jooq.Record::intoMap);
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("count", count != null ? count : 0L);
            out.put("orphans", sample);
            return out;
        });
    }

    /**
     * RDR-159 P-1b (nexus-avjdd): exact count of manifest orphans for the given
     * dim — the cheap count-only form for the migration validation gate (zero
     * orphans is the clean signal). Tenant-scoped via the RLS GUC (see
     * {@link #manifestOrphanReport} for the scoping rationale).
     */
    public long manifestOrphanCount(String tenant, int dim) {
        requireSupportedDim(dim);
        return tenantScope.withTenant(tenant, ctx -> {
            var rec = ctx.fetchOne(
                "select count(*) from nexus.manifest_orphans(?)", dim);
            return rec != null ? rec.get(0, Long.class) : 0L;
        });
    }

    private static void requireSupportedDim(int dim) {
        if (!MANIFEST_DIMS.contains(dim)) {
            throw new IllegalArgumentException(
                "unsupported dim " + dim + " — supported values: 384, 768, 1024");
        }
    }

    /** Documents by physical_collection. */
    public List<Map<String, Object>> documentsByCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(T_DOCS)
               .where(F_DOC_PCOLL.eq(collection)).orderBy(F_DOC_TUMBLER)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Documents by file_path (exact). */
    public List<Map<String, Object>> documentsByFilePath(String tenant, String filePath) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(T_DOCS)
               .where(F_DOC_FPATH.eq(filePath))
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Documents by source_uri (exact). */
    public List<Map<String, Object>> documentsBySourceUri(String tenant, String uri) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(T_DOCS)
               .where(F_DOC_URI.eq(uri))
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Documents by owner tumbler prefix. */
    public List<Map<String, Object>> documentsByOwner(String tenant, String ownerPrefix) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(T_DOCS)
               .where(F_DOC_TUMBLER.like(ownerPrefix + ".%"))
               .orderBy(F_DOC_TUMBLER)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Documents by content_type. */
    public List<Map<String, Object>> documentsByContentType(String tenant, String contentType) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(T_DOCS)
               .where(F_DOC_CTYPE.eq(contentType))
               .orderBy(F_DOC_TUMBLER)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Documents by corpus. */
    public List<Map<String, Object>> documentsByCorpus(String tenant, String corpus) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(T_DOCS)
               .where(F_DOC_CORPUS.eq(corpus))
               .orderBy(F_DOC_TUMBLER)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Descendants: all documents with tumbler starting with prefix + "." */
    public List<Map<String, Object>> descendants(String tenant, String prefix) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(T_DOCS)
               .where(F_DOC_TUMBLER.like(prefix + ".%"))
               .orderBy(F_DOC_TUMBLER)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Update physical_collection for one document. */
    public int updateDocumentCollection(String tenant, String tumbler, String newCollection) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(T_DOCS).set(F_DOC_PCOLL, newCollection).where(F_DOC_TUMBLER.eq(tumbler)).execute()
        );
    }

    /** Update physical_collection for many documents. */
    public int updateDocumentsCollectionBatch(String tenant, List<String> tumblers, String newCollection) {
        if (tumblers.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(T_DOCS).set(F_DOC_PCOLL, newCollection).where(F_DOC_TUMBLER.in(tumblers)).execute()
        );
    }

    /** Set alias_of for a document. */
    public int setAlias(String tenant, String tumbler, String aliasOf) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(T_DOCS).set(F_DOC_ALIAS, nne(aliasOf)).where(F_DOC_TUMBLER.eq(tumbler)).execute()
        );
    }

    /** Look up tumbler by (physical_collection, file_path). Returns null if not found. */
    public String lookupDocByCollectionAndPath(String tenant, String collection, String filePath) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(F_DOC_TUMBLER).from(T_DOCS)
                       .where(F_DOC_PCOLL.eq(collection).and(F_DOC_FPATH.eq(filePath)))
                       .fetchOne();
            return r != null ? r.value1() : null;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // LINKS
    // ══════════════════════════════════════════════════════════════════════════

    /** Insert a link. ON CONFLICT (tenant_id, from, to, type) update spans/created_by/metadata. */
    public void upsertLink(String tenant, Map<String, Object> lnk) {
        String metaJson = jsonOrNull(lnk.get("metadata"));
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_LINKS,
                    F_LNK_TENANT, F_LNK_FROM, F_LNK_TO, F_LNK_TYPE,
                    F_LNK_FSPAN, F_LNK_TSPAN, F_LNK_CRTBY, F_LNK_CRTAT, F_LNK_META)
               .values(DSL.val(tenant),
                       DSL.val(s(lnk,"from_tumbler")), DSL.val(s(lnk,"to_tumbler")), DSL.val(s(lnk,"link_type")),
                       DSL.val(nne(s(lnk,"from_span"))), DSL.val(nne(s(lnk,"to_span"))),
                       DSL.val(nne(s(lnk,"created_by"))), DSL.val(nne(s(lnk,"created_at"))),
                       jsonbVal(metaJson))
               .onConflict(F_LNK_TENANT, F_LNK_FROM, F_LNK_TO, F_LNK_TYPE)
               .doUpdate()
               .set(F_LNK_FSPAN, EX_LNK_FSPAN)
               .set(F_LNK_TSPAN, EX_LNK_TSPAN)
               .set(F_LNK_CRTBY, EX_LNK_CRTBY)
               .set(F_LNK_META,  EX_LNK_META)
               .execute();
            return null;
        });
    }

    /** Delete a link by (from, to, type). Returns deleted count. */
    public int deleteLink(String tenant, String fromT, String toT, String linkType) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(T_LINKS)
               .where(F_LNK_FROM.eq(fromT).and(F_LNK_TO.eq(toT)).and(F_LNK_TYPE.eq(linkType)))
               .execute()
        );
    }

    /** Links from a tumbler, optionally filtered by link_type. */
    public List<Map<String, Object>> linksFrom(String tenant, String fromTumbler, String linkType) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition where = F_LNK_FROM.eq(fromTumbler);
            if (linkType != null && !linkType.isBlank()) where = where.and(F_LNK_TYPE.eq(linkType));
            return ctx.select(F_LNK_ID, F_LNK_FROM, F_LNK_TO, F_LNK_TYPE,
                               F_LNK_FSPAN, F_LNK_TSPAN, F_LNK_CRTBY, F_LNK_CRTAT, F_LNK_META)
                      .from(T_LINKS).where(where).fetch()
                      .map(r -> linkRow(r.value1(), r.value2(), r.value3(), r.value4(),
                                        r.value5(), r.value6(), r.value7(), r.value8(), r.value9()));
        });
    }

    /** Links to a tumbler, optionally filtered by link_type. */
    public List<Map<String, Object>> linksTo(String tenant, String toTumbler, String linkType) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition where = F_LNK_TO.eq(toTumbler);
            if (linkType != null && !linkType.isBlank()) where = where.and(F_LNK_TYPE.eq(linkType));
            return ctx.select(F_LNK_ID, F_LNK_FROM, F_LNK_TO, F_LNK_TYPE,
                               F_LNK_FSPAN, F_LNK_TSPAN, F_LNK_CRTBY, F_LNK_CRTAT, F_LNK_META)
                      .from(T_LINKS).where(where).fetch()
                      .map(r -> linkRow(r.value1(), r.value2(), r.value3(), r.value4(),
                                        r.value5(), r.value6(), r.value7(), r.value8(), r.value9()));
        });
    }

    /** Query links with optional filters. */
    public List<Map<String, Object>> queryLinks(String tenant, String fromT, String toT,
                                                 String linkType, String createdBy,
                                                 String createdAtBefore, int limit, int offset,
                                                 String direction, String tumbler) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = DSL.trueCondition();
            if (fromT != null && !fromT.isBlank())         cond = cond.and(F_LNK_FROM.eq(fromT));
            if (toT != null && !toT.isBlank())             cond = cond.and(F_LNK_TO.eq(toT));
            if (linkType != null && !linkType.isBlank())   cond = cond.and(F_LNK_TYPE.eq(linkType));
            if (createdBy != null && !createdBy.isBlank()) cond = cond.and(F_LNK_CRTBY.eq(createdBy));
            if (createdAtBefore != null && !createdAtBefore.isBlank())
                cond = cond.and(F_LNK_CRTAT.lessThan(createdAtBefore));
            // direction + tumbler: filter by tumbler in the appropriate column(s)
            if (tumbler != null && !tumbler.isBlank()) {
                String dir = direction != null ? direction : "both";
                Condition tCond;
                if ("out".equals(dir)) {
                    tCond = F_LNK_FROM.eq(tumbler);
                } else if ("in".equals(dir)) {
                    tCond = F_LNK_TO.eq(tumbler);
                } else {
                    tCond = F_LNK_FROM.eq(tumbler).or(F_LNK_TO.eq(tumbler));
                }
                cond = cond.and(tCond);
            }
            return ctx.select(F_LNK_ID, F_LNK_FROM, F_LNK_TO, F_LNK_TYPE,
                               F_LNK_FSPAN, F_LNK_TSPAN, F_LNK_CRTBY, F_LNK_CRTAT, F_LNK_META)
                      .from(T_LINKS).where(cond).orderBy(F_LNK_ID)
                      .limit(limit <= 0 ? 200 : limit).offset(offset).fetch()
                      .map(r -> linkRow(r.value1(), r.value2(), r.value3(), r.value4(),
                                        r.value5(), r.value6(), r.value7(), r.value8(), r.value9()));
        });
    }

    /** Delete links matching filters. Returns deleted count. */
    public int bulkDeleteLinks(String tenant, String fromT, String toT,
                                String linkType, String createdBy, String createdAtBefore) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = DSL.trueCondition();
            if (fromT != null && !fromT.isBlank())         cond = cond.and(F_LNK_FROM.eq(fromT));
            if (toT != null && !toT.isBlank())             cond = cond.and(F_LNK_TO.eq(toT));
            if (linkType != null && !linkType.isBlank())   cond = cond.and(F_LNK_TYPE.eq(linkType));
            if (createdBy != null && !createdBy.isBlank()) cond = cond.and(F_LNK_CRTBY.eq(createdBy));
            if (createdAtBefore != null && !createdAtBefore.isBlank())
                cond = cond.and(F_LNK_CRTAT.lessThan(createdAtBefore));
            return ctx.deleteFrom(T_LINKS).where(cond).execute();
        });
    }

    /**
     * BFS graph traversal from seed tumblers.
     * Mirrors Catalog.graph() / Catalog.graph_many(): breadth-first up to maxDepth hops.
     *
     * @param seeds      starting tumblers
     * @param linkTypes  empty = all types; non-empty = only these types
     * @param direction  "out"=from only, "in"=to only, "both"=both
     * @param maxDepth   BFS depth cap (1-3)
     * @return map with "nodes" (list of tumblers) and "edges" (list of link maps)
     */
    public Map<String, Object> graphBFS(String tenant, List<String> seeds,
                                         List<String> linkTypes, String direction, int maxDepth) {
        if (seeds == null || seeds.isEmpty()) return Map.of("nodes", List.of(), "edges", List.of());
        int depth = Math.min(Math.max(maxDepth, 1), 3);

        return tenantScope.withTenant(tenant, ctx -> {
            Set<String> visited = new LinkedHashSet<>(seeds);
            List<Map<String, Object>> edges = new ArrayList<>();
            Set<String> frontier = new LinkedHashSet<>(seeds);

            for (int d = 0; d < depth && !frontier.isEmpty(); d++) {
                Set<String> next = new LinkedHashSet<>();
                List<String> fl = new ArrayList<>(frontier);

                Condition dirCond;
                if ("out".equals(direction)) {
                    dirCond = F_LNK_FROM.in(fl);
                } else if ("in".equals(direction)) {
                    dirCond = F_LNK_TO.in(fl);
                } else {
                    dirCond = F_LNK_FROM.in(fl).or(F_LNK_TO.in(fl));
                }
                if (!linkTypes.isEmpty()) {
                    dirCond = dirCond.and(F_LNK_TYPE.in(linkTypes));
                }

                var rows = ctx.select(F_LNK_ID, F_LNK_FROM, F_LNK_TO, F_LNK_TYPE,
                                       F_LNK_FSPAN, F_LNK_TSPAN, F_LNK_CRTBY, F_LNK_CRTAT, F_LNK_META)
                              .from(T_LINKS).where(dirCond).fetch();
                for (var r : rows) {
                    Map<String, Object> lm = linkRow(r.value1(), r.value2(), r.value3(), r.value4(),
                                                      r.value5(), r.value6(), r.value7(), r.value8(), r.value9());
                    edges.add(lm);
                    String fromT = (String) lm.get("from_tumbler");
                    String toT   = (String) lm.get("to_tumbler");
                    if (!visited.contains(fromT)) { next.add(fromT); visited.add(fromT); }
                    if (!visited.contains(toT))   { next.add(toT);   visited.add(toT); }
                }
                frontier = next;
            }

            List<Map<String, Object>> nodes = new ArrayList<>();
            if (!visited.isEmpty()) {
                nodes = ctx.select(documentFields()).from(T_DOCS)
                           .where(F_DOC_TUMBLER.in(new ArrayList<>(visited)))
                           .fetch().map(r -> docRowFromRecord(r.intoMap()));
            }
            return Map.of("nodes", nodes, "edges", edges);
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // DOCUMENT CHUNKS MANIFEST
    // ══════════════════════════════════════════════════════════════════════════

    /** Replace manifest for docId with the provided rows (atomic delete + insert). */
    public void writeManifest(String tenant, String docId, List<Map<String, Object>> rows) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.deleteFrom(T_CHUNKS).where(F_CHK_DOC.eq(docId)).execute();
            for (var row : rows) {
                ctx.insertInto(T_CHUNKS,
                        F_CHK_TENANT, F_CHK_DOC, F_CHK_POS, F_CHK_CHASH, F_CHK_IDX,
                        F_CHK_LST, F_CHK_LEN, F_CHK_CST, F_CHK_CEN)
                   .values(tenant, docId, i(row,"position"), s(row,"chash"), i(row,"chunk_index"),
                           i(row,"line_start"), i(row,"line_end"), i(row,"char_start"), i(row,"char_end"))
                   .execute();
            }
            return null;
        });
    }

    /** Append manifest rows (upsert by position). */
    public void appendManifestChunks(String tenant, String docId, List<Map<String, Object>> rows) {
        tenantScope.withTenant(tenant, ctx -> {
            for (var row : rows) {
                ctx.insertInto(T_CHUNKS,
                        F_CHK_TENANT, F_CHK_DOC, F_CHK_POS, F_CHK_CHASH, F_CHK_IDX,
                        F_CHK_LST, F_CHK_LEN, F_CHK_CST, F_CHK_CEN)
                   .values(tenant, docId, i(row,"position"), s(row,"chash"), i(row,"chunk_index"),
                           i(row,"line_start"), i(row,"line_end"), i(row,"char_start"), i(row,"char_end"))
                   .onConflict(F_CHK_TENANT, F_CHK_DOC, F_CHK_POS)
                   .doUpdate()
                   .set(F_CHK_CHASH, EX_CHK_CHASH)
                   .execute();
            }
            return null;
        });
    }

    /** Get manifest rows for docId, ordered by position. */
    public List<Map<String, Object>> getManifest(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(F_CHK_DOC, F_CHK_POS, F_CHK_CHASH, F_CHK_IDX,
                       F_CHK_LST, F_CHK_LEN, F_CHK_CST, F_CHK_CEN)
               .from(T_CHUNKS).where(F_CHK_DOC.eq(docId)).orderBy(F_CHK_POS)
               .fetch().map(r -> {
                   Map<String, Object> m = new LinkedHashMap<>();
                   m.put("doc_id",      r.value1());
                   m.put("position",    r.value2());
                   m.put("chash",       r.value3());
                   m.put("chunk_index", r.value4());
                   m.put("line_start",  r.value5());
                   m.put("line_end",    r.value6());
                   m.put("char_start",  r.value7());
                   m.put("char_end",    r.value8());
                   return m;
               })
        );
    }

    /** Purge all manifest rows for a document. */
    public int purgeManifest(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(T_CHUNKS).where(F_CHK_DOC.eq(docId)).execute()
        );
    }

    /** Get chashes for a physical_collection via manifest join. */
    public Set<String> chashesForCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.selectDistinct(F_CHK_CHASH)
                          .from(T_CHUNKS)
                          .join(T_DOCS).on(F_CHK_TENANT.eq(F_DOC_TENANT)
                                           .and(F_CHK_DOC.eq(F_DOC_TUMBLER)))
                          .where(F_DOC_PCOLL.eq(collection))
                          .fetch();
            Set<String> result = new LinkedHashSet<>();
            for (var r : rows) result.add(r.value1());
            return result;
        });
    }

    /** Get document tumblers that contain any of the given chashes. */
    public List<String> docsForChashes(String tenant, List<String> chashes) {
        if (chashes.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectDistinct(F_CHK_DOC).from(T_CHUNKS)
               .where(F_CHK_CHASH.in(chashes)).fetch().map(r -> r.value1())
        );
    }

    /** Resync chunk_count on catalog_documents from manifest row count. */
    public int resyncChunkCount(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> {
            int count = ctx.selectCount().from(T_CHUNKS).where(F_CHK_DOC.eq(docId))
                           .fetchOne(0, Integer.class);
            return ctx.update(T_DOCS).set(F_DOC_CHUNKS, count).where(F_DOC_TUMBLER.eq(docId)).execute();
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // COLLECTIONS
    // ══════════════════════════════════════════════════════════════════════════

    /** Upsert a collection. */
    public void upsertCollection(String tenant, Map<String, Object> coll) {
        tenantScope.withTenant(tenant, ctx -> {
            // Raw SQL for superseded_at / created_at: these are timestamptz NULL columns after
            // catalog-002-1-temporal-typing (RDR-156 P0.2).  jOOQ Field<String> would bind as
            // varchar which PostgreSQL rejects; ?::timestamptz accepts ISO-8601 strings or NULL.
            ctx.execute(
                "INSERT INTO nexus.catalog_collections"
                + " (tenant_id, name, content_type, owner_id, embedding_model, model_version,"
                + "  display_name, legacy_grandfathered, superseded_by, superseded_at, created_at)"
                + " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::timestamptz, ?::timestamptz)"
                + " ON CONFLICT (tenant_id, name) DO UPDATE SET"
                + "  content_type=EXCLUDED.content_type, owner_id=EXCLUDED.owner_id,"
                + "  embedding_model=EXCLUDED.embedding_model, model_version=EXCLUDED.model_version,"
                + "  display_name=EXCLUDED.display_name, legacy_grandfathered=EXCLUDED.legacy_grandfathered",
                tenant,
                s(coll, "name"), nne(s(coll, "content_type")),
                nne(s(coll, "owner_id")), nne(s(coll, "embedding_model")),
                nne(s(coll, "model_version")), nne(s(coll, "display_name")),
                ni(i(coll, "legacy_grandfathered"), 0),
                nne(s(coll, "superseded_by")), nz(s(coll, "superseded_at")),
                nz(s(coll, "created_at")));
            return null;
        });
    }

    /** Get a collection by name. Returns null if not found. */
    public Map<String, Object> getCollection(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(F_COL_NAME, F_COL_CTYPE, F_COL_OWNER, F_COL_EMBD, F_COL_MVER,
                               F_COL_DNAME, F_COL_LEGCY, F_COL_SUPBY, F_COL_SUPAT, F_COL_CRTAT)
                       .from(T_COLLS).where(F_COL_NAME.eq(name)).fetchOne();
            return r != null ? collRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(),
                                        r.value6(), r.value7(), r.value8(), r.value9(), r.value10()) : null;
        });
    }

    /** List all collections. */
    public List<Map<String, Object>> listCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(F_COL_NAME, F_COL_CTYPE, F_COL_OWNER, F_COL_EMBD, F_COL_MVER,
                       F_COL_DNAME, F_COL_LEGCY, F_COL_SUPBY, F_COL_SUPAT, F_COL_CRTAT)
               .from(T_COLLS).orderBy(F_COL_NAME).fetch()
               .map(r -> collRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(),
                                  r.value6(), r.value7(), r.value8(), r.value9(), r.value10()))
        );
    }

    /** Delete a collection projection row. */
    public int deleteCollection(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(T_COLLS).where(F_COL_NAME.eq(name)).execute()
        );
    }

    /**
     * Supersede a collection.
     * nz() for superseded_at: '' is invalid in the timestamptz column after catalog-002-1-temporal-typing.
     */
    public int supersedeCollection(String tenant, String name, String supersededBy, String supersededAt) {
        return tenantScope.withTenant(tenant, ctx ->
            // superseded_at is timestamptz NULL after catalog-002-1-temporal-typing; use ?::timestamptz cast.
            ctx.execute(
                "UPDATE nexus.catalog_collections SET superseded_by=?, superseded_at=?::timestamptz"
                + " WHERE tenant_id=? AND name=?",
                supersededBy, nz(supersededAt), tenant, name)
        );
    }

    /** Find highest-versioned collection for (content_type, owner_id, embedding_model). */
    public Map<String, Object> collectionForTuple(String tenant, String contentType,
                                                    String ownerId, String embeddingModel) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(F_COL_NAME, F_COL_CTYPE, F_COL_OWNER, F_COL_EMBD, F_COL_MVER,
                               F_COL_DNAME, F_COL_LEGCY, F_COL_SUPBY, F_COL_SUPAT, F_COL_CRTAT)
                       .from(T_COLLS)
                       .where(F_COL_CTYPE.eq(contentType)
                              .and(F_COL_OWNER.eq(ownerId))
                              .and(F_COL_EMBD.eq(embeddingModel))
                              .and(F_COL_LEGCY.eq(0))
                              .and(F_COL_SUPBY.eq("")))
                       .orderBy(F_COL_NAME.desc()).limit(1).fetchOne();
            return r != null ? collRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(),
                                        r.value6(), r.value7(), r.value8(), r.value9(), r.value10()) : null;
        });
    }

    /**
     * Rename a collection X-&gt;Y, re-homing every in-Postgres denorm-collection table in
     * one RLS-scoped transaction (RDR-164 P3, bead nexus-77vve). Returns per-table re-home
     * counts.
     *
     * <p><b>Mechanism (canonical rename, target absent).</b> The fk-002/fk-003 collection
     * FKs are {@code ON UPDATE NO ACTION}, so a bare {@code UPDATE catalog_collections SET
     * name=Y} is BLOCKED by any child row (proven: CollectionRegistryFkTest group-12). The
     * coherent re-home therefore never touches {@code catalog_collections.name}; instead it:
     * <ol>
     *   <li>INSERTs a new registry row Y, copying X's metadata;</li>
     *   <li>UPDATEs every child denorm collection X-&gt;Y (Y now exists, FK satisfied);</li>
     *   <li>DELETEs the old registry row X (no child references X now, RESTRICT satisfied).</li>
     * </ol>
     * Telemetry tables (search_telemetry, hook_failures) have no FK but ARE re-homed — a
     * rename is not a delete, audit rows follow the new name.
     *
     * <p><b>Cross-model COPY branch (RDR-162, target already exists).</b> When Y is already
     * registered (the bge-768 cross-model migrate registers the target via its chunk upsert),
     * renaming the source registry row would collide on the (tenant_id, name) PK. In that case
     * we repoint {@code catalog_documents.physical_collection} ONLY and leave both registry
     * rows untouched — preserving pre-RDR-164 RDR-162 behavior.
     */
    public Map<String, Integer> renameCollection(String tenant, String oldName, String newName) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, Integer> counts = new LinkedHashMap<>();
            boolean targetExists = ctx.fetchExists(
                ctx.selectOne().from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(newName)));

            if (targetExists) {
                // RDR-162 cross-model COPY branch: repoint catalog_documents only; leave
                // both registry rows (renaming the source would collide on the name PK).
                counts.put("catalog_documents",
                    ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, newName)
                       .where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(oldName)).execute());
                return counts;
            }

            // 1. New registry row Y, copying X's metadata (so children can re-home onto it).
            counts.put("catalog_collections_inserted",
                ctx.insertInto(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                        CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                        CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                        CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED,
                        CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_AT,
                        CATALOG_COLLECTIONS.CREATED_AT)
                    .select(ctx.select(
                            CATALOG_COLLECTIONS.TENANT_ID, DSL.val(newName),
                            CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                            CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                            CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED,
                            CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_AT,
                            CATALOG_COLLECTIONS.CREATED_AT)
                        .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(oldName)))
                    .execute());

            // 2. Re-home every child denorm-collection table X->Y (Y now exists, FK satisfied).
            //    T3 chunk vectors (fk-002 RESTRICT).
            counts.put("chunks_384",  ctx.update(CHUNKS_384).set(CHUNKS_384.COLLECTION, newName).where(CHUNKS_384.COLLECTION.eq(oldName)).execute());
            counts.put("chunks_768",  ctx.update(CHUNKS_768).set(CHUNKS_768.COLLECTION, newName).where(CHUNKS_768.COLLECTION.eq(oldName)).execute());
            counts.put("chunks_1024", ctx.update(CHUNKS_1024).set(CHUNKS_1024.COLLECTION, newName).where(CHUNKS_1024.COLLECTION.eq(oldName)).execute());
            //    chash index (physical_collection; fk-002-4 RESTRICT).
            counts.put("chash_index", ctx.update(CHASH_INDEX).set(CHASH_INDEX.PHYSICAL_COLLECTION, newName).where(CHASH_INDEX.PHYSICAL_COLLECTION.eq(oldName)).execute());
            //    taxonomy: assignments (source_collection, fk-002-5 RESTRICT), topics (fk-003 RESTRICT), meta (fk-003-4 RESTRICT).
            counts.put("topic_assignments", ctx.update(TOPIC_ASSIGNMENTS).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, newName).where(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(oldName)).execute());
            counts.put("topics", ctx.update(TOPICS).set(TOPICS.COLLECTION, newName).where(TOPICS.COLLECTION.eq(oldName)).execute());
            counts.put("taxonomy_meta", ctx.update(TAXONOMY_META).set(TAXONOMY_META.COLLECTION, newName).where(TAXONOMY_META.COLLECTION.eq(oldName)).execute());
            //    centroids (no FK to topics — explicit re-home).
            counts.put("taxonomy_centroids_384",  ctx.update(TAXONOMY_CENTROIDS_384).set(TAXONOMY_CENTROIDS_384.COLLECTION, newName).where(TAXONOMY_CENTROIDS_384.COLLECTION.eq(oldName)).execute());
            counts.put("taxonomy_centroids_768",  ctx.update(TAXONOMY_CENTROIDS_768).set(TAXONOMY_CENTROIDS_768.COLLECTION, newName).where(TAXONOMY_CENTROIDS_768.COLLECTION.eq(oldName)).execute());
            counts.put("taxonomy_centroids_1024", ctx.update(TAXONOMY_CENTROIDS_1024).set(TAXONOMY_CENTROIDS_1024.COLLECTION, newName).where(TAXONOMY_CENTROIDS_1024.COLLECTION.eq(oldName)).execute());
            //    aspect family (fk-003 RESTRICT; incl. doc-less rows).
            counts.put("document_aspects",        ctx.update(DOCUMENT_ASPECTS).set(DOCUMENT_ASPECTS.COLLECTION, newName).where(DOCUMENT_ASPECTS.COLLECTION.eq(oldName)).execute());
            counts.put("document_highlights",     ctx.update(DOCUMENT_HIGHLIGHTS).set(DOCUMENT_HIGHLIGHTS.COLLECTION, newName).where(DOCUMENT_HIGHLIGHTS.COLLECTION.eq(oldName)).execute());
            counts.put("aspect_extraction_queue", ctx.update(ASPECT_EXTRACTION_QUEUE).set(ASPECT_EXTRACTION_QUEUE.COLLECTION, newName).where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(oldName)).execute());
            //    catalog documents (physical_collection).
            counts.put("catalog_documents", ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, newName).where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(oldName)).execute());
            //    audit tables (no FK, but re-homed: audit rows follow the new name — RDR-164
            //    §Approach Phase 3: relevance_log + search_telemetry + hook_failures).
            counts.put("relevance_log",     ctx.update(RELEVANCE_LOG).set(RELEVANCE_LOG.COLLECTION, newName).where(RELEVANCE_LOG.COLLECTION.eq(oldName)).execute());
            counts.put("search_telemetry", ctx.update(SEARCH_TELEMETRY).set(SEARCH_TELEMETRY.COLLECTION, newName).where(SEARCH_TELEMETRY.COLLECTION.eq(oldName)).execute());
            counts.put("hook_failures",     ctx.update(HOOK_FAILURES).set(HOOK_FAILURES.COLLECTION, newName).where(HOOK_FAILURES.COLLECTION.eq(oldName)).execute());

            // 3. Delete the old registry row X (RESTRICT children are now re-homed onto Y).
            counts.put("catalog_collections_deleted",
                ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(oldName)).execute());
            return counts;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // META
    // ══════════════════════════════════════════════════════════════════════════

    public void setMeta(String tenant, String key, String value) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_META, F_META_TENANT, F_META_KEY, F_META_VAL)
               .values(tenant, key, value)
               .onConflict(F_META_TENANT, F_META_KEY)
               .doUpdate()
               .set(F_META_VAL, EX_META_VAL)
               .execute();
            return null;
        });
    }

    public String getMeta(String tenant, String key) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(F_META_VAL).from(T_META).where(F_META_KEY.eq(key)).fetchOne();
            return r != null ? r.value1() : null;
        });
    }

    /** Return owners filtered by owner_type. Used by repos.py:list_repos_dual (nexus-qnp5s). */
    public List<Map<String, Object>> ownersByType(String tenant, String ownerType) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE, F_OWN_REPO,
                       F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD)
               .from(T_OWNERS)
               .where(F_OWN_TYPE.eq(ownerType))
               .fetch()
               .map(r -> ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7()))
        );
    }

    /** Return a single owner by tumbler_prefix. Returns null if not found. */
    public Map<String, Object> ownerByPrefix(String tenant, String tumblerPrefix) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE, F_OWN_REPO,
                               F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD)
                       .from(T_OWNERS)
                       .where(F_OWN_PREFIX.eq(tumblerPrefix))
                       .fetchOne();
            return r != null
                ? ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7())
                : null;
        });
    }

    /**
     * Batch-fetch chunk_count for a set of document tumblers.
     * Returns map of {tumbler -> chunk_count}. Missing docs are absent from the map.
     * Used by scoring.py hot-path (nexus-qnp5s).
     */
    public Map<String, Integer> chunkCountsForDocs(String tenant, List<String> docIds) {
        if (docIds == null || docIds.isEmpty()) return Map.of();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(F_DOC_TUMBLER, F_DOC_CHUNKS)
                          .from(T_DOCS)
                          .where(F_DOC_TUMBLER.in(docIds))
                          .fetch();
            Map<String, Integer> result = new LinkedHashMap<>();
            for (var r : rows) {
                if (r.value2() != null) result.put(r.value1(), r.value2());
            }
            return result;
        });
    }

    /**
     * Batch-fetch outbound links for a set of tumblers.
     * Returns map of {from_tumbler -> list of {from_tumbler, link_type}}.
     * Used by scoring.py hot-path (nexus-qnp5s).
     */
    public Map<String, List<Map<String, Object>>> linksFromBatch(String tenant, List<String> tumblers) {
        if (tumblers == null || tumblers.isEmpty()) return Map.of();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(F_LNK_FROM, F_LNK_TYPE)
                          .from(T_LINKS)
                          .where(F_LNK_FROM.in(tumblers))
                          .fetch();
            Map<String, List<Map<String, Object>>> result = new LinkedHashMap<>();
            for (var r : rows) {
                String fromT    = r.value1();
                String linkType = r.value2();
                result.computeIfAbsent(fromT, k -> new ArrayList<>())
                      .add(Map.of("from_tumbler", fromT, "link_type", linkType));
            }
            return result;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // STATS
    // ══════════════════════════════════════════════════════════════════════════

    public Map<String, Object> stats(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            // RDR-154 P1.2 (nexus-h9qyp): the five scalar counts come from the
            // catalog_stats security_invoker view (per-subquery RLS scopes each to
            // the GUC tenant), replacing five separate selectCount calls and the
            // Java-side hand-assembly that the Python path duplicated.
            var s = ctx.fetchOne(
                "SELECT doc_count, link_count, owner_count, collection_count, chunk_count "
                + "FROM nexus.catalog_stats");
            long docCount  = s.get("doc_count", Long.class);
            long lnkCount  = s.get("link_count", Long.class);
            long ownCount  = s.get("owner_count", Long.class);
            long collCount = s.get("collection_count", Long.class);
            long chkCount  = s.get("chunk_count", Long.class);
            // RDR-154 P1.2: the two GROUP-BY breakdowns also read views (completing
            // the "5+2" collapse, Gap 3). links_by_type ← links_by_type_counts;
            // by_content_type reuses coverage_by_content_type.total (same per-type
            // document count — eliminates the duplicate aggregate the critic flagged).
            var ltypes = ctx.fetch(
                "SELECT link_type, link_count FROM nexus.links_by_type_counts");
            Map<String, Long> byType = new LinkedHashMap<>();
            for (var r : ltypes) byType.put(r.get("link_type", String.class),
                                            r.get("link_count", Long.class));
            // by_content_type: key is "" for null/empty content_type (the view already
            // COALESCEs to ''), matching SQLite Catalog.stats().
            var ctypes = ctx.fetch(
                "SELECT content_type, total FROM nexus.coverage_by_content_type");
            Map<String, Long> byContentType = new LinkedHashMap<>();
            for (var r : ctypes) {
                byContentType.put(r.get("content_type", String.class),
                                  r.get("total", Long.class));
            }
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("doc_count", docCount);
            result.put("link_count", lnkCount);
            result.put("owner_count", ownCount);
            result.put("collection_count", collCount);
            result.put("chunk_count", chkCount);
            result.put("links_by_type", byType);
            result.put("by_content_type", byContentType);
            return result;
        });
    }

    /**
     * nexus-dsu5z: Return {last_indexed, orphan_count} for a physical_collection.
     *
     * <p>{@code last_indexed} — MAX(indexed_at) over documents in the collection
     * (null when no documents found).
     * {@code orphan_count} — count of documents in the collection that have no
     * incoming link (LEFT JOIN catalog_links ON to_tumbler; id IS NULL).
     *
     * <p>Tenant-scoped via TenantScope.withTenant (RLS).
     */
    public Map<String, Object> collectionHealthMeta(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            // RDR-154 P1.2 (nexus-h9qyp): read the collection_health_meta
            // security_invoker view, filtered by collection (predicate pushdown).
            // The view GROUP BYs collection, so it emits NO row for a collection
            // with zero documents — default to {last_indexed:null, orphan_count:0}
            // to preserve the prior contract.
            var r = ctx.fetchOne(
                "SELECT last_indexed, orphan_count, stale_source_ratio "
                + "FROM nexus.collection_health_meta WHERE collection = ?", collection);

            Map<String, Object> result = new LinkedHashMap<>();
            result.put("last_indexed", r == null ? null : r.get("last_indexed", String.class));
            result.put("orphan_count", r == null ? 0L : r.get("orphan_count", Long.class));
            // nexus-agsq7: index-age staleness; null when no dated doc qualifies.
            result.put("stale_source_ratio",
                       r == null ? null : r.get("stale_source_ratio", Double.class));
            return result;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // ANALYTICS QUERIES (nexus-xnz0o CLI port helpers)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Return distinct non-empty physical_collection values across all documents.
     *
     * <p>Backs the Python {@code distinct_doc_collections()} HttpCatalogClient method.
     * Replaces direct SQLite:
     * {@code SELECT DISTINCT physical_collection FROM documents WHERE physical_collection != ''}
     */
    public List<String> distinctDocCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectDistinct(F_DOC_PCOLL)
               .from(T_DOCS)
               .where(F_DOC_PCOLL.ne(""))
               .orderBy(F_DOC_PCOLL)
               .fetch(F_DOC_PCOLL)
        );
    }

    /**
     * Return owners whose repo_root is non-empty, as
     * {@code [{tumbler_prefix, name, owner_type, repo_hash, description, repo_root, head_hash}]}.
     *
     * <p>Backs the Python {@code owners_with_roots()} HttpCatalogClient method.
     * Replaces direct SQLite:
     * {@code SELECT tumbler_prefix, repo_root FROM owners WHERE repo_root != ''}
     */
    public List<Map<String, Object>> ownersWithRoots(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE, F_OWN_REPO,
                       F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD)
               .from(T_OWNERS)
               .where(F_OWN_ROOT.ne(""))
               .fetch()
               .map(r -> ownerRow(r.value1(), r.value2(), r.value3(), r.value4(),
                                  r.value5(), r.value6(), r.value7()))
        );
    }

    /**
     * Return documents with no incoming AND no outgoing links.
     *
     * <p>Backs the Python {@code orphaned_docs()} HttpCatalogClient method.
     * Replaces direct SQLite LEFT JOIN query in orphans_cmd.
     * Returns list of dicts with tumbler, title, content_type, file_path.
     */
    public List<Map<String, Object>> orphanedDocs(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            // Documents with no outgoing links (from_tumbler not in links)
            // AND no incoming links (to_tumbler not in links).
            // Use NOT EXISTS subqueries for cross-tenant RLS safety.
            var noOut = DSL.notExists(
                ctx.selectOne().from(T_LINKS).where(F_LNK_FROM.eq(F_DOC_TUMBLER))
            );
            var noIn = DSL.notExists(
                ctx.selectOne().from(T_LINKS).where(F_LNK_TO.eq(F_DOC_TUMBLER))
            );
            return ctx.select(F_DOC_TUMBLER, F_DOC_TITLE, F_DOC_CTYPE, F_DOC_FPATH)
                      .from(T_DOCS)
                      .where(noOut.and(noIn))
                      .orderBy(F_DOC_TUMBLER)
                      .fetch()
                      .map(r -> {
                          Map<String, Object> m = new LinkedHashMap<>();
                          m.put("tumbler",      r.value1());
                          m.put("title",        r.value2());
                          m.put("content_type", r.value3());
                          m.put("file_path",    r.value4());
                          return m;
                      });
        });
    }

    /**
     * Return documents whose file_path begins with '/' (absolute path).
     *
     * <p>Backs the Python {@code docs_with_absolute_paths()} HttpCatalogClient method.
     * Replaces direct SQLite:
     * {@code SELECT tumbler, file_path, physical_collection FROM documents WHERE file_path LIKE '/%'}
     */
    public List<Map<String, Object>> docsWithAbsolutePaths(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(F_DOC_TUMBLER, F_DOC_FPATH, F_DOC_PCOLL)
               .from(T_DOCS)
               .where(F_DOC_FPATH.startsWith("/"))
               .orderBy(F_DOC_TUMBLER)
               .fetch()
               .map(r -> {
                   Map<String, Object> m = new LinkedHashMap<>();
                   m.put("tumbler",             r.value1());
                   m.put("file_path",           r.value2());
                   m.put("physical_collection", r.value3());
                   return m;
               })
        );
    }

    /**
     * Return (owner_id, repo_root) for a collection by name.
     *
     * <p>Backs the Python {@code get_collection_owner_root()} HttpCatalogClient method.
     * Replaces the two-query pattern in commands/collection.py:
     * {@code SELECT owner_id FROM collections WHERE name=?} then
     * {@code SELECT repo_root FROM owners WHERE tumbler_prefix=?}.
     * Returns null when the collection does not exist.
     */
    public Map<String, Object> collectionOwnerRoot(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(F_COL_OWNER, F_OWN_ROOT)
                       .from(T_COLLS)
                       .leftJoin(T_OWNERS)
                       .on(F_COL_OWNER.eq(F_OWN_PREFIX))
                       .where(F_COL_NAME.eq(name))
                       .fetchOne();
            if (r == null) return null;
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("owner_id",  r.value1());
            m.put("repo_root", r.value2() != null ? r.value2() : "");
            return m;
        });
    }

    /** Return {physical_collection -> doc_count} for all non-empty collections (nexus-xnz0o). */
    public Map<String, Long> collectionDocCounts(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            // RDR-154 P1.2 (nexus-h9qyp): read the collection_doc_counts
            // security_invoker view (replaces the hand-written GROUP BY).
            var rows = ctx.fetch(
                "SELECT physical_collection, doc_count FROM nexus.collection_doc_counts");
            Map<String, Long> result = new LinkedHashMap<>();
            for (var r : rows) {
                result.put(r.get("physical_collection", String.class),
                           r.get("doc_count", Long.class));
            }
            return result;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // COVERAGE ANALYTICS (nexus-3cwnx)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Return per-content-type link coverage: for each distinct content_type in
     * catalog_documents, return {content_type, total, linked} where:
     * <ul>
     *   <li>{@code total}  — COUNT(*) documents of that type (in scope)</li>
     *   <li>{@code linked} — COUNT(DISTINCT tumbler) documents that have
     *       at least one link in either direction (from_tumbler OR to_tumbler)</li>
     * </ul>
     *
     * <p>When {@code ownerPrefix} is non-empty, scope is limited to documents
     * whose tumbler LIKE 'prefix.%' OR = 'prefix' (mirrors the SQLite semantics
     * in coverage_cmd exactly).
     *
     * <p>Tenant-scoped via TenantScope.withTenant (RLS).
     *
     * @param tenant      tenant identifier
     * @param ownerPrefix filter to this owner prefix; empty string = all documents
     * @return list of maps, each with keys {content_type, total, linked}
     */
    public List<Map<String, Object>> coverageByContentType(String tenant, String ownerPrefix) {
        return tenantScope.withTenant(tenant, ctx -> {
            // RDR-154 P1.2 (nexus-h9qyp): replaces the 1+2N N+1 (one selectDistinct
            // + two selectCount per content_type) with a single GROUP BY +
            // count(*) FILTER. The unscoped case reads the coverage_by_content_type
            // security_invoker view; the owner-prefix case runs the same aggregation
            // with the prefix applied BEFORE the GROUP BY (a view cannot be
            // parameterized, but the N+1 is eliminated either way).
            org.jooq.Result<?> rows;
            if (ownerPrefix == null || ownerPrefix.isBlank()) {
                rows = ctx.fetch(
                    "SELECT content_type, total, linked FROM nexus.coverage_by_content_type");
            } else {
                String likePat = ownerPrefix.replaceAll("\\.$", "") + ".%";
                rows = ctx.fetch(
                    "SELECT COALESCE(d.content_type, '') AS content_type, "
                    + "count(*) AS total, "
                    + "count(*) FILTER (WHERE EXISTS ("
                    + "  SELECT 1 FROM nexus.catalog_links l "
                    + "   WHERE l.from_tumbler = d.tumbler OR l.to_tumbler = d.tumbler"
                    + ")) AS linked "
                    + "FROM nexus.catalog_documents d "
                    + "WHERE d.tumbler LIKE ? OR d.tumbler = ? "
                    + "GROUP BY COALESCE(d.content_type, '')",
                    likePat, ownerPrefix);
            }

            List<Map<String, Object>> result = new ArrayList<>();
            for (var r : rows) {
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("content_type", r.get("content_type", String.class));
                row.put("total",        r.get("total", Long.class));
                row.put("linked",       r.get("linked", Long.class));
                result.add(row);
            }
            return result;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // ETL / IMPORT (fidelity-preserving, idempotent)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Fidelity-preserving owner import. Unlike {@link #upsertOwner} (the live write path,
     * which never touches next_seq), the ETL path MUST carry next_seq from the SQLite source.
     * Otherwise every imported owner lands with next_seq=0 and the first post-cutover
     * registerDocument allocates tumbler {@code prefix.1}, colliding with the already-imported
     * document at that tumbler (unique violation on (tenant, tumbler), no ON CONFLICT clause).
     * GREATEST guards re-runs from downgrading a seq the live service has already advanced.
     */
    public void importOwner(String tenant, Map<String, Object> o) {
        if (TenantConstants.isWildcard(tenant)) {
            throw new IllegalArgumentException(
                "tenant '*' is a reserved sentinel and cannot own catalog entries");
        }
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_OWNERS,
                    F_OWN_TENANT, F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE,
                    F_OWN_REPO, F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD, F_OWN_SEQ)
               .values(tenant,
                       s(o,"tumbler_prefix"), s(o,"name"), s(o,"owner_type"),
                       s(o,"repo_hash"), s(o,"description"), nne(s(o,"repo_root")),
                       s(o,"head_hash"), lng(o,"next_seq", 0L))
               .onConflict(F_OWN_TENANT, F_OWN_PREFIX)
               .doUpdate()
               .set(F_OWN_NAME, EX_OWN_NAME)
               .set(F_OWN_TYPE, EX_OWN_TYPE)
               .set(F_OWN_REPO, EX_OWN_REPO)
               .set(F_OWN_DESC, EX_OWN_DESC)
               .set(F_OWN_ROOT, EX_OWN_ROOT)
               .set(F_OWN_HEAD, EX_OWN_HEAD)
               .set(F_OWN_SEQ,  EX_OWN_SEQ_GREATEST)
               .execute();
            return null;
        });
    }

    /** Fidelity-preserving document import. Uses GREATEST for source_mtime. */
    public void importDocument(String tenant, Map<String, Object> d) {
        String metaJson = jsonOrNull(d.get("metadata"));
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_DOCS,
                    F_DOC_TENANT, F_DOC_TUMBLER, F_DOC_TITLE, F_DOC_AUTHOR, F_DOC_YEAR,
                    F_DOC_CTYPE, F_DOC_FPATH, F_DOC_CORPUS, F_DOC_PCOLL, F_DOC_CHUNKS,
                    F_DOC_HEAD, F_DOC_IDXAT, F_DOC_META, F_DOC_SMTIME, F_DOC_ALIAS, F_DOC_URI,
                    F_DOC_BIBY, F_DOC_BIAU, F_DOC_BIVE, F_DOC_BICC,
                    F_DOC_BIS2, F_DOC_BIOA, F_DOC_BIDOI, F_DOC_BIAT)
               .values(tenant, s(d,"tumbler"), s(d,"title"), s(d,"author"), i(d,"year"),
                       nne(s(d,"content_type")), nne(s(d,"file_path")), nne(s(d,"corpus")),
                       nne(s(d,"physical_collection")), ni(i(d,"chunk_count"), 0),
                       nne(s(d,"head_hash")), nne(s(d,"indexed_at")),
                       jsonbVal(metaJson),
                       nd(dbl(d,"source_mtime")), nne(s(d,"alias_of")), nne(s(d,"source_uri")),
                       ni(i(d,"bib_year"), 0), nne(s(d,"bib_authors")),
                       nne(s(d,"bib_venue")), ni(i(d,"bib_citation_count"), 0),
                       nne(s(d,"bib_semantic_scholar_id")), nne(s(d,"bib_openalex_id")),
                       nne(s(d,"bib_doi")), nne(s(d,"bib_enriched_at")))
               .onConflict(F_DOC_TENANT, F_DOC_TUMBLER)
               .doUpdate()
               .set(F_DOC_TITLE,  EX_DOC_TITLE)
               .set(F_DOC_AUTHOR, EX_DOC_AUTHOR)
               .set(F_DOC_YEAR,   EX_DOC_YEAR)
               .set(F_DOC_CTYPE,  EX_DOC_CTYPE)
               .set(F_DOC_FPATH,  EX_DOC_FPATH)
               .set(F_DOC_CORPUS, EX_DOC_CORPUS)
               .set(F_DOC_PCOLL,  EX_DOC_PCOLL)
               .set(F_DOC_CHUNKS, EX_DOC_CHUNKS)
               .set(F_DOC_HEAD,   EX_DOC_HEAD)
               .set(F_DOC_IDXAT,  EX_DOC_IDXAT)
               .set(F_DOC_META,   EX_DOC_META)
               // GREATEST: never downgrade source_mtime on re-import
               .set(F_DOC_SMTIME, EX_DOC_SMTIME_GREATEST)
               .set(F_DOC_ALIAS,  EX_DOC_ALIAS)
               .set(F_DOC_URI,    EX_DOC_URI)
               .set(F_DOC_BIBY,   EX_DOC_BIBY)
               .set(F_DOC_BIAU,   EX_DOC_BIAU)
               .set(F_DOC_BIVE,   EX_DOC_BIVE)
               .set(F_DOC_BICC,   EX_DOC_BICC)
               .set(F_DOC_BIS2,   EX_DOC_BIS2)
               .set(F_DOC_BIOA,   EX_DOC_BIOA)
               .set(F_DOC_BIDOI,  EX_DOC_BIDOI)
               .set(F_DOC_BIAT,   EX_DOC_BIAT)
               .execute();
            return null;
        });
    }

    /**
     * Fidelity-preserving link import. ON CONFLICT DO NOTHING.
     *
     * <p>Stale-snapshot class: link metadata (spans, created_by, created_at) does
     * not converge on re-import — a changed metadata value in the source is silently
     * dropped.  Identity fields (from_tumbler, to_tumbler, link_type) are immutable
     * once the link exists, so this is accepted for the initial migration.  Same
     * convergence gap as pre-nexus-9wz72 importChunk; revisit at final cutover if
     * stale link metadata surfaces in production.
     */
    public void importLink(String tenant, Map<String, Object> lnk) {
        String metaJson = jsonOrNull(lnk.get("metadata"));
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_LINKS,
                    F_LNK_TENANT, F_LNK_FROM, F_LNK_TO, F_LNK_TYPE,
                    F_LNK_FSPAN, F_LNK_TSPAN, F_LNK_CRTBY, F_LNK_CRTAT, F_LNK_META)
               .values(DSL.val(tenant),
                       DSL.val(s(lnk,"from_tumbler")), DSL.val(s(lnk,"to_tumbler")), DSL.val(s(lnk,"link_type")),
                       DSL.val(nne(s(lnk,"from_span"))), DSL.val(nne(s(lnk,"to_span"))),
                       DSL.val(nne(s(lnk,"created_by"))), DSL.val(nne(s(lnk,"created_at"))),
                       jsonbVal(metaJson))
               .onConflict(F_LNK_TENANT, F_LNK_FROM, F_LNK_TO, F_LNK_TYPE)
               .doNothing()
               .execute();
            return null;
        });
    }

    /**
     * Convergent chunk manifest row import.
     *
     * <p>ON CONFLICT (tenant_id, doc_id, position) DO UPDATE SET — updates all
     * data columns so a re-index with changed chunk content converges to the new
     * state. Idempotency is preserved: when the incoming row is identical to the
     * stored row the SET is a no-op in effect (same values written). nexus-9wz72.
     */
    public void importChunk(String tenant, String docId, Map<String, Object> row) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_CHUNKS,
                    F_CHK_TENANT, F_CHK_DOC, F_CHK_POS, F_CHK_CHASH, F_CHK_IDX,
                    F_CHK_LST, F_CHK_LEN, F_CHK_CST, F_CHK_CEN)
               .values(tenant, docId, i(row,"position"), s(row,"chash"), i(row,"chunk_index"),
                       i(row,"line_start"), i(row,"line_end"), i(row,"char_start"), i(row,"char_end"))
               .onConflict(F_CHK_TENANT, F_CHK_DOC, F_CHK_POS)
               .doUpdate()
               .set(F_CHK_CHASH, EX_CHK_CHASH)
               .set(F_CHK_IDX,   EX_CHK_IDX)
               .set(F_CHK_LST,   EX_CHK_LST)
               .set(F_CHK_LEN,   EX_CHK_LEN)
               .set(F_CHK_CST,   EX_CHK_CST)
               .set(F_CHK_CEN,   EX_CHK_CEN)
               .execute();
            return null;
        });
    }

    /**
     * Fidelity-preserving collection import.
     *
     * <p>ON CONFLICT (tenant_id, name): performs DO UPDATE only when the existing row is a
     * backfill/auto-registered STUB (embedding_model = '' AND content_type = '' AND owner_id = '').
     * Stub rows are created by fk-002-0-backfill-stubs or by PgVectorRepository.upsertChunks
     * auto-registration.  They must be upgradable by the RDR-153 catalog ETL, but a re-run
     * must never clobber genuinely-newer live rows.
     *
     * <p>nz() for timestamptz columns: '' is invalid in timestamptz; NULL means "not set".
     * catalog-002-1-temporal-typing (RDR-156 P0.2) converted these columns to timestamptz NULL.
     */
    public void importCollection(String tenant, Map<String, Object> coll) {
        tenantScope.withTenant(tenant, ctx -> {
            // Raw SQL for superseded_at / created_at: timestamptz NULL after catalog-002-1-temporal-typing.
            // DO UPDATE WHERE stub: only upgrades rows where all three discriminator columns are empty
            // (i.e. auto-registered stubs from RDR-156 P0.2 ensure-registration steps).
            ctx.execute(
                "INSERT INTO nexus.catalog_collections"
                + " (tenant_id, name, content_type, owner_id, embedding_model, model_version,"
                + "  display_name, legacy_grandfathered, superseded_by, superseded_at, created_at)"
                + " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::timestamptz, ?::timestamptz)"
                + " ON CONFLICT (tenant_id, name) DO UPDATE SET"
                + "  content_type=EXCLUDED.content_type, owner_id=EXCLUDED.owner_id,"
                + "  embedding_model=EXCLUDED.embedding_model, model_version=EXCLUDED.model_version,"
                + "  display_name=EXCLUDED.display_name, legacy_grandfathered=EXCLUDED.legacy_grandfathered,"
                + "  superseded_by=EXCLUDED.superseded_by,"
                + "  superseded_at=EXCLUDED.superseded_at,"
                + "  created_at=EXCLUDED.created_at"
                + " WHERE catalog_collections.embedding_model='' AND catalog_collections.content_type=''"
                + "  AND catalog_collections.owner_id=''",
                tenant,
                s(coll, "name"), nne(s(coll, "content_type")),
                nne(s(coll, "owner_id")), nne(s(coll, "embedding_model")),
                nne(s(coll, "model_version")), nne(s(coll, "display_name")),
                ni(i(coll, "legacy_grandfathered"), 0),
                nne(s(coll, "superseded_by")), nz(s(coll, "superseded_at")),
                nz(s(coll, "created_at")));
            return null;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    @SuppressWarnings("unchecked")
    private static SelectField<?>[] documentFields() {
        return new SelectField<?>[]{
            F_DOC_TUMBLER, F_DOC_TITLE, F_DOC_AUTHOR, F_DOC_YEAR,
            F_DOC_CTYPE, F_DOC_FPATH, F_DOC_CORPUS, F_DOC_PCOLL, F_DOC_CHUNKS,
            F_DOC_HEAD, F_DOC_IDXAT, F_DOC_META, F_DOC_SMTIME, F_DOC_ALIAS, F_DOC_URI,
            F_DOC_BIBY, F_DOC_BIAU, F_DOC_BIVE, F_DOC_BICC,
            F_DOC_BIS2, F_DOC_BIOA, F_DOC_BIDOI, F_DOC_BIAT
        };
    }

    /** Convert a jOOQ Record.intoMap() to a strongly-typed doc map. */
    private static Map<String, Object> docRowFromRecord(Map<String, Object> raw) {
        Map<String, Object> m = new LinkedHashMap<>();
        // Column names in intoMap() are the unqualified column names
        m.put("tumbler",             raw.getOrDefault("tumbler", null));
        m.put("title",               raw.getOrDefault("title", null));
        m.put("author",              raw.getOrDefault("author", null));
        m.put("year",                raw.getOrDefault("year", null));
        m.put("content_type",        raw.getOrDefault("content_type", null));
        m.put("file_path",           raw.getOrDefault("file_path", null));
        m.put("corpus",              raw.getOrDefault("corpus", null));
        m.put("physical_collection", raw.getOrDefault("physical_collection", null));
        m.put("chunk_count",         raw.getOrDefault("chunk_count", null));
        m.put("head_hash",           raw.getOrDefault("head_hash", null));
        m.put("indexed_at",          raw.getOrDefault("indexed_at", null));
        Object rawMeta = raw.get("metadata");
        if (rawMeta != null) {
            try {
                m.put("metadata", MAPPER.readValue(rawMeta.toString(), MAP_TYPE));
            } catch (Exception e) {
                m.put("metadata", null);
            }
        } else {
            m.put("metadata", null);
        }
        m.put("source_mtime", raw.getOrDefault("source_mtime", 0.0));
        m.put("alias_of",     nne((String) raw.getOrDefault("alias_of", null)));
        m.put("source_uri",   nne((String) raw.getOrDefault("source_uri", null)));
        m.put("bib_year",                raw.getOrDefault("bib_year", 0));
        m.put("bib_authors",             nne((String) raw.getOrDefault("bib_authors", null)));
        m.put("bib_venue",               nne((String) raw.getOrDefault("bib_venue", null)));
        m.put("bib_citation_count",      raw.getOrDefault("bib_citation_count", 0));
        m.put("bib_semantic_scholar_id", nne((String) raw.getOrDefault("bib_semantic_scholar_id", null)));
        m.put("bib_openalex_id",         nne((String) raw.getOrDefault("bib_openalex_id", null)));
        m.put("bib_doi",                 nne((String) raw.getOrDefault("bib_doi", null)));
        m.put("bib_enriched_at",         nne((String) raw.getOrDefault("bib_enriched_at", null)));
        return m;
    }

    private static Map<String, Object> ownerRow(String prefix, String name, String type,
                                                  String repo, String desc, String root, String head) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("tumbler_prefix", prefix);
        m.put("name",           name);
        m.put("owner_type",     type);
        m.put("repo_hash",      repo);
        m.put("description",    desc);
        m.put("repo_root",      nne(root));
        m.put("head_hash",      head);
        return m;
    }

    private static Map<String, Object> linkRow(Long id, String from, String to, String type,
                                                 String fromSpan, String toSpan,
                                                 String createdBy, String createdAt, Object meta) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id",           id);
        m.put("from_tumbler", from);
        m.put("to_tumbler",   to);
        m.put("link_type",    type);
        m.put("from_span",    fromSpan);
        m.put("to_span",      toSpan);
        m.put("created_by",   createdBy);
        m.put("created_at",   createdAt);
        if (meta != null) {
            try {
                m.put("metadata", MAPPER.readValue(meta.toString(), MAP_TYPE));
            } catch (Exception e) { m.put("metadata", null); }
        } else { m.put("metadata", null); }
        return m;
    }

    private static Map<String, Object> collRow(String name, String ctype, String owner,
                                                 String embd, String mver, String dname,
                                                 Integer legcy, String supBy, String supAt, String crAt) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name",                 name);
        m.put("content_type",         nne(ctype));
        m.put("owner_id",             nne(owner));
        m.put("embedding_model",      nne(embd));
        m.put("model_version",        nne(mver));
        m.put("display_name",         nne(dname));
        m.put("legacy_grandfathered", legcy != null ? legcy : 0);
        m.put("superseded_by",        nne(supBy));
        m.put("superseded_at",        nne(supAt));
        m.put("created_at",           nne(crAt));
        return m;
    }

    // ── Null-safe helper statics ───────────────────────────────────────────────

    private static String s(Map<String, Object> m, String k) {
        Object v = m.get(k);
        return v instanceof String sv ? sv : null;
    }

    private static String s(Map<String, Object> m, String k, String def) {
        String v = s(m, k);
        return v != null ? v : def;
    }

    private static Integer i(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v instanceof Number n) return n.intValue();
        return null;
    }

    private static Double dbl(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v instanceof Number n) return n.doubleValue();
        return null;
    }

    /** Non-null empty: returns "" if null. */
    private static String nne(String v) { return v != null ? v : ""; }

    /**
     * Null-or-empty normalizer: returns null for null or blank/empty strings, else returns v.
     * Use for timestamptz columns (catalog_collections.created_at / superseded_at) where the
     * SQLite heritage used '' as the empty sentinel.  Binding '' into a timestamptz column
     * fails at the JDBC driver layer; NULL is the correct representation of "not set".
     * catalog-002-1-temporal-typing (RDR-156 P0.2) converts these columns to timestamptz NULL.
     */
    private static String nz(String v) { return (v != null && !v.isEmpty()) ? v : null; }

    /** Non-null integer: returns def if null. */
    private static int ni(Integer v, int def) { return v != null ? v : def; }

    /** Non-null double: returns 0.0 if null. */
    private static double nd(Double v) { return v != null ? v : 0.0; }

    /** Long with default: returns def if absent or non-numeric. */
    private static long lng(Map<String, Object> m, String k, long def) {
        Object v = m.get(k);
        return v instanceof Number n ? n.longValue() : def;
    }

    private String jsonOrNull(Object v) {
        if (v == null) return null;
        if (v instanceof String sv) return sv.isBlank() ? null : sv;
        try { return MAPPER.writeValueAsString(v); } catch (Exception e) { return null; }
    }

    /**
     * Wrap a JSON string as a jOOQ Field expression that casts to jsonb.
     * When metaJson is null, returns a typed null placeholder.
     * This avoids the set(Field<T>,T) vs set(Field<T>,Field<T>) overload ambiguity
     * that arises when T=Object.
     */
    private static Field<String> jsonbVal(String metaJson) {
        return metaJson != null
            ? DSL.field("CAST(? AS jsonb)", String.class, metaJson)
            : DSL.field("CAST(NULL AS jsonb)", String.class);
    }
}
