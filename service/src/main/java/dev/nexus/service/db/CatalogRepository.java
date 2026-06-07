// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

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

    private static final Field<String>  EX_META_VAL   = DSL.field("EXCLUDED.value",       String.class);
    private static final Field<String>  EX_CHK_CHASH  = DSL.field("EXCLUDED.chash",       String.class);

    private final TenantScope tenantScope;

    public CatalogRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ══════════════════════════════════════════════════════════════════════════
    // OWNERS
    // ══════════════════════════════════════════════════════════════════════════

    /** Upsert an owner row. ON CONFLICT update all mutable fields. */
    public void upsertOwner(String tenant, Map<String, Object> o) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_OWNERS,
                    F_OWN_TENANT, F_OWN_PREFIX, F_OWN_NAME, F_OWN_TYPE,
                    F_OWN_REPO, F_OWN_DESC, F_OWN_ROOT, F_OWN_HEAD)
               .values(tenant,
                       s(o,"tumbler_prefix"), s(o,"name"), s(o,"owner_type"),
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

            // Atomically claim the next sequence number
            long seq = ctx.select(F_OWN_SEQ).from(T_OWNERS)
                          .where(F_OWN_TENANT.eq(tenant).and(F_OWN_PREFIX.eq(ownerPrefix)))
                          .forUpdate()
                          .fetchOne(F_OWN_SEQ);

            ctx.update(T_OWNERS)
               .set(F_OWN_SEQ, seq + 1)
               .where(F_OWN_TENANT.eq(tenant).and(F_OWN_PREFIX.eq(ownerPrefix)))
               .execute();

            String tumbler = ownerPrefix + "." + (seq + 1);

            // Check idempotency by source_uri if present
            String srcUri = s(fields, "source_uri", "");
            if (!srcUri.isEmpty()) {
                var existing = ctx.select(F_DOC_TUMBLER).from(T_DOCS)
                                  .where(F_DOC_URI.eq(srcUri))
                                  .fetchOne();
                if (existing != null) return existing.value1();
            }
            // Check by file_path within owner if present
            String filePath = s(fields, "file_path", "");
            if (!filePath.isEmpty()) {
                var existing = ctx.select(F_DOC_TUMBLER).from(T_DOCS)
                                  .where(F_DOC_FPATH.eq(filePath).and(F_DOC_TUMBLER.startsWith(ownerPrefix + ".")))
                                  .fetchOne();
                if (existing != null) return existing.value1();
            }

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
                       .where(F_DOC_TUMBLER.eq(tumbler))
                       .fetchOne();
            return r != null ? docRowFromRecord(r.intoMap()) : null;
        });
    }

    /** Update mutable document fields. Only non-null fields in the map are updated. */
    public int updateDocument(String tenant, String tumbler, Map<String, Object> fields) {
        if (fields.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            var step = ctx.update(T_DOCS);
            UpdateSetMoreStep<?> more = null;
            for (var e : fields.entrySet()) {
                if (e.getValue() == null) continue;
                @SuppressWarnings("unchecked")
                Field<Object> f = (Field<Object>) DSL.field(DSL.name("catalog_documents", e.getKey()));
                more = (more == null) ? step.set(f, e.getValue()) : more.set(f, e.getValue());
            }
            if (more == null) return 0;
            return more.where(F_DOC_TENANT.eq(tenant).and(F_DOC_TUMBLER.eq(tumbler))).execute();
        });
    }

    /** Delete a document by tumbler. Returns 1 if deleted, 0 if not found. */
    public int deleteDocument(String tenant, String tumbler) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(T_DOCS).where(F_DOC_TUMBLER.eq(tumbler)).execute()
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
                                                 String createdAtBefore, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = DSL.trueCondition();
            if (fromT != null && !fromT.isBlank())         cond = cond.and(F_LNK_FROM.eq(fromT));
            if (toT != null && !toT.isBlank())             cond = cond.and(F_LNK_TO.eq(toT));
            if (linkType != null && !linkType.isBlank())   cond = cond.and(F_LNK_TYPE.eq(linkType));
            if (createdBy != null && !createdBy.isBlank()) cond = cond.and(F_LNK_CRTBY.eq(createdBy));
            if (createdAtBefore != null && !createdAtBefore.isBlank())
                cond = cond.and(F_LNK_CRTAT.lessThan(createdAtBefore));
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
                                String linkType, String createdBy) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = DSL.trueCondition();
            if (fromT != null && !fromT.isBlank())         cond = cond.and(F_LNK_FROM.eq(fromT));
            if (toT != null && !toT.isBlank())             cond = cond.and(F_LNK_TO.eq(toT));
            if (linkType != null && !linkType.isBlank())   cond = cond.and(F_LNK_TYPE.eq(linkType));
            if (createdBy != null && !createdBy.isBlank()) cond = cond.and(F_LNK_CRTBY.eq(createdBy));
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
            ctx.insertInto(T_COLLS,
                    F_COL_TENANT, F_COL_NAME, F_COL_CTYPE, F_COL_OWNER,
                    F_COL_EMBD, F_COL_MVER, F_COL_DNAME, F_COL_LEGCY,
                    F_COL_SUPBY, F_COL_SUPAT, F_COL_CRTAT)
               .values(tenant,
                       s(coll,"name"), nne(s(coll,"content_type")),
                       nne(s(coll,"owner_id")), nne(s(coll,"embedding_model")),
                       nne(s(coll,"model_version")), nne(s(coll,"display_name")),
                       ni(i(coll,"legacy_grandfathered"), 0),
                       nne(s(coll,"superseded_by")), nne(s(coll,"superseded_at")),
                       nne(s(coll,"created_at")))
               .onConflict(F_COL_TENANT, F_COL_NAME)
               .doUpdate()
               .set(F_COL_CTYPE, EX_COL_CTYPE)
               .set(F_COL_OWNER, EX_COL_OWNER)
               .set(F_COL_EMBD,  EX_COL_EMBD)
               .set(F_COL_MVER,  EX_COL_MVER)
               .set(F_COL_DNAME, EX_COL_DNAME)
               .set(F_COL_LEGCY, EX_COL_LEGCY)
               .execute();
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

    /** Supersede a collection. */
    public int supersedeCollection(String tenant, String name, String supersededBy, String supersededAt) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(T_COLLS)
               .set(F_COL_SUPBY, supersededBy)
               .set(F_COL_SUPAT, supersededAt)
               .where(F_COL_NAME.eq(name))
               .execute()
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

    /** Rename a collection across documents + collections. */
    public int renameCollection(String tenant, String oldName, String newName) {
        return tenantScope.withTenant(tenant, ctx -> {
            int docs = ctx.update(T_DOCS).set(F_DOC_PCOLL, newName)
                          .where(F_DOC_PCOLL.eq(oldName)).execute();
            ctx.update(T_COLLS).set(F_COL_NAME, newName).where(F_COL_NAME.eq(oldName)).execute();
            return docs;
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

    // ══════════════════════════════════════════════════════════════════════════
    // STATS
    // ══════════════════════════════════════════════════════════════════════════

    public Map<String, Object> stats(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            long docCount  = ctx.selectCount().from(T_DOCS).fetchOne(0, Long.class);
            long lnkCount  = ctx.selectCount().from(T_LINKS).fetchOne(0, Long.class);
            long ownCount  = ctx.selectCount().from(T_OWNERS).fetchOne(0, Long.class);
            long collCount = ctx.selectCount().from(T_COLLS).fetchOne(0, Long.class);
            long chkCount  = ctx.selectCount().from(T_CHUNKS).fetchOne(0, Long.class);
            var ltypes = ctx.select(F_LNK_TYPE, DSL.count()).from(T_LINKS).groupBy(F_LNK_TYPE).fetch();
            Map<String, Long> byType = new LinkedHashMap<>();
            for (var r : ltypes) byType.put(r.value1(), (long) r.value2());
            return Map.of(
                "doc_count", docCount, "link_count", lnkCount,
                "owner_count", ownCount, "collection_count", collCount,
                "chunk_count", chkCount, "links_by_type", byType
            );
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // ETL / IMPORT (fidelity-preserving, idempotent)
    // ══════════════════════════════════════════════════════════════════════════

    public void importOwner(String tenant, Map<String, Object> o) { upsertOwner(tenant, o); }

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

    /** Fidelity-preserving link import. ON CONFLICT DO NOTHING. */
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

    /** Fidelity-preserving chunk manifest row import. ON CONFLICT DO NOTHING. */
    public void importChunk(String tenant, String docId, Map<String, Object> row) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_CHUNKS,
                    F_CHK_TENANT, F_CHK_DOC, F_CHK_POS, F_CHK_CHASH, F_CHK_IDX,
                    F_CHK_LST, F_CHK_LEN, F_CHK_CST, F_CHK_CEN)
               .values(tenant, docId, i(row,"position"), s(row,"chash"), i(row,"chunk_index"),
                       i(row,"line_start"), i(row,"line_end"), i(row,"char_start"), i(row,"char_end"))
               .onConflict(F_CHK_TENANT, F_CHK_DOC, F_CHK_POS)
               .doNothing()
               .execute();
            return null;
        });
    }

    /** Fidelity-preserving collection import. ON CONFLICT DO NOTHING. */
    public void importCollection(String tenant, Map<String, Object> coll) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(T_COLLS,
                    F_COL_TENANT, F_COL_NAME, F_COL_CTYPE, F_COL_OWNER,
                    F_COL_EMBD, F_COL_MVER, F_COL_DNAME, F_COL_LEGCY,
                    F_COL_SUPBY, F_COL_SUPAT, F_COL_CRTAT)
               .values(tenant,
                       s(coll,"name"), nne(s(coll,"content_type")),
                       nne(s(coll,"owner_id")), nne(s(coll,"embedding_model")),
                       nne(s(coll,"model_version")), nne(s(coll,"display_name")),
                       ni(i(coll,"legacy_grandfathered"), 0),
                       nne(s(coll,"superseded_by")), nne(s(coll,"superseded_at")),
                       nne(s(coll,"created_at")))
               .onConflict(F_COL_TENANT, F_COL_NAME)
               .doNothing()
               .execute();
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

    /** Non-null integer: returns def if null. */
    private static int ni(Integer v, int def) { return v != null ? v : def; }

    /** Non-null double: returns 0.0 if null. */
    private static double nd(Double v) { return v != null ? v : 0.0; }

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
