// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_META;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_STATS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_CONFORMANCE_REPORT;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_1024;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_384;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_768;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_DOC_COUNTS;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_HEALTH_META;
import static dev.nexus.service.jooq.nexus.Tables.COVERAGE_BY_CONTENT_TYPE;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.HOOK_FAILURES;
import static dev.nexus.service.jooq.nexus.Tables.LINKS_BY_TYPE_COUNTS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_VERIFY;
import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_VERIFY_ALL;
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
import dev.nexus.service.vectors.DimTables;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Query;
import org.jooq.SelectField;
import org.jooq.Table;
import org.jooq.UpdateSetMoreStep;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

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
     * count-verification may count. A relation not in this set is never
     * counted (whitelist guard against arbitrary relation names).
     *
     * <p>The javadoc previously claimed this mirrors {@code
     * nexus.migration.orchestrator._VERIFY_TABLES} on the Python side; that
     * module (and the whole SQLite-era migration orchestrator it lived in)
     * was deleted as part of the RDR-158 P4 SQLite retirement, so the symbol
     * no longer exists to mirror. There is currently no {@code src/nexus/}
     * caller of {@link dev.nexus.service.http.CatalogHandler}'s {@code
     * /verify/relation-counts} route at all (the Python
     * {@code relation_counts()} client method is presently callerless) — this
     * set is retained for whichever migration-verify caller replaces the
     * retired orchestrator.
     *
     * <p><b>nexus-20agh:</b> deliberately does NOT include {@code
     * nexus.chash_index} — dropped by the {@code rdr187-001-drop-chash-index}
     * changeset (RDR-187). Unlike {@link
     * SchemaMigrator#CHASH_LEN_CONSTRAINTS}'s {@code chash_index} entry (which
     * is a PRE-migration preflight that genuinely runs, on an aged box, in the
     * window before that same changeset applies), this whitelist backs an
     * HTTP-served verification route, and {@code Main} runs {@link
     * SchemaMigrator#migrate} to completion — including the drop — BEFORE the
     * HTTP server ever binds. So no caller, aged box or otherwise, can ever
     * reach this route while {@code chash_index} still exists: there is no
     * mid-migration window here to protect, only a table that is unqualified
     * gone by the time anything could ask about it. Whitelisting it anyway
     * would guarantee an unhandled "relation does not exist" SQL error the
     * one time a stale caller's relation list still names it, instead of the
     * intended silent-omit/INDETERMINATE contract below.
     */
    private static final Set<String> VERIFY_RELATIONS = Set.of(
        "nexus.memory",
        "nexus.plans",
        "nexus.topics",
        "nexus.topic_assignments",
        "nexus.topic_links",
        "nexus.hook_failures",
        "nexus.nx_answer_runs",
        "nexus.catalog_owners",
        "nexus.catalog_documents",
        "nexus.catalog_collections",
        "nexus.catalog_document_chunks",
        "nexus.catalog_links"
    );

    /**
     * nexus-te885.10 + critique (soundness classes): relations whose counts are
     * served for OBSERVABILITY and the verify-fill watermark's target-shrank
     * invalidation guard ONLY — a count match here is NOT a parity signal
     * (DO-NOTHING dedup collapse + live target-side writes make count equality
     * ambiguous; relevance_log additionally has a rolling TTL sweep). Callers
     * treating "count returned" as "safe for parity" for these relations are
     * WRONG by contract, not just by convention.
     */
    private static final Set<String> COUNT_ONLY_RELATIONS = Set.of(
        "nexus.relevance_log",
        "nexus.search_telemetry",
        "nexus.tier_writes",
        "nexus.frecency"
    );

    static final ObjectMapper MAPPER = new ObjectMapper()
        .configure(com.fasterxml.jackson.databind.DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    // ── Retained hand-built fields (type-skew vs generated jOOQ Tables) ────────
    // nexus-xtmtf: every OTHER plain table/column reference in this class was
    // deduped onto dev.nexus.service.jooq.nexus.Tables.* (CATALOG_OWNERS,
    // CATALOG_DOCUMENTS, CATALOG_LINKS, CATALOG_DOCUMENT_CHUNKS,
    // CATALOG_COLLECTIONS, CATALOG_META — see the static imports above). These
    // four remain hand-built because the generated column type differs from
    // the wire-response value type this class already returns, and switching
    // would either change the JSON shape callers see or force a matching
    // change to the paired EXCLUDED.* fragment (out of scope: EXCLUDED
    // fragments are not plain column references). Wire shapes must not change
    // in this commit — see nexus-xtmtf dedup report.

    // type-skew vs generated (String vs JSONB) — wire shape pinned, see nexus-xtmtf report
    static final Field<String>  F_DOC_META    = DSL.field(DSL.name("catalog_documents","metadata"), String.class);
    // type-skew vs generated (String vs JSONB) — wire shape pinned, see nexus-xtmtf report
    static final Field<String> F_LNK_META   = DSL.field(DSL.name("catalog_links","metadata"), String.class);
    // type-skew vs generated (String vs OffsetDateTime) — wire shape pinned, see nexus-xtmtf report
    static final Field<String>  F_COL_SUPAT  = DSL.field(DSL.name("catalog_collections","superseded_at"), String.class);
    // type-skew vs generated (String vs OffsetDateTime) — wire shape pinned, see nexus-xtmtf report
    static final Field<String>  F_COL_CRTAT  = DSL.field(DSL.name("catalog_collections","created_at"), String.class);

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
    private static final Field<String>  EX_LNK_META   = DSL.field("EXCLUDED.metadata",    String.class);

    /**
     * nexus-s4e1n — the canonical link-merge metadata fold, in SQL.
     *
     * <p>Ports the local {@code catalog_links.py} merge (recovered from the
     * pre-deletion tree) verbatim:
     * <pre>
     *   existing_meta = json.loads(row.metadata) if row.metadata else {}
     *   existing_meta.update(meta)                     # incoming wins per key
     *   co = existing_meta.get("co_discovered_by", [])
     *   if created_by != row.created_by and created_by not in co:
     *       co.append(created_by)
     *   existing_meta["co_discovered_by"] = co
     * </pre>
     * The pre-fix {@code metadata = EXCLUDED.metadata} was a data-loss bug in
     * both directions: it dropped every previously merged key, and it wrote
     * SQL NULL whenever the second caller carried no metadata of its own.
     * {@code src/nexus/mcp/catalog.py} advertises this fold ("Duplicate links
     * are merged with co_discovered_by tracking") for service mode too.
     *
     * <p>{@code jsonb_build_object()} / {@code jsonb_build_array()} rather than
     * the literals {@code '{}'::jsonb} / {@code '[]'::jsonb}: jOOQ plain-SQL
     * templates treat braces as placeholder syntax, and the builder functions
     * produce the identical empty values without them.
     *
     * <p>The {@code jsonb_typeof(...) = 'array'} guard keeps a caller who
     * stored a non-array under that key from crashing the whole upsert — the
     * bad value is replaced rather than concatenated into.
     */
    private static final String LNK_META_BASE =
        "(coalesce(catalog_links.metadata, jsonb_build_object()) "
        + "|| coalesce(EXCLUDED.metadata, jsonb_build_object()))";
    private static final String LNK_META_CO_EXISTING =
        "(case when jsonb_typeof(" + LNK_META_BASE + " -> 'co_discovered_by') = 'array' "
        + "then " + LNK_META_BASE + " -> 'co_discovered_by' else jsonb_build_array() end)";
    private static final Field<String> LNK_META_FOLD = DSL.field(
        LNK_META_BASE + " || jsonb_build_object('co_discovered_by', "
        + "case when EXCLUDED.created_by is not null and EXCLUDED.created_by <> '' "
        + "      and EXCLUDED.created_by is distinct from catalog_links.created_by "
        + "      and not (" + LNK_META_CO_EXISTING + " @> to_jsonb(EXCLUDED.created_by)) "
        + "then " + LNK_META_CO_EXISTING + " || jsonb_build_array(EXCLUDED.created_by) "
        + "else " + LNK_META_CO_EXISTING + " end)",
        String.class);

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
    // RDR-180 (nexus-jxizy.7): bytea chash columns carried as hex in Java —
    // the ChashHex converted type binds/fetches through the codec uniformly.
    private static final Field<String>  CHK_CHASH_HEX   = ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH);
    private static final Field<String>  C384_CHASH_HEX  = ChashHex.hex(CHUNKS_384.CHASH);
    private static final Field<String>  C768_CHASH_HEX  = ChashHex.hex(CHUNKS_768.CHASH);
    private static final Field<String>  C1024_CHASH_HEX = ChashHex.hex(CHUNKS_1024.CHASH);
    // nexus-eslkl / nexus-nl3fn: the engine-side mirror of the client's
    // is_note_shaped(entry) predicate (indexer_utils.py) — a manifest-less
    // MCP store_put / nx store put note's OWN catalog_documents row stamps
    // its identity chash into metadata->>'doc_id' (catalog/store_hook.py
    // ::catalog_store_hook_tracked, meta={"doc_id": doc_id}). Same jOOQ raw-
    // template idiom PgVectorRepository already uses for JSONB extraction
    // (DSL.field("metadata ->> {0}", ...)), qualified to CATALOG_DOCUMENTS
    // explicitly since this field is used in subqueries alongside other
    // tables.
    //
    // TEXT, deliberately NOT compared to a ChashHex-converted chash field
    // directly: ChashHex's Converter operates ONLY at the JDBC value-binding
    // layer (Java String <-> byte[] when a VALUE is bound), never by
    // rewriting the SQL for a bare field-to-field reference — a call site
    // comparing DOC_META_DOC_ID.eq(C384_CHASH_HEX) renders literally as
    // `metadata->>'doc_id' = chunks_384.chash` (text = bytea) and PostgreSQL
    // rejects it with no implicit cast. Every call site instead compares
    // against `encode(chunks_<dim>.chash, 'hex')` (an explicit TEXT-to-TEXT
    // comparison) — see sweepChunks384/768/1024.
    private static final Field<String>  DOC_META_DOC_ID =
        DSL.field("{0} ->> 'doc_id'", String.class, CATALOG_DOCUMENTS.METADATA);
    private static final Field<String>  EX_CHK_COLL   = DSL.field("EXCLUDED.collection",  String.class);
    private static final Field<Integer> EX_CHK_IDX   = DSL.field("EXCLUDED.chunk_index",  Integer.class);
    private static final Field<Integer> EX_CHK_LST   = DSL.field("EXCLUDED.line_start",   Integer.class);
    private static final Field<Integer> EX_CHK_LEN   = DSL.field("EXCLUDED.line_end",     Integer.class);
    private static final Field<Integer> EX_CHK_CST   = DSL.field("EXCLUDED.char_start",   Integer.class);
    private static final Field<Integer> EX_CHK_CEN   = DSL.field("EXCLUDED.char_end",     Integer.class);

    private final TenantScope tenantScope;

    /**
     * Dedicated single-thread daemon executor for {@link #applyPostPurgeVacuum}'s
     * post-commit VACUUM work (nexus-tyxnh). MUST NOT run on the HTTP request
     * thread: the production incident that motivated nexus-0ys55 reported
     * chunks_1024 ALONE took 195s to vacuum, while the Python client's shared
     * httpx timeout is 30s — running synchronously means the client times out
     * mid-VACUUM, sees an apparent failure for a purge that already committed, and
     * a reasonable retry starts a SECOND concurrent vacuum sequence against the
     * same tables during exactly the incident window this bead exists to help
     * with. Single-threaded so at most one vacuum sequence ever runs per engine
     * instance; {@link #vacuumInProgress} is the fast-fail guard that keeps a
     * second {@code purgeTrash} call from queuing behind it instead of reporting
     * {@code already-running} immediately. Mirrors the {@code sweepScheduler} /
     * {@code rekeyJobs} daemon-executor idiom in {@link
     * dev.nexus.service.NexusService}.
     */
    private final ExecutorService vacuumExecutor;

    /**
     * Single-flight guard for {@link #applyPostPurgeVacuum} (nexus-tyxnh): true
     * while a purge-trash VACUUM sequence is running on {@link #vacuumExecutor}.
     * Scoped to this {@link CatalogRepository} instance, matching the executor's
     * own scope (one instance per running engine).
     */
    private final AtomicBoolean vacuumInProgress = new AtomicBoolean(false);

    public CatalogRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
        this.vacuumExecutor = Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "purge-trash-vacuum");
            t.setDaemon(true);
            return t;
        });
    }

    /**
     * Test-only (nexus-tyxnh): observe whether a purge-trash VACUUM sweep is
     * currently running on {@link #vacuumExecutor}. Lets a test bounded-poll for
     * the async sweep's completion (submit, then poll this until false, then
     * assert on database state) instead of a fixed sleep — same idiom as {@link
     * dev.nexus.service.vectors.PgVectorRepository}'s {@code *ForTests} counters.
     */
    public boolean isVacuumInProgressForTests() {
        return vacuumInProgress.get();
    }

    /**
     * Test-only (nexus-tyxnh): force the single-flight guard's state directly, so
     * a test can exercise {@link #applyPostPurgeVacuum}'s {@code already-running}
     * branch deterministically without needing a genuinely slow VACUUM in flight
     * (this class's only realistic way to make one artificially slow would be a
     * multi-GB fixture — disproportionate for testing a boolean CAS). Callers
     * MUST reset to {@code false} afterward (e.g. in a {@code finally}) or every
     * subsequent {@code purgeTrash} call on this instance reports
     * {@code already-running} forever.
     */
    public void setVacuumInProgressForTests(boolean inProgress) {
        vacuumInProgress.set(inProgress);
    }

    /**
     * Shuts down {@link #vacuumExecutor} (nexus-tyxnh). Called from {@link
     * dev.nexus.service.NexusService#stop()} alongside its other executor
     * teardowns ({@code sweepScheduler.shutdownNow()}, {@code rekeyJobs.close()}).
     * Idempotent (repeated {@code shutdownNow()} calls are a no-op past the
     * first); safe to call even when no vacuum was ever submitted.
     */
    public void close() {
        vacuumExecutor.shutdownNow();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // UNIQUE-KEY COMPLETENESS (nexus-0ehwe / pbawi / jq53b / z3ssg)
    // ══════════════════════════════════════════════════════════════════════════
    //
    // An INSERT ... ON CONFLICT takes exactly ONE conflict target — naming two in one
    // statement is a PostgreSQL *syntax* error, verified against PG 17. So on a table
    // with more than one CALLER-determined unique key, every key the arbiter does not
    // name is an unhandled 23505 path. Both catalog tables in this class are in that
    // position, and each separates one ADDRESS key from one or more IDENTITY keys:
    //
    //   catalog_owners     address (tenant_id, tumbler_prefix)             = catalog_owners_pk
    //                      identity (tenant_id, name, owner_type)          = catalog_owners_unique_name_type
    //                      alias    (tenant_id, repo_hash) partial         = idx_catalog_owners_repo_hash
    //   catalog_documents  address (tenant_id, tumbler)                    = catalog_documents_pk
    //                      identity (tenant_id, source_uri) live-only      = ux_catalog_documents_live_source_uri
    //
    // The arbiter keeps naming the ADDRESS (or, at the register sites, the identity).
    // The keys it does NOT name are handled HERE, by resolving them BEFORE the write —
    // the same prevent-rather-than-catch stance claimNextSeq takes for the tumbler PK —
    // with UniqueRaceRetry as the belt for the residual READ COMMITTED race.
    //
    // NOTE: catalog_links also carries two unique keys, but its PK is (tenant_id, id)
    // with id BIGSERIAL and no insert site supplies it, so that key cannot collide.
    // Server-generated keys are exempt; caller-determined ones are not. That, not
    // "the arbiter matches the only unique key", is why catalog_links is clean.

    /** Address key of {@code catalog_owners} (catalog-001-baseline.xml:46). */
    static final String OWNERS_PK = "catalog_owners_pk";
    /** Identity key of {@code catalog_owners} (catalog-001-baseline.xml:47). */
    static final String OWNERS_NAME_TYPE = "catalog_owners_unique_name_type";
    /** Alias key of {@code catalog_owners}, partial (catalog-001-baseline.xml:51). */
    static final String OWNERS_REPO_HASH = "idx_catalog_owners_repo_hash";
    /** Address key of {@code catalog_documents} (catalog-001-baseline.xml:96). */
    static final String DOCUMENTS_PK = "catalog_documents_pk";
    /** Identity key of {@code catalog_documents}, partial (catalog-016:153). */
    static final String DOCUMENTS_LIVE_SOURCE_URI = "ux_catalog_documents_live_source_uri";

    /** The {@code catalog_owners} keys no owner-site arbiter names. */
    static final String[] OWNER_NON_ARBITRATED = {OWNERS_NAME_TYPE, OWNERS_REPO_HASH};
    /** The {@code catalog_documents} keys the address-arbitrated sites do not name. */
    static final String[] DOCUMENT_NON_ARBITRATED = {DOCUMENTS_LIVE_SOURCE_URI};

    /**
     * The owner address that already holds this identity, or {@code null}.
     *
     * <p>Resolution order is {@code (name, owner_type)} then {@code repo_hash}, and the
     * two must AGREE: if the identity key and the alias key point at different owners,
     * there is no single "the existing owner" and proceeding would pick one arbitrarily.
     * That disagreement is itself refused.
     *
     * <p>{@code repo_hash} is only consulted when non-blank, mirroring the partial
     * index's own predicate ({@code repo_hash IS NOT NULL AND repo_hash != ''}) — a
     * blank hash is not an identity and must not alias every other blank-hash owner
     * onto one row.
     */
    private static String resolveOwnerAddress(DSLContext ctx, String tenant,
                                              String name, String ownerType, String repoHash) {
        String byIdentity = null;
        if (name != null && !name.isBlank() && ownerType != null && !ownerType.isBlank()) {
            byIdentity = ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX).from(CATALOG_OWNERS)
                            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)
                                   .and(CATALOG_OWNERS.NAME.eq(name))
                                   .and(CATALOG_OWNERS.OWNER_TYPE.eq(ownerType)))
                            .limit(1)
                            .fetchOne(CATALOG_OWNERS.TUMBLER_PREFIX);
        }
        String byRepoHash = null;
        if (repoHash != null && !repoHash.isBlank()) {
            byRepoHash = ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX).from(CATALOG_OWNERS)
                            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)
                                   .and(CATALOG_OWNERS.REPO_HASH.eq(repoHash)))
                            .limit(1)
                            .fetchOne(CATALOG_OWNERS.TUMBLER_PREFIX);
        }
        if (byIdentity != null && byRepoHash != null && !byIdentity.equals(byRepoHash)) {
            throw new CatalogIdentityConflictException(
                OWNERS_REPO_HASH, "repo_hash=" + repoHash, byRepoHash, byIdentity);
        }
        return byIdentity != null ? byIdentity : byRepoHash;
    }

    /**
     * Refuse a write that would give one owner identity to two addresses.
     *
     * <p>Called with the address the caller ASKED for. A resolution to that same address
     * is the ordinary idempotent case and returns quietly; a resolution to a DIFFERENT
     * address is the rename/merge hazard and is refused by name.
     */
    private static void guardOwnerIdentity(DSLContext ctx, String tenant, String attemptedPrefix,
                                           String name, String ownerType, String repoHash) {
        String existing = resolveOwnerAddress(ctx, tenant, name, ownerType, repoHash);
        if (existing != null && !existing.equals(attemptedPrefix)) {
            boolean viaRepoHash = repoHash != null && !repoHash.isBlank()
                && ctx.fetchExists(ctx.selectOne().from(CATALOG_OWNERS)
                    .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)
                           .and(CATALOG_OWNERS.REPO_HASH.eq(repoHash))
                           .and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(existing))));
            throw new CatalogIdentityConflictException(
                viaRepoHash ? OWNERS_REPO_HASH : OWNERS_NAME_TYPE,
                viaRepoHash ? "repo_hash=" + repoHash
                            : "owner name=" + name + " owner_type=" + ownerType,
                existing, attemptedPrefix);
        }
    }

    /**
     * The tumbler of the LIVE document that already holds {@code sourceUri}, or
     * {@code null}.
     *
     * <p>Scoped to live rows and non-empty URIs to match the partial index's predicate
     * exactly ({@code deleted_at IS NULL AND source_uri <> ''}). A tombstoned row does
     * NOT hold its source_uri — re-registering it is legal and must not be refused.
     */
    private static String liveTumblerForSourceUri(DSLContext ctx, String tenant, String sourceUri) {
        if (sourceUri == null || sourceUri.isEmpty()) return null;
        return ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
                  .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                         .and(CATALOG_DOCUMENTS.SOURCE_URI.eq(sourceUri))
                         .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                  .limit(1)
                  .fetchOne(CATALOG_DOCUMENTS.TUMBLER);
    }

    /**
     * Refuse an address-arbitrated document write that would move a live
     * {@code source_uri} onto a second row.
     *
     * <p>Guards BOTH arms of the upsert, which is the half nexus-z3ssg named and the
     * half that is easy to miss: a fresh INSERT at a new tumbler carrying an
     * already-live source_uri violates the index, AND an insert whose PK conflict is
     * cleanly HANDLED then violates it again from the DO UPDATE arm, because that arm
     * sets {@code source_uri} from the excluded row. Both were reproduced against PG 17
     * before this guard was written.
     */
    private static void guardDocumentIdentity(DSLContext ctx, String tenant,
                                              String attemptedTumbler, String sourceUri) {
        String existing = liveTumblerForSourceUri(ctx, tenant, sourceUri);
        if (existing != null && !existing.equals(attemptedTumbler)) {
            throw new CatalogIdentityConflictException(
                DOCUMENTS_LIVE_SOURCE_URI, "source_uri=" + sourceUri, existing, attemptedTumbler);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // OWNERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * The DO UPDATE assignments for {@code upsertOwner}, PRESERVE-ON-OMIT when the row was
     * reached by identity rather than by address (nexus-upg3s).
     *
     * <p><strong>The bug this exists to prevent.</strong> Resolving a prefix-less payload onto
     * an existing owner makes the INSERT conflict on the PK and take the DO UPDATE arm. That
     * arm used to assign EVERY column from EXCLUDED unconditionally — so a payload that merely
     * OMITTED a field wrote emptiness over the incumbent's value ({@code s()} yields null for
     * an absent key, {@code nne()} yields ""). The reachable shape is not exotic:
     * {@code register_owner("nexus", "repo")} in http_catalog_client sends exactly
     * {@code {name, owner_type}}, because that client builds its payload omit-if-falsy.
     *
     * <p>Before the identity-converge change, that same payload allocated a FRESH prefix,
     * collided on {@code catalog_owners_unique_name_type}, and rolled back — a loud 23505 that
     * changed nothing. Converging made the statement REACH a row it could never reach before,
     * which turned a safe no-op into a silent partial overwrite.
     *
     * <p>{@code repo_root} is the severe one: {@code deriveSourceUri} anchors every derived
     * source_uri on it (RDR-096 / nexus-3e4s), so blanking it makes the idempotency lookup miss
     * and already-registered files draw NEW tumblers — the duplicate-document class
     * catalog-016 exists to prevent, and one the partial unique index cannot catch because the
     * resulting URIs genuinely differ.
     *
     * <p><strong>Scope, deliberately narrow.</strong> Only the converge path preserves. When the
     * caller supplied an EXPLICIT prefix it named this row on purpose, and the blind overwrite
     * there is pre-existing behaviour that this change does not touch — widening the fix to
     * that path is a separate decision, not a bug fix.
     *
     * <p>Presence, not truthiness, is the test: a caller that explicitly sends {@code ""} means
     * to clear the field and still can.
     */
    private static Map<Field<?>, Object> ownerUpdateSet(Map<String, Object> o, boolean converged) {
        Map<Field<?>, Object> upd = new LinkedHashMap<>();
        // Identity columns: always assigned. On the converge path these are what we matched
        // on, so EXCLUDED already carries the incumbent's own values.
        upd.put(CATALOG_OWNERS.NAME, EX_OWN_NAME);
        upd.put(CATALOG_OWNERS.OWNER_TYPE, EX_OWN_TYPE);
        if (!converged || o.containsKey("repo_hash"))   upd.put(CATALOG_OWNERS.REPO_HASH, EX_OWN_REPO);
        if (!converged || o.containsKey("description")) upd.put(CATALOG_OWNERS.DESCRIPTION, EX_OWN_DESC);
        if (!converged || o.containsKey("repo_root"))   upd.put(CATALOG_OWNERS.REPO_ROOT, EX_OWN_ROOT);
        if (!converged || o.containsKey("head_hash"))   upd.put(CATALOG_OWNERS.HEAD_HASH, EX_OWN_HEAD);
        // nexus-cw262: any live upsert (register/converge) is affirmative evidence the
        // owner is in active use again — clear deactivated_at unconditionally, the same
        // way a repo that was census-flagged path_vanished and later remounted/re-cloned
        // self-heals back into the default owner list the next time `nx index repo`
        // registers into it. Explicit `POST /owners/reactivate` exists for the no-write
        // correction case (Hal manually undoing a batch deactivation); this is the
        // automatic path.
        upd.put(CATALOG_OWNERS.DEACTIVATED_AT, null);
        return upd;
    }

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
        UniqueRaceRetry.run("upsertOwner", OWNER_NON_ARBITRATED, () ->
        tenantScope.withTenant(tenant, ctx -> {
            // nexus-0cy4b: tumbler_prefix is NOT NULL. The SQLite catalog
            // (Catalog.register_owner) assigns the owner prefix server-side; the
            // HTTP client sends none and expects the same here. Mirror it: reuse
            // the existing owner's prefix for this repo (idempotent), else
            // allocate 1.{MAX+1}. An explicit prefix (ETL/import) is honoured.
            String prefix = s(o, "tumbler_prefix");
            String name = s(o, "name");
            String ownerType = s(o, "owner_type");
            String repoHash = s(o, "repo_hash");

            // nexus-jq53b, Hal decision (a) 2026-07-28: (tenant, name, owner_type) IS
            // an identity key, so register_owner is idempotent BY it. Resolve it BEFORE
            // allocating, not after — the pre-fix order allocated a fresh prefix first,
            // which made the ON CONFLICT (tenant_id, tumbler_prefix) arbiter miss the
            // existing row and let catalog_owners_unique_name_type fire as a raw 23505.
            // That is reachable with no concurrency at all: registering the same
            // name+type twice was enough (the nexus-aqbrk sequential repro).
            String existing = resolveOwnerAddress(ctx, tenant, name, ownerType, repoHash);
            // nexus-upg3s: did this call land on an EXISTING row purely by IDENTITY — no
            // address given, none allocated? That is the one case where the caller never
            // named this row, so an OMITTED field means "leave it alone", not "blank it".
            // See the DO UPDATE below; this flag is the whole reason it is conditional.
            boolean convergedOnExisting = false;
            if (prefix == null || prefix.isBlank()) {
                prefix = existing;
                if (prefix == null || prefix.isBlank()) {
                    // Next owner number: MAX(int after the first dot) + 1 over
                    // '1.%' owners. RLS scopes this to the tenant.
                    Integer maxNum = ctx.select(
                            DSL.coalesce(
                                DSL.max(DSL.field(
                                    "CAST(split_part(tumbler_prefix, '.', 2) AS INTEGER)",
                                    Integer.class)),
                                DSL.inline(0)))
                        .from(CATALOG_OWNERS)
                        .where(CATALOG_OWNERS.TUMBLER_PREFIX.like("1.%"))
                        .fetchOne(0, Integer.class);
                    prefix = "1." + ((maxNum == null ? 0 : maxNum) + 1);
                } else {
                    convergedOnExisting = true;
                }
            } else {
                // An EXPLICIT prefix is an address. Honour it — but not by silently
                // stealing an identity that already belongs to a different owner, and
                // not by routing a rename through this path (Hal's jq53b constraint 1).
                guardOwnerIdentity(ctx, tenant, prefix, name, ownerType, repoHash);
            }
            ctx.insertInto(CATALOG_OWNERS,
                    CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE,
                    CATALOG_OWNERS.REPO_HASH, CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH)
               .values(tenant,
                       prefix, s(o,"name"), s(o,"owner_type"),
                       s(o,"repo_hash"), s(o,"description"), nne(s(o,"repo_root")),
                       s(o,"head_hash"))
               .onConflict(CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX)
               .doUpdate()
               .set(ownerUpdateSet(o, convergedOnExisting))
               .execute();
            return null;
        }));
    }

    /** Return all ACTIVE (non-deactivated) owners for tenant as list of maps. */
    public List<Map<String, Object>> listOwners(String tenant) {
        return listOwners(tenant, false);
    }

    /**
     * Return owners for tenant as list of maps (nexus-cw262: {@code
     * includeDeactivated} audit option). Default read path ({@code
     * includeDeactivated=false}, what {@link #listOwners(String)} and
     * {@code GET /v1/catalog/owners/list} use) excludes deactivated owners
     * — the entire point of the deactivate route is that a dead owner
     * stops appearing here, so doctor's git-hooks walk and the 7kl32
     * census stop re-surfacing the same debris every run.
     */
    public List<Map<String, Object>> listOwners(String tenant, boolean includeDeactivated) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = includeDeactivated ? DSL.trueCondition() : CATALOG_OWNERS.DEACTIVATED_AT.isNull();
            // nexus-0ehwe item 3: next_seq on the LIST too — the drift check
            // must be able to sweep EVERY owner without N round trips.
            return ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH,
                       CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH,
                       CATALOG_OWNERS.NEXT_SEQ, CATALOG_OWNERS.DEACTIVATED_AT)
               .from(CATALOG_OWNERS)
               .where(cond)
               .fetch()
               .map(r -> ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7(),
                                  r.value8(), r.value9()));
        });
    }

    /** Find owner by repo_hash. Returns null if not found. */
    public Map<String, Object> ownerByRepoHash(String tenant, String repoHash) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH,
                               CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH)
                       .from(CATALOG_OWNERS)
                       .where(CATALOG_OWNERS.REPO_HASH.eq(repoHash))
                       .fetchOne();
            return r != null ? ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7()) : null;
        });
    }

    /** Find owners by name. */
    public List<Map<String, Object>> ownersByName(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH,
                       CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH)
               .from(CATALOG_OWNERS)
               .where(CATALOG_OWNERS.NAME.eq(name))
               .fetch()
               .map(r -> ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7()))
        );
    }

    /** Update head_hash for an owner. */
    public int setOwnerHeadHash(String tenant, String tumblerPrefix, String headHash) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(CATALOG_OWNERS)
               .set(CATALOG_OWNERS.HEAD_HASH, headHash)
               .where(CATALOG_OWNERS.TUMBLER_PREFIX.eq(tumblerPrefix))
               .execute()
        );
    }

    // ══════════════════════════════════════════════════════════════════════════
    // DOCUMENTS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Upsert a document. ON CONFLICT (tenant_id, tumbler) update all mutable fields.
     *
     * <p>nexus-mqd6t (Hal ruling, tombstone NON-RESURRECTION): this is the ONE
     * sanctioned way a tombstoned row comes back. {@link #updateDocument}
     * refuses tombstoned targets outright ({@code deleted_at IS NULL} in its
     * WHERE), so an incidental field write can never resurrect a deleted
     * document; an EXPLICIT register of the same tumbler clears the tombstone
     * here. Before this, a register addressed at a tombstoned tumbler updated
     * every column and left {@code deleted_at} set — the row was written and
     * then stayed invisible to every reader, silently.
     */
    public void upsertDocument(String tenant, Map<String, Object> d) {
        String metaJson = jsonOrNull(d.get("metadata"));
        UniqueRaceRetry.run("upsertDocument", DOCUMENT_NON_ARBITRATED, () ->
        tenantScope.withTenant(tenant, ctx -> {
            // nexus-0ehwe arbiter class / nexus-z3ssg shape. This statement arbitrates
            // the ADDRESS key (tenant_id, tumbler) and sets source_uri from the excluded
            // row, so it can violate ux_catalog_documents_live_source_uri on BOTH arms:
            // a fresh INSERT at a new tumbler carrying an already-live source_uri, and an
            // insert whose PK conflict is cleanly HANDLED and whose DO UPDATE arm then
            // moves that source_uri onto a second live row. Both reproduce on PG 17.
            guardDocumentIdentity(ctx, tenant, s(d, "tumbler"), nne(s(d, "source_uri")));
            ctx.insertInto(CATALOG_DOCUMENTS,
                    CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.YEAR,
                    CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.CORPUS, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT,
                    CATALOG_DOCUMENTS.HEAD_HASH, CATALOG_DOCUMENTS.INDEXED_AT, F_DOC_META, CATALOG_DOCUMENTS.SOURCE_MTIME, CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.SOURCE_URI,
                    CATALOG_DOCUMENTS.BIB_YEAR, CATALOG_DOCUMENTS.BIB_AUTHORS, CATALOG_DOCUMENTS.BIB_VENUE, CATALOG_DOCUMENTS.BIB_CITATION_COUNT,
                    CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID, CATALOG_DOCUMENTS.BIB_OPENALEX_ID, CATALOG_DOCUMENTS.BIB_DOI, CATALOG_DOCUMENTS.BIB_ENRICHED_AT)
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
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doUpdate()
               .set(CATALOG_DOCUMENTS.TITLE,  EX_DOC_TITLE)
               .set(CATALOG_DOCUMENTS.AUTHOR, EX_DOC_AUTHOR)
               .set(CATALOG_DOCUMENTS.YEAR,   EX_DOC_YEAR)
               .set(CATALOG_DOCUMENTS.CONTENT_TYPE,  EX_DOC_CTYPE)
               .set(CATALOG_DOCUMENTS.FILE_PATH,  EX_DOC_FPATH)
               .set(CATALOG_DOCUMENTS.CORPUS, EX_DOC_CORPUS)
               .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,  EX_DOC_PCOLL)
               .set(CATALOG_DOCUMENTS.CHUNK_COUNT, EX_DOC_CHUNKS)
               .set(CATALOG_DOCUMENTS.HEAD_HASH,   EX_DOC_HEAD)
               .set(CATALOG_DOCUMENTS.INDEXED_AT,  EX_DOC_IDXAT)
               .set(F_DOC_META,   EX_DOC_META)
               .set(CATALOG_DOCUMENTS.SOURCE_MTIME, EX_DOC_SMTIME)
               .set(CATALOG_DOCUMENTS.ALIAS_OF,  EX_DOC_ALIAS)
               .set(CATALOG_DOCUMENTS.SOURCE_URI,    EX_DOC_URI)
               .set(CATALOG_DOCUMENTS.BIB_YEAR,   EX_DOC_BIBY)
               .set(CATALOG_DOCUMENTS.BIB_AUTHORS,   EX_DOC_BIAU)
               .set(CATALOG_DOCUMENTS.BIB_VENUE,   EX_DOC_BIVE)
               .set(CATALOG_DOCUMENTS.BIB_CITATION_COUNT,   EX_DOC_BICC)
               .set(CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID,   EX_DOC_BIS2)
               .set(CATALOG_DOCUMENTS.BIB_OPENALEX_ID,   EX_DOC_BIOA)
               .set(CATALOG_DOCUMENTS.BIB_DOI,  EX_DOC_BIDOI)
               .set(CATALOG_DOCUMENTS.BIB_ENRICHED_AT,   EX_DOC_BIAT)
               // nexus-mqd6t: explicit un-tombstone (see the javadoc above).
               // TOMBSTONE-EXEMPT (nexus-mqd6t): the ONE sanctioned resurrection --
               // deliberately unconditional, no WHERE guard applies to an ON
               // CONFLICT DO UPDATE arm. See TombstoneFilterGateTest.TOMBSTONE_EXEMPT.
               .set(CATALOG_DOCUMENTS.DELETED_AT, (java.time.OffsetDateTime) null)
               .execute();
            return null;
        }));
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
    /**
     * RDR-096 source_uri derivation at the REGISTER boundary (nexus-78n33
     * critique Critical 2): {@code file://<abspath>} from {@code file_path}
     * when the caller sent no {@code source_uri}. Mirrors the Python
     * {@code Catalog._normalize_source_uri} — which only ran on the
     * local-SQLite path, so every service-mode registration from the
     * dominant {@code nx index repo} pipeline arrived with {@code ''} and
     * silently bypassed the catalog-016 unique-index backstop. Deriving
     * HERE makes the identity rule live beside the index that enforces it,
     * for every wire client.
     *
     * <p>Relative {@code file_path} anchors on the OWNER's {@code repo_root}
     * (the nexus-3e4s contamination fix) via pure LEXICAL normalization —
     * the server must never resolve against its own filesystem/CWD. A
     * relative path with no known repo_root stays shapeless ({@code ''}),
     * exactly like the Python legacy-entry rule (those rows keep the
     * owner-scoped file_path idempotency, unguarded — the honest residual).
     */
    public static String deriveSourceUri(String sourceUri, String filePath, String repoRoot) {
        if (!sourceUri.isEmpty() || filePath.isEmpty()) return sourceUri;
        java.nio.file.Path p = java.nio.file.Paths.get(filePath);
        if (!p.isAbsolute()) {
            if (repoRoot == null || repoRoot.isEmpty()) return "";
            java.nio.file.Path root = java.nio.file.Paths.get(repoRoot);
            if (!root.isAbsolute()) return "";
            p = root.resolve(filePath);
        }
        return "file://" + p.normalize();
    }

    /**
     * nexus-e7cys: {@link #deriveSourceUri(String, String, String)} plus the
     * nexus-3e4s cross-project containment guard, restored engine-side.
     *
     * <p>The local arm ({@code catalog.py}, deleted at RDR-158 P4) REJECTED an
     * explicit {@code file://} {@code source_uri} whose path did not resolve
     * inside a {@code "repo"} owner's {@code repo_root} — the signature of a
     * ~6,500-row contamination class where one project's owner accumulated
     * rows whose source_uri lived in a DIFFERENT project's working tree. The
     * port to this class kept the RECOMBINATION leg (relative {@code
     * file_path} anchored on {@code repoRoot}, which is inherently
     * containment-safe by construction) but dropped the REJECTION leg: an
     * explicit {@code source_uri} was returned verbatim, unchecked.
     *
     * <p>This overload restores the rejection leg with ONE deliberate
     * departure from the local arm: it is LEXICAL, never REALPATH. The local
     * guard resolved symlinks on both sides (tolerating e.g. macOS's
     * {@code /private/var} vs {@code /var}) because it ran on the SAME
     * filesystem the paths described. This server does not — a
     * {@code file://} URI names a path on the CALLING client's filesystem,
     * and resolving it against the server's own filesystem would be
     * resolving against the wrong machine entirely (or, worse, silently
     * "succeeding" against a same-named path that happens to exist on the
     * server host by coincidence). A normalize()-then-{@code startsWith()}
     * prefix check is the honest engine-side equivalent; the local arm's
     * symlink tolerance is deliberately NOT restored here.
     *
     * <p>Scope, matching the local arm exactly: only {@code "repo"} owners
     * with a non-empty {@code repoRoot} enforce the check; {@code "curator"}
     * owners and pre-existing repo owners with no {@code repoRoot} pass
     * through. Only {@code file://} URIs carry a filesystem identity to
     * compare — {@code chroma://}, {@code https://}, etc. pass through
     * unchanged. {@code allowCrossProject} is the wire form of the local
     * arm's {@code NEXUS_CATALOG_ALLOW_CROSS_PROJECT=1} escape hatch (see
     * {@code src/nexus/catalog/types.py:143}): the CLIENT reads its own
     * environment and populates this field, since the server has no access
     * to the client's environment.
     */
    public static String deriveSourceUri(String sourceUri, String filePath, String repoRoot,
                                          String ownerType, boolean allowCrossProject) {
        String derived = deriveSourceUri(sourceUri, filePath, repoRoot);
        if (!sourceUri.isEmpty()) {
            // derived == sourceUri whenever sourceUri was non-empty (the 3-arg
            // method's own early return) — this is exactly the explicit-uri
            // (rejection-leg) case; the recombination leg never reaches here.
            checkCrossProjectContainment(derived, ownerType, repoRoot, allowCrossProject);
        }
        return derived;
    }

    /** Thrown by {@link #checkCrossProjectContainment}; maps to 400 like {@link DanglingEndpointException}. */
    public static final class CrossProjectSourceUriException extends IllegalArgumentException {
        private static final long serialVersionUID = 1L;

        CrossProjectSourceUriException(String message) {
            super(message);
        }
    }

    private static void checkCrossProjectContainment(
            String sourceUri, String ownerType, String repoRoot, boolean allowCrossProject) {
        if (sourceUri == null || sourceUri.isEmpty()) return;
        if (!"repo".equals(ownerType)) return;
        if (repoRoot == null || repoRoot.isEmpty()) return;

        java.net.URI uri;
        try {
            uri = java.net.URI.create(sourceUri);
        } catch (IllegalArgumentException e) {
            // Malformed URI is a different bug class; not this guard's job.
            return;
        }
        if (!"file".equals(uri.getScheme())) return;
        String rawPath = uri.getPath();
        if (rawPath == null || rawPath.isEmpty()) return;

        java.nio.file.Path filePath = java.nio.file.Paths.get(rawPath).normalize();
        java.nio.file.Path root = java.nio.file.Paths.get(repoRoot).normalize();
        if (filePath.startsWith(root)) return;

        if (allowCrossProject) {
            // nexus-e7cys: the override was actually EXERCISED (the check would
            // have failed without it) — log so the bypass leaves an audit trail,
            // matching the local arm's "never the right answer for normal
            // indexing" framing of the env-var escape hatch.
            log.warn("event=cross_project_source_uri_override_used repo_root={} source_uri={}",
                      repoRoot, sourceUri);
            return;
        }
        throw new CrossProjectSourceUriException(
            "cross-project source_uri rejected (nexus-3e4s/nexus-e7cys): "
            + "owner repo_root=" + repoRoot + " but source_uri=" + sourceUri
            + " normalizes to " + filePath + ", which is outside the owner's "
            + "repo_root. This is the signature of the contamination bug "
            + "class. Pass allow_cross_project=true to bypass for emergency "
            + "recovery.");
    }

    private String ownerRepoRoot(org.jooq.DSLContext ctx, String tenant, String ownerPrefix) {
        String root = ctx.select(CATALOG_OWNERS.REPO_ROOT).from(CATALOG_OWNERS)
                         .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)
                                .and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(ownerPrefix)))
                         .fetchOne(CATALOG_OWNERS.REPO_ROOT);
        return root != null ? root : "";
    }

    /**
     * Ensure the owner row at {@code ownerPrefix} exists, and return its
     * {@code repo_root} (nexus-0ehwe arbiter class).
     *
     * <p>Replaces a blind {@code INSERT ... ON CONFLICT (tenant_id, tumbler_prefix) DO
     * NOTHING} at both register sites. That insert named only the ADDRESS key, so it was
     * exposed to both of {@code catalog_owners}' other unique keys: an owner whose
     * {@code (name, owner_type)} already lives at a different prefix raised a raw 23505
     * that no arm caught (nexus-jq53b), and it is a documented TOCTOU guard that fires
     * under exactly the concurrency the register path sees (nexus-z3ssg).
     *
     * <p>Costs no extra round trip. The SELECT that decides whether the row exists is
     * the SAME one both callers already made for {@code repo_root} via
     * {@link #ownerRepoRoot}; {@code repo_root} is {@code NOT NULL DEFAULT ''}, so a
     * {@code null} here unambiguously means "no row at this address" rather than "row
     * with an empty root".
     */
    private static String ensureOwnerRow(DSLContext ctx, String tenant, String ownerPrefix,
                                         String ownerName, String ownerType) {
        String repoRoot = ctx.select(CATALOG_OWNERS.REPO_ROOT).from(CATALOG_OWNERS)
                             .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)
                                    .and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(ownerPrefix)))
                             .fetchOne(CATALOG_OWNERS.REPO_ROOT);
        if (repoRoot != null) return repoRoot;

        // Creating the row: its identity must not already belong to another address.
        guardOwnerIdentity(ctx, tenant, ownerPrefix, ownerName, ownerType, null);
        ctx.insertInto(CATALOG_OWNERS, CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX,
                       CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH,
                       CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT,
                       CATALOG_OWNERS.HEAD_HASH, CATALOG_OWNERS.NEXT_SEQ)
           .values(tenant, ownerPrefix, ownerName, ownerType, null, null, "", null, 0L)
           .onConflict(CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX)
           .doNothing()
           .execute();
        return "";
    }

    /**
     * Outcome of a {@code registerDocument}/{@code registerDocumentMany} call
     * (nexus-vfef0): the assigned/matched {@code tumbler} plus whether THIS
     * call minted it. {@code created=false} covers every leg that hands back
     * a PRE-EXISTING row — an idempotency-leg hit (source_uri or file_path)
     * or an ON-CONFLICT race-loser (a concurrent first-put race winner beat
     * this call's INSERT) — so a caller's compensating rollback on a later
     * failure (e.g. the client's {@code rollback_minted_catalog_entry}) can
     * tell "I minted this, it's safe to delete on failure" from "this row
     * predates my call, deleting it would destroy someone else's document."
     * Before this record existed, the wire response carried no such signal
     * at all and every leg looked identical to a caller.
     *
     * <p><b>Batch intra-alias caveat</b> (see {@link #registerDocumentManyWithOutcome}):
     * when a batch call contains two entries that alias to the SAME newly-
     * minted tumbler (same source_uri, intra-batch dedup), BOTH entries'
     * outcomes report {@code created=true} for that one shared row — this
     * is correct for BATCH-LEVEL accounting only ("this call's INSERT
     * activity created N rows"). It is NOT safe to treat as a per-index
     * mint signal for INDEPENDENT per-entry delete-on-failure compensation:
     * a future caller that rolled back entry A's tumbler on A's own
     * failure, and separately rolled back entry B's (identical) tumbler on
     * B's own failure, would issue two deletes against one row — the
     * second a no-op at best, but the pattern is a double-delete bug
     * waiting to happen if the delete ever gains side effects keyed on
     * "did a row exist to delete." A per-entry rollback consumer must
     * dedup by tumbler across the batch first.
     */
    public record RegisterOutcome(String tumbler, boolean created) {}

    /**
     * Register a document, returning only the assigned/matched tumbler.
     *
     * <p>Thin wrapper over {@link #registerDocumentWithOutcome} for callers
     * that only need the tumbler (the majority of call sites). See that
     * method for the {@code created} signal (nexus-vfef0).
     */
    public String registerDocument(String tenant, String ownerPrefix, Map<String, Object> fields) {
        return registerDocumentWithOutcome(tenant, ownerPrefix, fields).tumbler();
    }

    /**
     * Register a document, returning both the assigned/matched tumbler and
     * whether THIS call minted it. See {@link RegisterOutcome} for the exact
     * {@code created} contract (nexus-vfef0).
     */
    public RegisterOutcome registerDocumentWithOutcome(String tenant, String ownerPrefix, Map<String, Object> fields) {
        if (TenantConstants.isWildcard(tenant)) {
            throw new IllegalArgumentException(
                "tenant '*' is a reserved sentinel and cannot own catalog entries");
        }
        return UniqueRaceRetry.run("registerDocument", OWNER_NON_ARBITRATED, () ->
        tenantScope.withTenant(tenant, ctx -> {
            // Ensure owner row exists. Resolve-first (nexus-0ehwe arbiter class): the
            // blind DO-NOTHING upsert this replaces named only the address key and was
            // exposed to catalog_owners' other two. Same round-trip count — this SELECT
            // is the repo_root lookup that used to happen a few lines below.
            String ownerType = s(fields, "owner_type", "repo");
            String repoRoot = ensureOwnerRow(ctx, tenant, ownerPrefix,
                s(fields, "owner_name", ownerPrefix),
                ownerType);

            // Idempotency check BEFORE claiming a sequence number — avoids permanent seq gaps
            // on re-registration of existing documents.
            // Idempotency check: only match LIVE (non-tombstoned) docs.
            // A tombstoned source_uri re-registration allocates a NEW tumbler;
            // the trash entry is left untouched (users can restore or purge it separately).
            // nexus-e7cys: cross-project containment guard on an explicit source_uri;
            // allow_cross_project is the wire form of the client's
            // NEXUS_CATALOG_ALLOW_CROSS_PROJECT env-var escape hatch.
            String srcUri = deriveSourceUri(
                s(fields, "source_uri", ""),
                s(fields, "file_path", ""),
                repoRoot, ownerType, bool(fields, "allow_cross_project", false));
            if (!srcUri.isEmpty()) {
                var existing = ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
                                  .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                                         .and(CATALOG_DOCUMENTS.SOURCE_URI.eq(srcUri))
                                         .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                                  .fetchOne();
                if (existing != null) return new RegisterOutcome(existing.value1(), false);
            }
            String filePath = s(fields, "file_path", "");
            if (!filePath.isEmpty()) {
                var existing = ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
                                  .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                                         .and(CATALOG_DOCUMENTS.FILE_PATH.eq(filePath))
                                         .and(CATALOG_DOCUMENTS.TUMBLER.startsWith(ownerPrefix + "."))
                                         .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                                  .fetchOne();
                if (existing != null) return new RegisterOutcome(existing.value1(), false);
            }

            // No existing document — atomically claim the next sequence number
            long seq = ctx.select(CATALOG_OWNERS.NEXT_SEQ).from(CATALOG_OWNERS)
                          .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(ownerPrefix)))
                          .forUpdate()
                          .fetchOne(CATALOG_OWNERS.NEXT_SEQ);

            // nexus-0ehwe: floor past a drifted counter instead of colliding.
            long claimed = claimNextSeq(ctx, tenant, ownerPrefix, seq);

            String tumbler = ownerPrefix + "." + claimed;

            // Insert document
            String metaJson = jsonOrNull(fields.get("meta"));
            int inserted = ctx.insertInto(CATALOG_DOCUMENTS,
                    CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.YEAR,
                    CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.CORPUS, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT,
                    CATALOG_DOCUMENTS.HEAD_HASH, CATALOG_DOCUMENTS.INDEXED_AT, F_DOC_META, CATALOG_DOCUMENTS.SOURCE_MTIME, CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.SOURCE_URI,
                    CATALOG_DOCUMENTS.BIB_YEAR, CATALOG_DOCUMENTS.BIB_AUTHORS, CATALOG_DOCUMENTS.BIB_VENUE, CATALOG_DOCUMENTS.BIB_CITATION_COUNT,
                    CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID, CATALOG_DOCUMENTS.BIB_OPENALEX_ID, CATALOG_DOCUMENTS.BIB_DOI, CATALOG_DOCUMENTS.BIB_ENRICHED_AT)
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
                       srcUri,
                       ni(i(fields,"bib_year"), 0),
                       nne(s(fields,"bib_authors", "")),
                       nne(s(fields,"bib_venue", "")),
                       ni(i(fields,"bib_citation_count"), 0),
                       nne(s(fields,"bib_semantic_scholar_id", "")),
                       nne(s(fields,"bib_openalex_id", "")),
                       nne(s(fields,"bib_doi", "")),
                       nne(s(fields,"bib_enriched_at", "")))
               // nexus-78n33: TOCTOU backstop. Two concurrent registrations of
               // the same NEW source_uri can both pass the idempotency SELECT
               // above (READ COMMITTED); the partial unique index
               // ux_catalog_documents_live_source_uri (catalog-016) makes the
               // loser's INSERT a no-op instead of a duplicate. The loser then
               // returns the winner's tumbler below. The already-claimed seq
               // is burned — an accepted, race-only gap (the common
               // re-registration path still returns before claiming).
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.SOURCE_URI)
               .where(CATALOG_DOCUMENTS.DELETED_AT.isNull()
                      .and(CATALOG_DOCUMENTS.SOURCE_URI.ne("")))
               .doNothing()
               .execute();
            // (jOOQ execute() returns the affected rowcount; DO NOTHING on a
            // lost race yields 0. Only non-empty source_uri rows can engage
            // the partial-index arbiter, so inserted==0 implies srcUri set.)
            if (inserted == 0) {
                // Our INSERT was the conflict loser — hand back the winner.
                // Review (78n33): if the winner has ALSO vanished by now (a
                // concurrent tombstone in the microsecond gap between our
                // no-op'd INSERT and this SELECT), returning our own tumbler
                // would fabricate a document that was never persisted — the
                // silent-fallback class. FAIL LOUD instead; the caller
                // retries into a now-clear registration.
                var winner = ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
                                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                                       .and(CATALOG_DOCUMENTS.SOURCE_URI.eq(srcUri))
                                       .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                                .fetchOne();
                if (winner == null) {
                    throw new IllegalStateException(
                        "registerDocument race lost for source_uri=" + srcUri
                        + " but no live winner exists (concurrent tombstone?) — "
                        + "refusing to return a never-persisted tumbler; retry");
                }
                log.info("event=register_document_race_lost tenant={} source_uri={} winner={}",
                    tenant, srcUri, winner.value1());
                return new RegisterOutcome(winner.value1(), false);
            }
            return new RegisterOutcome(tumbler, true);
        }));
    }

    /**
     * Batch variant of {@link #registerDocument} (nexus-9dvqy, duoak.11 sink #2).
     *
     * <p>Registers N documents under one owner in a SINGLE transaction, returning
     * their tumblers in INPUT ORDER. The per-doc {@code registerDocument} path pays
     * one {@code SELECT next_seq FOR UPDATE} owner-row lock per call — 2,133 serial
     * WAN round-trips on a --force index (333s / 16% of the duoak.11 gate wall), all
     * queued on the SAME owner lock (so client concurrency cannot help). This claims
     * the whole contiguous sequence block under ONE lock acquisition and inserts all
     * new rows in one multi-row INSERT.
     *
     * <p>Idempotency matches the single-doc path exactly: a doc whose {@code source_uri}
     * (then {@code file_path}) already maps to a LIVE (non-tombstoned) document returns
     * that existing tumbler and does NOT consume a sequence number — so a re-registration
     * leaves no seq gap. Only genuinely-new docs draw from the claimed block.
     *
     * <p>Caller (the HTTP handler) caps the batch under the PostgreSQL 32767 bind-param
     * ceiling; with ~24 columns per row the safe cap is 1000 rows (24000 params).
     *
     * <p>Thin wrapper over {@link #registerDocumentManyWithOutcome} for
     * callers that only need tumblers. See that method for the per-entry
     * {@code created} signal (nexus-vfef0).
     */
    public java.util.List<String> registerDocumentMany(
            String tenant, String ownerPrefix, java.util.List<java.util.Map<String, Object>> docs) {
        return registerDocumentManyWithOutcome(tenant, ownerPrefix, docs).stream()
            .map(RegisterOutcome::tumbler)
            .toList();
    }

    /**
     * Batch-register, returning per-entry {@link RegisterOutcome} (tumbler +
     * {@code created}) aligned 1:1 with {@code docs} (nexus-vfef0). An entry
     * resolved via the pre-batch idempotency lookup, or an ON-CONFLICT
     * cross-transaction race loser, reports {@code created=false} — the row
     * predates this call (or was minted by a concurrent winner). An entry
     * whose own INSERT landed reports {@code true}. An intra-batch alias of
     * another NEW entry in the SAME call (two docs sharing one source_uri)
     * MIRRORS whichever outcome its first occurrence resolved to — usually
     * {@code true} (the shared row's INSERT landed), {@code false} only if
     * that first occurrence itself lost a cross-transaction race. See the
     * batch intra-alias caveat on {@link RegisterOutcome} before treating
     * this per-entry flag as a per-index rollback signal: two aliased
     * entries reporting {@code created=true} for the SAME tumbler is
     * correct for batch-level accounting but is not two independent mints.
     */
    public java.util.List<RegisterOutcome> registerDocumentManyWithOutcome(
            String tenant, String ownerPrefix, java.util.List<java.util.Map<String, Object>> docs) {
        if (TenantConstants.isWildcard(tenant)) {
            throw new IllegalArgumentException(
                "tenant '*' is a reserved sentinel and cannot own catalog entries");
        }
        if (docs == null || docs.isEmpty()) {
            return java.util.List.of();
        }
        return UniqueRaceRetry.run("registerDocumentMany", OWNER_NON_ARBITRATED, () ->
        tenantScope.withTenant(tenant, ctx -> {
            // nexus-oub13: per-step wall timing — the live duoak.11 re-gate
            // measured ~38s/page client-side with no way to tell which step
            // (or whether the server at all) was the sink. One structured
            // line per page; negligible overhead at page granularity.
            long tStart = System.nanoTime();
            // Ensure owner row exists — once. Resolve-first, same as the single-doc
            // path (nexus-0ehwe arbiter class); this is also the repo_root lookup.
            // One owner_type for the whole batch — the owner row is ensured ONCE
            // from docs[0], so a per-doc re-read below could silently diverge from
            // the owner's registered type if a caller ever sent mixed values
            // (review 2026-08-01 finding on nexus-e7cys's batch guard).
            String batchOwnerType = s(docs.get(0), "owner_type", "repo");
            String repoRoot = ensureOwnerRow(ctx, tenant, ownerPrefix,
                s(docs.get(0), "owner_name", ownerPrefix),
                batchOwnerType);
            long tOwner = System.nanoTime();

            // Batch idempotency: fetch existing LIVE docs for every source_uri and
            // file_path in the batch in ONE query per direction, then join locally.
            // nexus-78n33: source_uri is DERIVED (RDR-096, file_path anchored on
            // the owner's repo_root) when the caller sent none — see
            // deriveSourceUri. The derived value is used for lookup, insert,
            // and race patching alike.
            String[] uriOf = new String[docs.size()];
            java.util.List<String> srcUris = new java.util.ArrayList<>();
            java.util.List<String> filePaths = new java.util.ArrayList<>();
            for (int i = 0; i < docs.size(); i++) {
                var d = docs.get(i);
                // nexus-e7cys: per-doc containment guard, same as the single-doc path.
                uriOf[i] = deriveSourceUri(
                    s(d, "source_uri", ""), s(d, "file_path", ""), repoRoot,
                    batchOwnerType, bool(d, "allow_cross_project", false));
                if (!uriOf[i].isEmpty()) srcUris.add(uriOf[i]);
                String fp = s(d, "file_path", "");
                if (!fp.isEmpty()) filePaths.add(fp);
            }
            java.util.Map<String, String> tumblerBySrcUri = new java.util.HashMap<>();
            if (!srcUris.isEmpty()) {
                ctx.select(CATALOG_DOCUMENTS.SOURCE_URI, CATALOG_DOCUMENTS.TUMBLER)
                   .from(CATALOG_DOCUMENTS)
                   .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                          .and(CATALOG_DOCUMENTS.SOURCE_URI.in(srcUris))
                          .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                   .fetch()
                   .forEach(r -> tumblerBySrcUri.putIfAbsent(r.value1(), r.value2()));
            }
            java.util.Map<String, String> tumblerByFilePath = new java.util.HashMap<>();
            if (!filePaths.isEmpty()) {
                ctx.select(CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.TUMBLER)
                   .from(CATALOG_DOCUMENTS)
                   .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                          .and(CATALOG_DOCUMENTS.FILE_PATH.in(filePaths))
                          .and(CATALOG_DOCUMENTS.TUMBLER.startsWith(ownerPrefix + "."))
                          .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                   .fetch()
                   .forEach(r -> tumblerByFilePath.putIfAbsent(r.value1(), r.value2()));
            }

            long tLookup = System.nanoTime();

            // Resolve each doc to an existing tumbler or mark it NEW (input order).
            // (In-memory joins; timed into resolve_ms so the warm-rerun case
            // doesn't mislabel this as seq-update time — review M-1.)
            String[] out = new String[docs.size()];
            // nexus-vfef0: per-entry created flag, aligned with out[]. Default
            // false covers the pre-batch idempotency hit (existing != null,
            // just below) with no further action needed.
            boolean[] created = new boolean[docs.size()];
            java.util.List<Integer> newIdx = new java.util.ArrayList<>();
            // nexus-78n33: intra-batch source_uri dedup. Two NEW docs in the
            // SAME batch sharing a source_uri would otherwise both be
            // assigned tumblers, and the catalog-016 partial unique index
            // would drop the second row's INSERT (ON CONFLICT DO NOTHING) —
            // returning a tumbler that points at no document. First
            // occurrence wins; later duplicates alias to its tumbler.
            java.util.Map<String, Integer> firstNewBySrcUri = new java.util.HashMap<>();
            java.util.Map<Integer, Integer> aliasOfIdx = new java.util.HashMap<>();
            for (int i = 0; i < docs.size(); i++) {
                var d = docs.get(i);
                String su = uriOf[i];
                String fp = s(d, "file_path", "");
                String existing = null;
                if (!su.isEmpty()) existing = tumblerBySrcUri.get(su);
                if (existing == null && !fp.isEmpty()) existing = tumblerByFilePath.get(fp);
                if (existing != null) {
                    out[i] = existing;
                } else if (!su.isEmpty() && firstNewBySrcUri.containsKey(su)) {
                    aliasOfIdx.put(i, firstNewBySrcUri.get(su));
                } else {
                    if (!su.isEmpty()) firstNewBySrcUri.put(su, i);
                    newIdx.add(i);
                }
            }

            long tResolve = System.nanoTime();
            long tClaim = tResolve;
            long tInsert = tResolve;
            if (!newIdx.isEmpty()) {
                // Claim a contiguous seq block under ONE FOR UPDATE lock.
                long seq = ctx.select(CATALOG_OWNERS.NEXT_SEQ).from(CATALOG_OWNERS)
                              .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(ownerPrefix)))
                              .forUpdate()
                              .fetchOne(CATALOG_OWNERS.NEXT_SEQ);
                tClaim = System.nanoTime();
                // nexus-0ehwe, SECOND claim site: floor the cursor past a
                // drifted counter exactly as the single-doc path does, or a
                // batch into a wedged owner collides on (tenant_id, tumbler)
                // with no ON CONFLICT arm and 409s the whole batch.
                long cursor = Math.max(seq, highestChildSeq(ctx, tenant, ownerPrefix));
                if (cursor != seq) {
                    log.warn("event=next_seq_drift_healed_batch tenant={} owner={} next_seq={} floored_to={}",
                        tenant, ownerPrefix, seq, cursor);
                }

                var insert = ctx.insertInto(CATALOG_DOCUMENTS,
                        CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.YEAR,
                        CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.CORPUS, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT,
                        CATALOG_DOCUMENTS.HEAD_HASH, CATALOG_DOCUMENTS.INDEXED_AT, F_DOC_META, CATALOG_DOCUMENTS.SOURCE_MTIME, CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.SOURCE_URI,
                        CATALOG_DOCUMENTS.BIB_YEAR, CATALOG_DOCUMENTS.BIB_AUTHORS, CATALOG_DOCUMENTS.BIB_VENUE, CATALOG_DOCUMENTS.BIB_CITATION_COUNT,
                        CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID, CATALOG_DOCUMENTS.BIB_OPENALEX_ID, CATALOG_DOCUMENTS.BIB_DOI, CATALOG_DOCUMENTS.BIB_ENRICHED_AT);
                for (int idx : newIdx) {
                    var fields = docs.get(idx);
                    cursor += 1;
                    String tumbler = ownerPrefix + "." + cursor;
                    out[idx] = tumbler;
                    // Tentative: this row's own INSERT is about to run. Only
                    // non-empty source_uri rows can lose the cross-transaction
                    // race below (see the TOCTOU backstop comment ahead), so
                    // this is already final truth for every empty-source_uri
                    // NEW row.
                    created[idx] = true;
                    String metaJson = jsonOrNull(fields.get("meta"));
                    insert = insert.values(tenant, tumbler,
                            s(fields, "title", ""),
                            nne(s(fields, "author", null)),
                            ni(i(fields, "year"), 0),
                            nne(s(fields, "content_type", "")),
                            nne(s(fields, "file_path", "")),
                            nne(s(fields, "corpus", "")),
                            nne(s(fields, "physical_collection", "")),
                            ni(i(fields, "chunk_count"), 0),
                            nne(s(fields, "head_hash", "")),
                            nne(s(fields, "indexed_at", "")),
                            jsonbVal(metaJson),
                            nd(dbl(fields, "source_mtime")),
                            nne(s(fields, "alias_of", "")),
                            uriOf[idx],
                            ni(i(fields, "bib_year"), 0),
                            nne(s(fields, "bib_authors", "")),
                            nne(s(fields, "bib_venue", "")),
                            ni(i(fields, "bib_citation_count"), 0),
                            nne(s(fields, "bib_semantic_scholar_id", "")),
                            nne(s(fields, "bib_openalex_id", "")),
                            nne(s(fields, "bib_doi", "")),
                            nne(s(fields, "bib_enriched_at", "")));
                }
                // nexus-78n33: same TOCTOU backstop as the single-doc path —
                // a row whose source_uri was registered by a CONCURRENT
                // transaction between our lookup and this INSERT becomes a
                // no-op against the catalog-016 partial unique index instead
                // of a duplicate. Detected below by rowcount; the affected
                // out[] slots are patched to the winners' tumblers.
                int inserted = insert
                    .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.SOURCE_URI)
                    .where(CATALOG_DOCUMENTS.DELETED_AT.isNull()
                           .and(CATALOG_DOCUMENTS.SOURCE_URI.ne("")))
                    .doNothing()
                    .execute();
                tInsert = System.nanoTime();

                // Advance next_seq by exactly the number of new docs claimed.
                // (Race-dropped rows still consume their seq — an accepted,
                // race-only gap; the block was claimed under FOR UPDATE.)
                ctx.update(CATALOG_OWNERS)
                   .set(CATALOG_OWNERS.NEXT_SEQ, cursor)
                   .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(ownerPrefix)))
                   .execute();

                if (inserted < newIdx.size()) {
                    // At least one row lost the cross-transaction race: its
                    // assigned tumbler points at no document. Re-resolve every
                    // uri-bearing NEW row; winners overwrite (a non-conflicted
                    // row re-resolves to its own fresh tumbler — harmless).
                    log.info("event=register_many_race_lost tenant={} owner={} dropped={}",
                        tenant, ownerPrefix, newIdx.size() - inserted);
                    java.util.Map<String, String> winners = new java.util.HashMap<>();
                    if (!firstNewBySrcUri.isEmpty()) {
                        ctx.select(CATALOG_DOCUMENTS.SOURCE_URI, CATALOG_DOCUMENTS.TUMBLER)
                           .from(CATALOG_DOCUMENTS)
                           .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                                  .and(CATALOG_DOCUMENTS.SOURCE_URI.in(firstNewBySrcUri.keySet()))
                                  .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                           .fetch()
                           .forEach(r -> winners.put(r.value1(), r.value2()));
                    }
                    for (var e : firstNewBySrcUri.entrySet()) {
                        String winner = winners.get(e.getKey());
                        if (winner == null) {
                            // Our own inserted rows are visible to this
                            // same-transaction SELECT, so a missing uri means
                            // BOTH our row was conflict-dropped AND the winner
                            // has since been tombstoned. Returning the
                            // pre-assigned (never-persisted) tumbler would be
                            // the silent-fallback class — FAIL LOUD (review
                            // 78n33); the batch retries into a clear state.
                            throw new IllegalStateException(
                                "registerDocumentMany race lost for source_uri=" + e.getKey()
                                + " but no live winner exists (concurrent tombstone?) — "
                                + "refusing to return a never-persisted tumbler; retry");
                        }
                        int idx = e.getValue();
                        // nexus-vfef0: out[idx] still holds THIS call's
                        // pre-assigned tumbler at this point. If the re-
                        // resolved winner differs, our own INSERT lost the
                        // cross-transaction race — the row belongs to a
                        // concurrent caller, not us.
                        if (!winner.equals(out[idx])) {
                            created[idx] = false;
                        }
                        out[idx] = winner;
                    }
                }
            }
            // Fill intra-batch aliases from their winning row's final tumbler
            // and created flag (nexus-vfef0): an alias never ran its own
            // INSERT, so its created status mirrors whichever outcome the
            // first occurrence in this batch resolved to.
            for (var e : aliasOfIdx.entrySet()) {
                out[e.getKey()] = out[e.getValue()];
                created[e.getKey()] = created[e.getValue()];
            }
            long tEnd = System.nanoTime();
            log.info("event=register_many_timing tenant={} owner={} docs={} new={} "
                    + "owner_upsert_ms={} lookup_ms={} resolve_ms={} claim_ms={} "
                    + "insert_ms={} seq_update_ms={} total_ms={}",
                tenant, ownerPrefix, docs.size(), newIdx.size(),
                (tOwner - tStart) / 1_000_000,
                (tLookup - tOwner) / 1_000_000,
                (tResolve - tLookup) / 1_000_000,
                (tClaim - tResolve) / 1_000_000,
                (tInsert - tClaim) / 1_000_000,
                (tEnd - tInsert) / 1_000_000,
                (tEnd - tStart) / 1_000_000);

            var result = new java.util.ArrayList<RegisterOutcome>(docs.size());
            for (int i = 0; i < docs.size(); i++) {
                result.add(new RegisterOutcome(out[i], created[i]));
            }
            return result;
        }));
    }

    /** Fetch a document by tumbler. Returns null if not found. */
    public Map<String, Object> getDocument(String tenant, String tumbler) {
        return getDocument(tenant, tumbler, false);
    }

    /**
     * Maximum {@code alias_of} hops before {@link #resolveAliasTarget} gives up
     * (nexus-ekaxn; matches the local {@code resolve_alias(max_hops=16)} the
     * service client declared and never implemented).
     */
    private static final int MAX_ALIAS_HOPS = 16;

    /**
     * Fetch a document by tumbler, optionally following the {@code alias_of}
     * chain to its canonical target (nexus-ekaxn).
     *
     * <p>{@code followAlias=false} is the DEFAULT and is byte-for-byte the
     * pre-fix behaviour, so a client that sends no {@code follow_alias} param
     * sees exactly what it saw before.
     *
     * @param followAlias when true, hop {@code alias_of} up to
     *                    {@value #MAX_ALIAS_HOPS} times, stopping early on a
     *                    cycle or a pointer that resolves to nothing.
     */
    public Map<String, Object> getDocument(String tenant, String tumbler, boolean followAlias) {
        return tenantScope.withTenant(tenant, ctx -> {
            String target = followAlias ? resolveAliasTarget(ctx, tenant, tumbler) : tumbler;
            var r = ctx.select(documentFields())
                       .from(CATALOG_DOCUMENTS)
                       .where(CATALOG_DOCUMENTS.TUMBLER.eq(target).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                       .fetchOne();
            return r != null ? docRowFromRecord(r.intoMap()) : null;
        });
    }

    /**
     * Highest child sequence actually in use under *ownerPrefix*, INCLUDING
     * tombstoned rows (nexus-0ehwe).
     *
     * <p>Tombstones matter: the only unique key on {@code catalog_documents} is
     * {@code (tenant_id, tumbler)}, and it does NOT exclude soft-deleted rows —
     * unlike the partial {@code source_uri} index. So a tumbler belonging to a
     * deleted document is still taken, and an allocator that ignores tombstones
     * will hand it out again and collide.
     *
     * <p>Returns 0 when the owner has no numeric children, which makes the
     * caller's {@code max(next_seq, high_water) + 1} degrade to plain
     * {@code next_seq + 1}.
     */
    private static long highestChildSeq(DSLContext ctx, String tenant, String ownerPrefix) {
        // TOMBSTONE-EXEMPT (nexus-mqd6t): tumbler allocator -- the tumbler PK
        // does not exclude tombstones, and filtering would re-issue an
        // already-taken child sequence number to a NEW document. See
        // TombstoneFilterGateTest.TOMBSTONE_EXEMPT.
        // tumbler is "<prefix>.<n>"; the child segment starts one char past the dot.
        int childStart = ownerPrefix.length() + 2;
        Long max = ctx.select(DSL.field(
                    "COALESCE(MAX(CAST(substring(tumbler FROM {0}) AS BIGINT)), 0)",
                    Long.class, DSL.val(childStart)))
               .from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                      .and(CATALOG_DOCUMENTS.TUMBLER.like(ownerPrefix + ".%"))
                      // digits only — a deeper address like "1.2.3" is not a child seq
                      .and(DSL.condition("substring(tumbler FROM {0}) ~ '^[0-9]+$'",
                                         DSL.val(childStart))))
               .fetchOne(0, Long.class);
        return max == null ? 0L : max;
    }

    /**
     * Claim the next tumbler sequence for *ownerPrefix*, self-healing past a
     * drifted counter (nexus-0ehwe items 1 + 2).
     *
     * <p>THE WEDGE THIS REMOVES. {@code registerDocument} used to claim
     * {@code next_seq} and insert; the INSERT's only ON CONFLICT arbiter is
     * {@code (tenant_id, source_uri)}, so a {@code (tenant_id, tumbler)}
     * collision had no arm and escaped as a bare {@code 409 integrity
     * constraint violation}. The {@code next_seq} increment shared the failing
     * transaction and rolled back WITH it, so the allocator never advanced:
     * one drifted owner was a PERMANENT, TOTAL outage for that owner, and retry
     * could never clear it (nexus-pbawi fixed one owner by hand).
     *
     * <p>Rather than catch the collision and retry, this prevents it: the claim
     * is {@code max(next_seq, highest_child_seq) + 1}, so a counter that has
     * fallen behind its own children is floored on the FIRST attempt. Monotonic
     * by construction — it can only raise, and can never re-issue a tumbler —
     * which is the same guarantee {@code EX_OWN_SEQ_GREATEST} gives the ETL
     * import path.
     *
     * <p>Runs inside the caller's {@code SELECT ... FOR UPDATE} on the owner
     * row, so two concurrent registrations cannot both claim the same value.
     */
    private static long claimNextSeq(DSLContext ctx, String tenant, String ownerPrefix, long nextSeq) {
        long highWater = highestChildSeq(ctx, tenant, ownerPrefix);
        long claim = Math.max(nextSeq, highWater) + 1;
        if (claim != nextSeq + 1) {
            log.warn("event=next_seq_drift_healed tenant={} owner={} next_seq={} high_water={} claimed={}",
                tenant, ownerPrefix, nextSeq, highWater, claim);
        }
        ctx.update(CATALOG_OWNERS)
           .set(CATALOG_OWNERS.NEXT_SEQ, claim)
           .where(CATALOG_OWNERS.TENANT_ID.eq(tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(ownerPrefix)))
           .execute();
        return claim;
    }

    /**
     * One-shot sweep: floor {@code next_seq} for EVERY owner in *tenant* to at least its
     * own high-water mark (nexus-0ehwe item 5).
     *
     * <p><strong>Why this exists on top of the claim-time self-heal.</strong>
     * {@link #claimNextSeq} floors a drifted owner the moment it is next WRITTEN to — but
     * an owner that is never registered into again stays drifted forever, invisibly. The
     * doctor check ({@code _check_next_seq_drift} in {@code src/nexus/health.py}) can only
     * ever REPORT that; nothing converged it. This sweep is the converge verb: same floor
     * primitive ({@code max(next_seq, high_water)}, monotonic, tombstone-inclusive,
     * matching {@code EX_OWN_SEQ_GREATEST}'s guarantee on the ETL path), applied to every
     * owner in one pass, reporting exactly which owners were actually below their
     * high-water mark — so the blast radius of a drift incident is KNOWN rather than
     * guessed (nexus-pbawi's owner 1.12 was found only because an operator happened to
     * suspect it).
     *
     * <p><strong>Concurrency.</strong> The owner list is read in one transaction, then each
     * owner is probed and floored in its OWN short transaction under {@code SELECT ... FOR
     * UPDATE} on that owner's row (the same locking discipline as the registration claim
     * path), so the lock never accumulates across the sweep and a live
     * {@code registerDocument} blocked on that row waits out one single-owner probe+floor,
     * not the remainder of the sweep. The floor
     * itself is the atomic {@code UPDATE ... SET next_seq = GREATEST(next_seq, ?) WHERE
     * next_seq < ?}: PostgreSQL evaluates both against the row's value AT UPDATE TIME, so a
     * concurrent {@link #claimNextSeq} that has already advanced {@code next_seq} past the
     * computed high-water mark makes this a no-op rather than a regression — monotonic,
     * can-only-raise. Atomicity ACROSS owners is deliberately not provided: each owner's
     * floor is independently idempotent, and a sweep interrupted midway simply leaves the
     * owners it already reached healed.
     *
     * @return {@code {"checked": int, "healed": int, "owners": [{"tumbler_prefix",
     *         "next_seq", "high_water", "floored_to"}, ...]}} — {@code owners} lists ONLY
     *         the owners that were actually drifted (and successfully floored);
     *         {@code checked} counts owners actually examined (one deleted between the
     *         list and its probe is skipped and not counted).
     *
     * <p>nexus-cw262 audit: this sweep queries {@code CATALOG_OWNERS.TUMBLER_PREFIX}
     * directly (not via {@link #listOwners(String)} / {@link #ownersByType}), so it is
     * unaffected by those methods' new default deactivated-owner exclusion and
     * deliberately continues to cover EVERY owner regardless of {@code
     * deactivated_at}. next_seq drift-floor repair is orthogonal to owner
     * liveness/visibility — a deactivated owner can still receive late-arriving
     * writes via an explicit tumbler_prefix (e.g. ETL replay), and floor convergence
     * is monotonic/idempotent either way, so excluding deactivated owners here would
     * only reintroduce the exact invisible-drift failure mode this sweep exists to
     * close, for no offsetting benefit.
     */
    public Map<String, Object> sweepNextSeqDrift(String tenant) {
        List<String> prefixes = tenantScope.withTenant(tenant, ctx ->
            ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX)
               .from(CATALOG_OWNERS)
               .where(CATALOG_OWNERS.TENANT_ID.eq(tenant))
               .fetch(CATALOG_OWNERS.TUMBLER_PREFIX));

        int checked = 0;
        List<Map<String, Object>> healed = new ArrayList<>();
        for (String prefix : prefixes) {
            Map<String, Object> h = tenantScope.withTenant(tenant,
                ctx -> sweepOneOwner(ctx, tenant, prefix));
            if (h == null) continue;   // deleted between the list and its probe — not examined
            checked++;
            if (!h.isEmpty()) healed.add(h);
        }

        log.info("event=next_seq_drift_sweep_summary tenant={} checked={} healed={}",
            tenant, checked, healed.size());
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("checked", checked);
        result.put("healed", healed.size());
        result.put("owners", healed);
        return result;
    }

    /**
     * Probe ONE owner and floor it if drifted, inside the caller's (short) transaction.
     * Returns {@code null} if the owner vanished since it was listed, an empty map if it
     * was examined and healthy (or lost the floor race to a concurrent claim — no longer
     * drifted either way), or the healed-owner report row.
     */
    private static Map<String, Object> sweepOneOwner(DSLContext ctx, String tenant, String prefix) {
        // FOR UPDATE before computing high water — the same locking discipline as
        // registerDocument's claim path, so a concurrent registration cannot land a new
        // child between the high-water scan and the floor and leave the report stale.
        Long beforeSeq = ctx.select(CATALOG_OWNERS.NEXT_SEQ).from(CATALOG_OWNERS)
                             .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)
                                    .and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix)))
                             .forUpdate()
                             .fetchOne(CATALOG_OWNERS.NEXT_SEQ);
        if (beforeSeq == null) return null;

        long highWater = highestChildSeq(ctx, tenant, prefix);
        if (highWater <= beforeSeq) return Map.of();   // healthy: not drifted

        int updated = ctx.update(CATALOG_OWNERS)
            .set(CATALOG_OWNERS.NEXT_SEQ, DSL.greatest(CATALOG_OWNERS.NEXT_SEQ, DSL.val(highWater)))
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)
                   .and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix))
                   .and(CATALOG_OWNERS.NEXT_SEQ.lt(highWater)))
            .execute();
        if (updated == 0) return Map.of();   // lost the race to a concurrent claim

        log.warn("event=next_seq_drift_healed_sweep tenant={} owner={} next_seq={} high_water={} floored_to={}",
            tenant, prefix, beforeSeq, highWater, highWater);
        Map<String, Object> h = new LinkedHashMap<>();
        h.put("tumbler_prefix", prefix);
        h.put("next_seq", beforeSeq);
        h.put("high_water", highWater);
        h.put("floored_to", highWater);
        return h;
    }

    /**
     * Walk {@code alias_of} from *tumbler* to the canonical document tumbler
     * (nexus-ekaxn). Bounded at {@value #MAX_ALIAS_HOPS} hops and cycle-safe
     * (a visited set, so A→B→A terminates on the row it started from rather
     * than spinning).
     *
     * <p>Fail-SOFT by construction, and deliberately so: a pointer that names a
     * missing or tombstoned row resolves to the LAST live row on the chain, not
     * to null. Returning null would turn "this alias is stale" into "this
     * document does not exist" for every caller — a strictly worse answer than
     * the alias row itself, which is what the pre-fix behaviour already gave.
     *
     * @return the canonical tumbler, or *tumbler* itself when it is not an alias
     */
    private static String resolveAliasTarget(DSLContext ctx, String tenant, String tumbler) {
        String current = tumbler;
        Set<String> seen = new LinkedHashSet<>();
        seen.add(current);
        for (int hop = 0; hop < MAX_ALIAS_HOPS; hop++) {
            String next = ctx.select(CATALOG_DOCUMENTS.ALIAS_OF)
                             .from(CATALOG_DOCUMENTS)
                             .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                                    .and(CATALOG_DOCUMENTS.TUMBLER.eq(current))
                                    .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                             .fetchOne(CATALOG_DOCUMENTS.ALIAS_OF);
            if (next == null || next.isBlank()) return current;   // not an alias: the common exit
            if (!seen.add(next)) {
                // A cycle is corrupt data, not a routine shape.
                log.warn("event=alias_chain_cycle tenant={} start={} stopped_at={} repeated={}",
                    tenant, tumbler, current, next);
                return current;
            }
            // A pointer at a row that is gone (or tombstoned) stops the walk on
            // the last row that actually exists.
            boolean liveTarget = ctx.fetchExists(
                ctx.selectOne().from(CATALOG_DOCUMENTS)
                   .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                          .and(CATALOG_DOCUMENTS.TUMBLER.eq(next))
                          .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())));
            if (!liveTarget) {
                // critique C2: the fail-soft substitution is defensible, SILENCE
                // about it is not — this is a broken pointer in the graph and the
                // caller is about to receive a DIFFERENT row than it addressed.
                log.warn("event=alias_target_not_live tenant={} start={} resolved_to={} broken_pointer={}",
                    tenant, tumbler, current, next);
                return current;
            }
            current = next;
        }
        log.warn("event=alias_chain_hop_limit tenant={} start={} stopped_at={} max_hops={}",
            tenant, tumbler, current, MAX_ALIAS_HOPS);
        return current;
    }

    /**
     * Resolve a 4-segment chunk address to its document + chunk metadata
     * (nexus-gc2ze). Mirrors the local {@code Catalog._DocumentOps.resolve_chunk}
     * contract (catalog_docs.py): chunks are implicit addresses — the catalog
     * tracks document-level rows only, and chunk sub-addresses are resolved on
     * demand from the document's {@code chunk_count}. This is a pure lookup +
     * range-check over an existing document row: it delegates entirely to
     * {@link #getDocument}, so there is no new SQL to audit here.
     *
     * <p>{@code chunkCount} of 0 (or absent) means the count is not yet known
     * — the bounds check is skipped in that case, mirroring the local
     * Python's {@code if entry.chunk_count and chunk_idx >= entry.chunk_count}.
     *
     * <p><b>Staleness invariant (nexus-ojazb).</b> This bounds check trusts
     * {@code documents.chunk_count} as an up-to-date mirror of the manifest
     * ({@code catalog_document_chunks}) row count. That is true for every
     * PRODUCTION manifest writer: {@link #writeManifestRows} (REPLACE, used by
     * {@code writeManifest}/{@code writeManifestMany}), {@link
     * #appendManifestChunks}, and {@link #purgeManifest} each fold {@code
     * documents.chunk_count} in the SAME transaction as their manifest
     * delete/insert (nexus-b6enc F5, nexus-e4gel), so a manifest written
     * through any of those three has no read-after-write staleness window —
     * pinned by {@code resolveChunk_writtenThroughNormalManifestPath_resolvesLastChunk}.
     * {@link #buildUpdateDocumentQuery} (the {@code /update} endpoint)
     * independently re-derives the count from the manifest via {@link
     * #reDerivedChunkCount} whenever the caller does not name {@code
     * chunk_count} explicitly, so a plain document update cannot desync it
     * either.
     *
     * <p><b>Residual exposure.</b> The ETL import legs — {@link #importChunk}
     * and {@link #importChunksBatch} (the {@code POST /v1/catalog/import/chunk}
     * route) — insert/upsert manifest rows directly and do NOT fold {@code
     * chunk_count}; a document whose manifest was populated ONLY through that
     * leg (with no explicit {@code chunk_count} on registration/update and no
     * follow-up {@link #resyncChunkCount}) can have a stale (typically 0,
     * skipping the bounds check) count relative to the true manifest size.
     * As of this writing that route has no caller anywhere in {@code
     * src/nexus/} — it exists for a since-retired SQLite-era migration chain
     * (RDR-158 P4) — so the window is real but currently unexercised in
     * production. A future ETL/migration caller of {@code import/chunk} MUST
     * call {@link #resyncChunkCount} (or set an explicit {@code chunk_count})
     * once its import completes, or reintroduce the same fold this method's
     * three production writers already carry.
     *
     * @param tenant      tenant identifier
     * @param docTumbler  the document tumbler (chunk segment already stripped
     *                    by the caller — {@link dev.nexus.service.http.CatalogHandler})
     * @param chunkIndex  the chunk's position within the document
     * @return {@code {document_tumbler, chunk_index, physical_collection,
     *         title, content_type}} or {@code null} if the document is
     *         missing or {@code chunkIndex} is out of range
     */
    public Map<String, Object> resolveChunk(String tenant, String docTumbler, int chunkIndex) {
        var doc = getDocument(tenant, docTumbler);
        if (doc == null) return null;
        Object rawCount = doc.get("chunk_count");
        long chunkCount = rawCount instanceof Number ? ((Number) rawCount).longValue() : 0L;
        if (chunkCount > 0 && chunkIndex >= chunkCount) return null;
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("document_tumbler",    docTumbler);
        m.put("chunk_index",         chunkIndex);
        m.put("physical_collection", doc.getOrDefault("physical_collection", ""));
        m.put("title",               doc.getOrDefault("title", ""));
        m.put("content_type",        doc.getOrDefault("content_type", ""));
        return m;
    }

    /**
     * Update mutable document fields. Only non-null fields in the map are updated.
     * Refuses to update tombstoned documents (returns 0).
     * Silently strips {@code deleted_at} from the input map — callers must use
     * {@code document_trash} / {@code document_restore} to manage the tombstone column.
     */
    /**
     * Settable columns for {@link #updateDocument} (wave review, SQL audit CRITICAL):
     * request JSON keys become SET targets via {@code DSL.field(DSL.name(...))}, so
     * WITHOUT this whitelist any body key was an arbitrary-column write from
     * {@code POST /v1/catalog/update} — including {@code tenant_id} (re-homing a
     * document across tenants) and lifecycle columns like {@code created_at}.
     * The set mirrors the local {@code Catalog.update} mutable surface
     * ({@link #documentFields()} minus the identity/lifecycle columns:
     * tumbler, tenant_id, deleted_at, created_at). Unknown keys fail loud with
     * {@code IllegalArgumentException} → 400, never a silent skip.
     */
    private static final Set<String> UPDATABLE_DOC_COLUMNS = Set.of(
        "title", "author", "year", "content_type", "file_path", "corpus",
        "physical_collection", "chunk_count", "head_hash", "indexed_at",
        "meta", "metadata", "source_mtime", "alias_of", "source_uri",
        "bib_year", "bib_authors", "bib_venue", "bib_citation_count",
        "bib_semantic_scholar_id", "bib_openalex_id", "bib_doi", "bib_enriched_at");

    public int updateDocument(String tenant, String tumbler, Map<String, Object> fields) {
        if (fields.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            Query query = buildUpdateDocumentQuery(ctx, tenant, tumbler, fields);
            return query == null ? 0 : query.execute();
        });
    }

    /**
     * Batch-update mutable document fields for N documents in ONE round trip
     * (nexus-xedhp, duoak.11 follow-up to the register_many fix above).
     *
     * <p>{@code writer.update()} per changed doc was the WAN-round-trip sink:
     * a HEAD bump (any new git commit) flips every indexed doc's stored
     * {@code head_hash} to "changed", forcing the indexer's catalog hook
     * through one serial {@code POST /update} per file — 175.5s / 1718 files
     * (~102ms/file) on this repo's own shakeout. Each entry in *updates*
     * carries the same shape as {@link #updateDocument}'s {@code fields} map
     * plus a {@code "tumbler"} key identifying the row.
     *
     * <p>Executed as ONE {@code ctx.batch(...)} — i.e. one JDBC
     * {@code Statement.addBatch()}/{@code executeBatch()} round trip to
     * Postgres, not N sequential {@code execute()} calls. This is standard
     * JDBC batching, the same mechanism used for heterogeneous-shape batch
     * writes throughout the JDBC ecosystem; jOOQ's {@code batch(Query...)}
     * groups identical-SQL queries into one {@code PreparedStatement} batch
     * and any remaining distinct-SQL queries into one {@code Statement}
     * batch, flushing everything via a single {@code executeBatch()} — never
     * a per-query round trip. (An attempt at a single hand-built
     * multi-row {@code UPDATE ... FROM (VALUES ...)} statement was tried
     * here first and reverted: jOOQ's typed {@code DSL.values(RowN...)} API
     * requires uniform per-column types across all rows, which this
     * whitelist's heterogeneous optional fields can't satisfy without
     * fighting Postgres's NULL-parameter type-inference rules; the raw-SQL
     * {@code DSL.table("(VALUES {0})", ...)} template compiles but its
     * `.field(...)` accessors don't resolve column metadata at all — this
     * specific failure (a {@code NullPointerException} on the returned
     * field) was reproduced against the real Postgres testcontainer suite
     * below. The tests below verify functional correctness, per-entry
     * failure isolation, and idempotency — NOT round-trip count or
     * statement count; the "one JDBC round trip" claim for {@code
     * ctx.batch(...)} on heterogeneous SQL is drawn from jOOQ's documented
     * {@code batch(Query...)} behavior, not independently measured here.)
     *
     * <p>Per-doc failure isolation mirrors {@code register_many}: a single
     * malformed entry (missing tumbler, non-updatable column) is excluded
     * from the batch and marked {@code -1} in the result rather than
     * aborting the whole call — the caller's existing per-file try/except
     * ghost-class-isolation contract depends on partial-batch survivability.
     *
     * @return per-index update counts aligned 1:1 with the input list:
     *         {@code 1} updated, {@code 0} not found/tombstoned/no-op,
     *         {@code -1} malformed entry (build-time rejection, never sent).
     */
    public List<Integer> updateDocumentsMany(String tenant, List<Map<String, Object>> updates) {
        if (updates.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx -> {
            Integer[] resultSlots = new Integer[updates.size()];
            var queries = new ArrayList<Query>();
            var queryIndexes = new ArrayList<Integer>();  // index into `updates` for each entry in `queries`

            // nexus-ekaxn: ONE alias-resolution query for the whole batch (see
            // batchAliasTargets) — never one per document.
            var batchTumblers = new ArrayList<String>(updates.size());
            for (var upd : updates) {
                if (upd.get("tumbler") instanceof String t && !t.isBlank()) batchTumblers.add(t);
            }
            Map<String, String> aliasTargets = batchAliasTargets(ctx, tenant, batchTumblers);

            for (int i = 0; i < updates.size(); i++) {
                var upd = updates.get(i);
                Object tumblerObj = upd.get("tumbler");
                if (!(tumblerObj instanceof String tumbler) || tumbler.isBlank()) {
                    resultSlots[i] = -1;
                    continue;
                }
                Map<String, Object> fields = new LinkedHashMap<>(upd);
                fields.remove("tumbler");
                Query query;
                try {
                    query = buildUpdateDocumentQuery(ctx, tenant, tumbler, fields, aliasTargets);
                } catch (IllegalArgumentException e) {
                    resultSlots[i] = -1;
                    continue;
                }
                if (query == null) {
                    resultSlots[i] = 0;
                    continue;
                }
                queryIndexes.add(i);
                queries.add(query);
            }
            if (!queries.isEmpty()) {
                int[] batchResults = ctx.batch(queries).execute();
                for (int q = 0; q < queryIndexes.size(); q++) {
                    resultSlots[queryIndexes.get(q)] = batchResults[q];
                }
            }
            return java.util.Arrays.asList(resultSlots);
        });
    }

    /**
     * Shared SET-clause builder for {@link #updateDocument} and
     * {@link #updateDocumentsMany} — same column whitelist, same
     * {@code deleted_at}-strip, same {@code meta} jsonb-merge semantics.
     * Returns {@code null} when *fields* yields no settable column (mirrors
     * {@code updateDocument}'s 0-row short-circuit).
     */
    /**
     * The row an update ADDRESSED at *tumbler* must actually write (nexus-ekaxn).
     *
     * <p>An update addressed to an ALIAS must land on the CANONICAL target, not
     * on the alias row. Only the engine can fix this half — the client cannot
     * make {@code WHERE tumbler = ?} mean anything else.
     *
     * <p>CARVE-OUT: a write that SETS {@code alias_of} is a write about the
     * pointer itself, so it must NOT hop. Without it, re-pointing an existing
     * alias would rewrite its current canonical target's {@code alias_of}
     * instead — turning a pointer edit into silent corruption of the row it
     * points at.
     *
     * @param prefetched batch-resolved targets from
     *                   {@link #batchAliasTargets}, or null to resolve
     *                   single-doc (one extra SELECT)
     */
    private static String aliasTarget(DSLContext ctx, String tenant, String tumbler,
                                      Map<String, Object> fields, Map<String, String> prefetched) {
        if (fields.containsKey("alias_of")) return tumbler;
        if (prefetched != null) return prefetched.getOrDefault(tumbler, tumbler);
        return resolveAliasTarget(ctx, tenant, tumbler);
    }

    /**
     * Resolve alias targets for a WHOLE batch in ONE query (nexus-ekaxn meets
     * nexus-xedhp).
     *
     * <p>Load-bearing for {@link #updateDocumentsMany}: a per-entry
     * {@link #resolveAliasTarget} would add one SELECT per document, which is
     * precisely the N-round-trip cost that method exists to remove (175.5s /
     * 1718 files on this repo's own shakeout). One {@code WHERE tumbler IN (?)}
     * covers the batch; only the RARE genuinely-aliased rows then pay a chain
     * walk. Tumblers absent from the result (unknown or tombstoned) map to
     * themselves, so the UPDATE matches 0 rows — the same answer as before.
     */
    private static Map<String, String> batchAliasTargets(
        DSLContext ctx, String tenant, List<String> tumblers
    ) {
        Map<String, String> out = new LinkedHashMap<>();
        if (tumblers.isEmpty()) return out;
        var rows = ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.ALIAS_OF)
                      .from(CATALOG_DOCUMENTS)
                      .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                             .and(CATALOG_DOCUMENTS.TUMBLER.in(tumblers))
                             .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                      .fetch();
        for (var r : rows) {
            String alias = r.value2();
            out.put(r.value1(), (alias == null || alias.isBlank())
                ? r.value1()
                : resolveAliasTarget(ctx, tenant, r.value1()));
        }
        return out;
    }

    private Query buildUpdateDocumentQuery(
        DSLContext ctx, String tenant, String tumbler, Map<String, Object> fields
    ) {
        return buildUpdateDocumentQuery(ctx, tenant, tumbler, fields, null);
    }

    // TOMBSTONE-FILTER-WIDEN (nexus-mqd6t): the ctx.update(CATALOG_DOCUMENTS)
    // initiator below and its .where(...DELETED_AT.isNull()...) guard are
    // different Java statements -- the SET clause is built across a loop over
    // *fields*. ONE query, split across statements; see
    // TombstoneFilterGateTest.WIDEN.
    private Query buildUpdateDocumentQuery(
        DSLContext ctx, String tenant, String tumbler, Map<String, Object> fields,
        Map<String, String> prefetchedAliasTargets
    ) {
        String target = aliasTarget(ctx, tenant, tumbler, fields, prefetchedAliasTargets);
        for (String key : fields.keySet()) {
            // deleted_at keeps its documented silent-strip contract (callers must use
            // trash/restore); every OTHER unknown key is a caller error — fail loud.
            if (!"deleted_at".equals(key) && !UPDATABLE_DOC_COLUMNS.contains(key)) {
                throw new IllegalArgumentException(
                    "updateDocument: column not updatable: '" + key
                    + "' (allowed: " + UPDATABLE_DOC_COLUMNS + ")");
            }
        }
        var step = ctx.update(CATALOG_DOCUMENTS);
        UpdateSetMoreStep<?> more = null;
        for (var e : fields.entrySet()) {
            if (e.getValue() == null) continue;
            // Strip deleted_at — must not be settable via updateDocument
            if ("deleted_at".equals(e.getKey())) continue;
            // metadata is a jsonb column: callers pass it as an object (or JSON
            // string) under "meta"/"metadata". A bare set() of a Map fails with
            // "LinkedHashMap is not supported in dialect POSTGRES"; JSON-encode and
            // bind as jsonb, mirroring upsertDocument (RDR-168 nexus-njrcn.7).
            // nexus-ke45f: MERGE, not replace — local Catalog.update() does
            // dict.update (add/overwrite keys, never remove), and every
            // writer.update(meta=...) caller (enrich write-back, catalog
            // hook, dt stamp, remediation) is written against that
            // contract; the bare SET silently dropped pre-existing keys
            // in service mode. jsonb_concat == the || operator.
            if ("meta".equals(e.getKey()) || "metadata".equals(e.getKey())) {
                Field<String> merged = DSL.function("jsonb_concat", String.class,
                    DSL.coalesce(F_DOC_META, jsonbVal("{}")),
                    jsonbVal(jsonOrNull(e.getValue())));
                more = (more == null)
                    ? step.set(F_DOC_META, merged)
                    : more.set(F_DOC_META, merged);
                continue;
            }
            @SuppressWarnings("unchecked")
            Field<Object> f = (Field<Object>) DSL.field(DSL.name("catalog_documents", e.getKey()));
            more = (more == null) ? step.set(f, e.getValue()) : more.set(f, e.getValue());
        }
        if (more == null) return null;
        // nexus-e4gel / nexus-zq79 F4: when the caller does NOT name chunk_count,
        // re-derive it from the manifest — the manifest IS the count's source of
        // truth (nexus-b6enc F5, already enforced by writeManifestRows /
        // purgeManifest). Without this an /update that follows a manifest change
        // pins a stale count until someone calls /manifest/resync by hand. An
        // EXPLICIT chunk_count still wins (the orphan-backfill paths depend on
        // asserting a count the manifest does not yet carry).
        if (!fields.containsKey("chunk_count")) {
            more = more.set(CATALOG_DOCUMENTS.CHUNK_COUNT, reDerivedChunkCount(ctx, tenant, target));
        }
        // nexus-927mo: /update must refresh indexed_at when it CHANGES head_hash —
        // the same re-index-refresh contract the manifest write paths already
        // enforce via stampIndexedAt (nexus-p5qk8/GH #1397), which this method
        // never shared. Before this, a head_hash-only update (re-index that
        // finds an unchanged chunk set, so no manifest write follows) left
        // indexed_at frozen at the last manifest write or register time, and
        // `nx catalog show` last_indexed never advanced for that class of
        // re-index. A no-op update (same head_hash resubmitted) must NOT stamp —
        // only an actual change advances it — so this compares against the OLD
        // row's head_hash via a single-statement CASE (mirrors
        // reDerivedChunkCount immediately above: no read-then-write race with a
        // concurrent writer, and updateDocumentsMany's ctx.batch(...) inherits it
        // for free since it is folded into the same Query). An explicit
        // caller-supplied indexed_at still wins outright — checked here, before
        // this SET is added, exactly like chunk_count's explicit-wins rule.
        if (!fields.containsKey("indexed_at") && fields.get("head_hash") != null) {
            more = more.set(CATALOG_DOCUMENTS.INDEXED_AT,
                             stampedIndexedAtOnHeadHashChange(String.valueOf(fields.get("head_hash"))));
        }
        // AND deleted_at IS NULL: refuse to update tombstoned documents
        return more.where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                          .and(CATALOG_DOCUMENTS.TUMBLER.eq(target))
                          .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()));
    }

    /**
     * The head_hash-triggered indexed_at refresh used by
     * {@link #buildUpdateDocumentQuery} (nexus-927mo). {@code IS DISTINCT FROM}
     * is the null-safe comparison — a document whose head_hash has never been
     * set (empty string, per {@link #registerDocument}'s default) still counts
     * a first real head_hash as a change.
     */
    private static Field<String> stampedIndexedAtOnHeadHashChange(String newHeadHash) {
        return DSL.when(CATALOG_DOCUMENTS.HEAD_HASH.isDistinctFrom(newHeadHash),
                        DSL.val(java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC).format(INDEXED_AT_FMT)))
                  .otherwise(CATALOG_DOCUMENTS.INDEXED_AT);
    }

    /**
     * Correlated {@code SELECT count(*)} over a document's manifest rows, used
     * as a SET expression so the fold happens inside the same statement (no
     * read-then-write race with a concurrent manifest writer).
     *
     * <p>A-1 constraint: stays unfiltered ONLY because every caller already
     * guards the OUTER update's WHERE with {@code DELETED_AT.isNull()} (see
     * {@link #buildUpdateDocumentQuery}, {@link #writeManifestRows}, {@link
     * #appendManifestChunks}) — filtering this subquery in isolation would
     * silently zero the count instead of failing loud on those guarded
     * D-5/eldyi paths. Do not filter it here.
     */
    // TOMBSTONE-EXEMPT (nexus-mqd6t): see the A-1 constraint above. See
    // TombstoneFilterGateTest.TOMBSTONE_EXEMPT.
    private static Field<Integer> manifestRowCount(DSLContext ctx, String tenant, String tumbler) {
        return DSL.field(ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                                   .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(tumbler))));
    }

    /**
     * The re-derivation used by {@link #buildUpdateDocumentQuery} — the manifest
     * count, EXCEPT that it will not zero a positive stored count against an
     * EMPTY manifest (Hal decision 2026-07-30, the "H2 guard").
     *
     * <p>WHY THE ASYMMETRY. {@code chunk_count > 0} with an empty manifest is not
     * noise — it is the GH #1371 / GH #1397 damage signature, and it is the ONLY
     * discriminator {@code nx catalog reconcile} has: {@code manifest_heal.py}
     * sorts unrebuildable documents into {@code lost} ("chunks LOST — a real
     * gap") versus {@code never_chunked} ("expected: nothing to rebuild") on
     * exactly this field. An unguarded re-derivation lets any INCIDENTAL update
     * — a routine head_hash bump on an unrelated file — silently rewrite a real
     * data-loss event as routine noise, while the underlying content stays
     * unindexed. The document would still be found and rebuilt, but the operator
     * would be told nothing was wrong.
     *
     * <p>WHAT THIS DOES NOT BREAK. The nexus-zq79 F4 contract this implements
     * only ever moves the count UPWARD (a manifest landed rows that
     * documents.chunk_count never learned about); its pin exercises 0 -> 5. The
     * guard is inert in that direction. And zeroing remains fully available on
     * the paths that MEAN it: an explicit {@code chunk_count} in the update wins
     * outright (caller intent), and {@code purgeManifest} folds the count to 0
     * itself. What is refused is only the incidental, unasked-for zeroing.
     *
     * <p>Expressed as a SQL CASE rather than a read-then-decide so the whole
     * thing stays one statement — no extra round trip on the single-doc path,
     * no read-then-write race with a concurrent manifest writer, and
     * {@code updateDocumentsMany} inherits it for free.
     */
    private static Field<Integer> reDerivedChunkCount(DSLContext ctx, String tenant, String tumbler) {
        Field<Integer> derived = manifestRowCount(ctx, tenant, tumbler);
        return DSL.when(derived.eq(0).and(CATALOG_DOCUMENTS.CHUNK_COUNT.gt(0)),
                        CATALOG_DOCUMENTS.CHUNK_COUNT)
                  .otherwise(derived);
    }

    /**
     * Tombstone a document by tumbler (RDR-156 P1.2 soft delete).
     * Sets deleted_at = NOW() (PG server clock, same clock as purge_trash) instead of
     * physically deleting, so fk-001 CASCADE chains (manifest, aspects, highlights, queue)
     * do NOT fire. AND deleted_at IS NULL: idempotent — double-tombstone does not reset
     * the purge age clock.
     * Returns 1 if tombstoned, 0 if not found or already tombstoned.
     */
    // ⚠ TRIPWIRE (nexus-7n553, RDR-164 P4 open gap): deleteDocument /
    // deleteDocumentsMany are SOFT tombstones by design. If you ever add a
    // per-document HARD delete (trash-empty, GC), fk-001 cascades only the
    // four FK children — topic_assignments has NO document-rooted FK (its
    // doc_id is a chunk chash, not a tumbler) and WILL orphan silently. A
    // hard path must explicitly purge that doc's assignments by its
    // manifest chashes (deleteCollection does the collection-scoped
    // equivalent at line ~1917). Pinned by CatalogDocumentCascadeTest.
    // TOMBSTONE-EXEMPT note (nexus-mqd6t): this method's DELETED_AT.isNull()
    // WHERE guard is IDEMPOTENCY (double-tombstone must not reset the purge
    // age clock), a different reason than the read-invisibility concern this
    // gate otherwise enforces -- it passes TombstoneFilterGateTest on its own
    // merits (the guard is present) and is not a TOMBSTONE_EXEMPT table entry.
    public int deleteDocument(String tenant, String tumbler) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(CATALOG_DOCUMENTS)
               .set(CATALOG_DOCUMENTS.DELETED_AT, DSL.currentOffsetDateTime())
               .where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .execute()
        );
    }

    /**
     * Batch-tombstone N documents in ONE round trip (nexus-xedhp follow-up:
     * completes the update_many/register_many/delete_many batch trio).
     *
     * <p>Unlike {@link #updateDocumentsMany}, every row shares the identical
     * {@code SET deleted_at = NOW()} — no per-row heterogeneous values, so a
     * plain {@code WHERE tumbler = ANY(?)} multi-row match is both simplest
     * and a genuine single SQL statement (no VALUES-table / batch-API
     * ambiguity to navigate). {@code RETURNING tumbler} identifies exactly
     * which of the input tumblers were actually tombstoned (already-deleted
     * or non-existent tumblers are silently excluded, same as the single-doc
     * {@link #deleteDocument}'s idempotent 0-return).
     *
     * @return the SET of tumblers that were tombstoned by this call (a
     *         subset of *tumblers*; order not significant — callers map
     *         membership, not position, unlike updateDocumentsMany's
     *         positional contract, since there's only one outcome shape
     *         here: deleted or not).
     *
     * <p>TOMBSTONE-EXEMPT note (nexus-mqd6t): same idempotency rationale as
     * {@link #deleteDocument} — passes on its own merits, not a {@code
     * TombstoneFilterGateTest.TOMBSTONE_EXEMPT} table entry.
     */
    public Set<String> deleteDocumentsMany(String tenant, List<String> tumblers) {
        if (tumblers.isEmpty()) return Set.of();
        return tenantScope.withTenant(tenant, ctx ->
            new java.util.HashSet<>(ctx.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, DSL.currentOffsetDateTime())
                .where(CATALOG_DOCUMENTS.TUMBLER.in(tumblers)
                       .and(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
                       .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                .returning(CATALOG_DOCUMENTS.TUMBLER)
                .fetch(CATALOG_DOCUMENTS.TUMBLER))
        );
    }

    /**
     * Per-dim stranded-chunk count (nexus-3ck2g E3) — the SELECT-only mirror of
     * {@code nexus.purge_trash}'s Step 1-3 DELETE predicate (catalog-003-soft-delete.xml
     * :200-296): a chunk row is stranded iff it has at least one manifest row
     * ({@code catalog_document_chunks}) AND none of its manifest rows belong to a live
     * ({@code deleted_at IS NULL}) document. This is the logical negation of the
     * live_chunks predicate ({@link dev.nexus.service.vectors.PgVectorRepository}'s
     * inlined {@code liveChunksPredicate}), restricted to manifest-backed chunks —
     * manifest-less chunks (RDR-145 MCP/{@code store_put} note chunks) are never
     * stranded by construction (the {@code hasManifest} EXISTS excludes them, same
     * safety contract {@code purge_trash} itself enforces).
     */
    // nexus-msz9i: this method deliberately RETAINS the old two-subquery liveness shape
    // (hasManifest AND NOT hasLiveManifest) that PgVectorRepository#liveChunksPredicate was
    // rewritten away from. That shape makes PostgreSQL build hashed SubPlans which seq-scan
    // the ENTIRE catalog_document_chunks manifest once per call — a cost fixed per query and
    // linear in total manifest size. Acceptable HERE and not worth the churn: this is a
    // diagnostics/reporting count (purge-trash dry-run preview and the stranded-chunk census,
    // ~3 calls per explicit operator-invoked report), not a serving read path, and unlike the
    // predicate it is computing the DEAD set as its result rather than filtering rows by it.
    // If this ever moves onto a hot path — a periodic health poll, a dashboard refresh, a
    // per-request census — port it to the dead-set form first; see
    // PgVectorRepository#liveChunksPredicate and T2 nexus/msz9i-explain-verdict for the
    // plan evidence and the FK-dependent equivalence argument.
    private static long strandedChunkCount(DSLContext ctx, String tenant, DimTables.ChunkTable ch) {
        Condition hasManifest = DSL.exists(
            ctx.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
               .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(ch.tenantId())
                      .and(CHK_CHASH_HEX.eq(ch.chash()))));
        Condition hasLiveManifest = DSL.exists(
            ctx.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
               .join(CATALOG_DOCUMENTS)
                 .on(CATALOG_DOCUMENTS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
                     .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID)))
               .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(ch.tenantId())
                      .and(CHK_CHASH_HEX.eq(ch.chash()))
                      .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())));
        Long count = ctx.selectCount().from(ch.table())
            .where(ch.tenantId().eq(tenant).and(hasManifest).and(hasLiveManifest.not()))
            .fetchOne(0, Long.class);
        return count != null ? count : 0L;
    }

    /**
     * The {@code older_than} argument for {@code nexus.purge_trash(interval)}, in EXACT
     * DAYS (nexus-ff85q).
     *
     * <p>⚠ DO NOT reintroduce {@code YearToSecond.valueOf(Duration.ofDays(n))} here. jOOQ
     * normalises a {@link java.time.Duration} into an interval's year/month/day fields
     * using FIXED 30-day months and 365.25-day years, so {@code Duration.ofDays(30)}
     * renders as {@code '+0-1 +0'} — literally {@code interval '1 mon'} — and
     * {@code Duration.ofDays(365)} renders as {@code '+1-0 +5'} ({@code '1 year 5 days'}).
     * PostgreSQL then evaluates {@code NOW() - interval '1 mon'} with CALENDAR arithmetic,
     * which is 28-31 real days depending on the month, NOT the 30 the caller asked for.
     * That mismatch is the nexus-ff85q production defect: a purge advertised as "older
     * than 30 days" silently applied a different cut point than the dry-run preview did,
     * and skipped every tombstone in the gap (63 previewed, 2 purged).
     *
     * <p>Putting the whole magnitude in the DAY field leaves nothing for jOOQ to
     * normalise: PostgreSQL receives {@code '+0-0 +30 00:00:00'} and {@code NOW() - that}
     * is exactly 30×24h. Pinned by {@code CatalogPurgeTrashPopulationParityTest}.
     */
    private static org.jooq.types.YearToSecond olderThanInterval(int olderThanDays) {
        return new org.jooq.types.YearToSecond(
            new org.jooq.types.YearToMonth(0, 0),
            new org.jooq.types.DayToSecond(olderThanDays));
    }

    /**
     * Aged-tombstone document count (nexus-3ck2g E3) — mirrors {@code nexus.purge_trash}'s
     * Step 4 WHERE ({@code deleted_at IS NOT NULL AND deleted_at <= NOW() - older_than}).
     *
     * <p>The threshold is evaluated SERVER-SIDE, from the identical {@code NOW() - {interval}}
     * expression and the identical {@link #olderThanInterval} argument the function itself
     * receives (nexus-ff85q). It deliberately does NOT compute a Java-clock threshold: that
     * made this count a SECOND, independently-written definition of one population, and the
     * two definitions drifted — by up to a day per month of threshold on the interval units
     * alone, plus whatever app-host/DB-host clock skew exists in a cloud deployment. One
     * population needs one predicate; this is that predicate, in the same dialect and on the
     * same clock as the DELETE it previews.
     */
    private static long agedTombstoneCount(DSLContext ctx, String tenant, int olderThanDays) {
        Field<OffsetDateTime> threshold = DSL.field(
            "now() - {0}", OffsetDateTime.class,
            DSL.val(olderThanInterval(olderThanDays), SQLDataType.INTERVAL));
        // TOMBSTONE-EXEMPT (nexus-mqd6t): this read's whole PURPOSE (nexus-3ck2g E3) is
        // counting the TOMBSTONED population itself (deleted_at IS NOT NULL), mirroring
        // nexus.purge_trash's own Step 4 WHERE -- the inverse of every other CATALOG_DOCUMENTS
        // read this gate polices, which must exclude tombstones. See
        // TombstoneFilterGateTest.TOMBSTONE_EXEMPT's "agedTombstoneCount" entry.
        Long count = ctx.selectCount().from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                   .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())
                   .and(CATALOG_DOCUMENTS.DELETED_AT.le(threshold)))
            .fetchOne(0, Long.class);
        return count != null ? count : 0L;
    }

    /**
     * POST /v1/catalog/purge-trash dry-run preview (nexus-3ck2g E3): count-only,
     * mutates nothing. Mirrors {@code nexus.purge_trash}'s own predicates via SELECT
     * COUNT so a caller can see what a real purge would sweep before authorizing one
     * (reconcile-stale gate pattern — LIVE INVOCATION is Hal-gated on the client side).
     */
    public Map<String, Object> purgeTrashPreview(String tenant, int olderThanDays) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("dry_run", true);
            out.put("documents_purged", agedTombstoneCount(ctx, tenant, olderThanDays));
            out.put("chunks_384_stranded", strandedChunkCount(ctx, tenant, DimTables.CHUNKS.get(384)));
            out.put("chunks_768_stranded", strandedChunkCount(ctx, tenant, DimTables.CHUNKS.get(768)));
            out.put("chunks_1024_stranded", strandedChunkCount(ctx, tenant, DimTables.CHUNKS.get(1024)));
            return out;
        });
    }

    /**
     * POST /v1/catalog/purge-trash execute (nexus-3ck2g E3): invokes the
     * {@code nexus.purge_trash(interval)} stored function (catalog-003-soft-delete.xml)
     * under the request tenant's RLS GUC — {@link TenantScope#withTenant} always stamps
     * {@code nexus.tenant} via {@code set_config(..., true)} before this runs, so the
     * function's own GUC guard (raises on unset/empty tenant) is satisfied by
     * construction; there is no unscoped call path. The per-dim stranded-chunk and
     * aged-tombstone counts are computed FIRST, in the SAME transaction as the
     * function call (same pattern as {@link #manifestBackfill}'s routine invocation) —
     * the closest achievable snapshot of what the mutation is about to sweep, modulo
     * the ordinary read-committed race against a concurrent write in the same instant
     * (no data-safety impact either way; purge_trash's own WHERE is authoritative for
     * what actually gets deleted, these counts are reporting only).
     *
     * <p>PARTIAL PURGES ARE NEVER SILENT (nexus-ff85q): the aged-tombstone count is
     * measured in this same transaction, immediately before the function runs, and is
     * returned as {@code documents_eligible} alongside {@code documents_purged}. Under
     * identical state the two are equal by construction — both now derive from the same
     * {@link #olderThanInterval} argument and the same server clock. A difference means the
     * purge took a strict SUBSET of what it found, which is what production saw (63
     * eligible, 2 purged, reported as success); it is logged at WARN and is visible on the
     * wire so {@code nx catalog purge-trash} can say so instead of printing a bare
     * completion. Not an exception: a concurrent committed tombstone/restore between the
     * two statements is a legitimate (if rare) read-committed cause, and aborting a purge
     * that has ALREADY deleted rows would be worse than reporting the discrepancy.
     *
     * <p>POST-COMMIT VACUUM, ASYNCHRONOUS (nexus-0ys55, made async by nexus-tyxnh):
     * after the transaction above commits (inside {@link TenantScope#withTenant}'s own
     * return path), {@link #applyPostPurgeVacuum} SUBMITS a {@code VACUUM (ANALYZE)}
     * sweep of every table the DELETE above swept — {@code chunks_384/768/1024},
     * {@code catalog_document_chunks} (fk-001 CASCADE), and {@code catalog_documents}
     * itself — to {@link #vacuumExecutor} and returns IMMEDIATELY; it does NOT wait for
     * the vacuum to finish. Deliberately OUTSIDE the {@code withTenant} lambda: VACUUM
     * cannot run inside a transaction block, and by the time this method's caller sees
     * the map, {@code withTenant} has already committed and returned the connection to
     * the pool.
     *
     * <p>WHY ASYNC (nexus-tyxnh, substantive-critique Critical): the production
     * incident that motivated nexus-0ys55 reported chunks_1024 ALONE took 195s to
     * vacuum manually. Running the 5-table sweep synchronously on the HTTP request
     * thread would hold the response open past the Python client's 30s shared httpx
     * timeout — the client sees an apparent failure for a purge that already
     * committed, and a reasonable operator retry risks a SECOND vacuum sequence
     * running concurrently against the same tables during exactly the incident window
     * this bead exists to help with. Submitting the work and returning immediately
     * removes that failure mode entirely: the response always returns promptly, and
     * {@link #vacuumInProgress} (single-flight) makes a concurrent retry report
     * {@code already-running} rather than queueing a second sweep.
     *
     * <p>Gated by {@link #VACUUM_THRESHOLD_ROWS} — see that constant's javadoc — and
     * reported in the envelope as {@code vacuum} (one of {@code "scheduled"} /
     * {@code "already-running"} / {@code "skipped:below-threshold"}) plus
     * {@code skipped_reason} (a human-readable detail string, null only when {@code
     * vacuum == "scheduled"}). Per-table durations and the permission-skip detection
     * ({@link TenantScope#vacuumAnalyze}'s {@code getWarnings()} check) are NOT part
     * of the synchronous response — they cannot be known until the async sweep
     * actually runs — and land instead in the {@code event=purge_trash_vacuum_complete}
     * / {@code event=purge_trash_vacuum_failed} structured log lines the executor
     * writes on completion; that log is the operator's completion signal.
     */
    public Map<String, Object> purgeTrash(String tenant, int olderThanDays) {
        long[] totalRowsAffectedHolder = new long[1];
        Map<String, Object> out = tenantScope.withTenant(tenant, ctx -> {
            long chunks384  = strandedChunkCount(ctx, tenant, DimTables.CHUNKS.get(384));
            long chunks768  = strandedChunkCount(ctx, tenant, DimTables.CHUNKS.get(768));
            long chunks1024 = strandedChunkCount(ctx, tenant, DimTables.CHUNKS.get(1024));
            long eligible   = agedTombstoneCount(ctx, tenant, olderThanDays);

            Long purgedRaw = dev.nexus.service.jooq.nexus.Routines.purgeTrash(
                ctx.configuration(), olderThanInterval(olderThanDays));
            long purged = purgedRaw != null ? purgedRaw : 0L;

            if (purged != eligible) {
                log.warn("event=purge_trash_partial tenant={} older_than_days={} "
                         + "documents_eligible={} documents_purged={}",
                         tenant, olderThanDays, eligible, purged);
            }

            // Best available proxy for "rows this run actually swept": purge_trash's
            // own bigint return is document-count-only (no per-table chunk-sweep counts
            // come back across the SQL-function boundary without changing its
            // signature), but chunks384/768/1024 above are computed in the SAME
            // transaction immediately before the routine call — the identical
            // eligible-vs-purged snapshot pattern this method already uses for
            // documents. Read-committed race caveat applies equally to both.
            totalRowsAffectedHolder[0] = purged + chunks384 + chunks768 + chunks1024;

            Map<String, Object> m = new LinkedHashMap<>();
            m.put("dry_run", false);
            m.put("documents_purged", purged);
            m.put("documents_eligible", eligible);
            m.put("chunks_384_stranded", chunks384);
            m.put("chunks_768_stranded", chunks768);
            m.put("chunks_1024_stranded", chunks1024);
            return m;
        });

        applyPostPurgeVacuum(out, tenant, olderThanDays, totalRowsAffectedHolder[0]);
        return out;
    }

    /**
     * Rows-affected threshold below which {@link #applyPostPurgeVacuum} skips VACUUM
     * entirely (nexus-0ys55). Guards against paying VACUUM's own I/O + lock-acquisition
     * cost (a dedicated connection, five sequential table-level VACUUM statements) on a
     * trivial or near-empty purge cycle, where the resulting dead-tuple bloat is
     * negligible regardless. Chosen well below the production cycles that motivated
     * this bead: the relay that filed it purged 63 documents / swept 285 chunks in one
     * cycle (348 rows), and the incident that bumped it to P1 purged 11,594 documents
     * (~60k dead tuples, 195s to VACUUM chunks_1024 alone) — both clear this threshold
     * by well over an order of magnitude, so every cycle with real cleanup work gets
     * vacuumed; only genuinely tiny/empty runs are skipped.
     */
    private static final long VACUUM_THRESHOLD_ROWS = 10L;

    /**
     * Schema-qualified tables {@code purge_trash} bulk-deletes from, in the order
     * VACUUM is applied (nexus-0ys55). {@code catalog_document_chunks} is included even
     * though the SQL function never names it explicitly — its rows are removed via
     * fk-001's {@code ON DELETE CASCADE} when {@code catalog_documents} rows are
     * deleted (catalog-003-soft-delete.xml Step 4), so it accumulates dead tuples from
     * the SAME purge cycle (production evidence: +18,210 dead tuples on
     * catalog_document_chunks alongside +41,358 on chunks_1024 in the same 11,594-doc
     * purge).
     *
     * <p>LOCKSTEP (nexus-0ys55): must name the SAME five tables as {@link
     * TenantScope#VACUUM_ALLOWED_TABLES} (this is the list {@link
     * TenantScope#vacuumAnalyze} validates any call against) and the {@code
     * grants-003-purge-vacuum-maintain} changeset in {@code grants-nexus-svc.xml}
     * (the MAINTAIN grant that lets {@code nexus_svc} actually vacuum these tables
     * rather than PostgreSQL warning-and-skipping every one of them). {@code
     * TenantScopeVacuumMaintainGrantParityTest} pins the Java-side half of that
     * agreement. Package-private (not private) so that test can read it directly.
     */
    static final List<String> PURGE_VACUUM_TABLES = List.of(
        "nexus.chunks_384",
        "nexus.chunks_768",
        "nexus.chunks_1024",
        "nexus.catalog_document_chunks",
        "nexus.catalog_documents");

    /**
     * Decides whether to SUBMIT the post-commit VACUUM step for {@link #purgeTrash}
     * and mutates {@code out} in place with {@code vacuum} (one of {@code "scheduled"}
     * / {@code "already-running"} / {@code "skipped:below-threshold"}) plus {@code
     * skipped_reason} (nullable String, populated whenever {@code vacuum != "scheduled"}).
     * Returns to the caller IMMEDIATELY in every case — never blocks on the vacuum
     * itself (nexus-tyxnh). Never throws: submission failure (executor rejection) is
     * caught and reported the same as any other skip, never surfaced as a failed HTTP
     * response for an already-committed purge.
     */
    private void applyPostPurgeVacuum(Map<String, Object> out, String tenant, int olderThanDays,
                                       long totalRowsAffected) {
        if (totalRowsAffected < VACUUM_THRESHOLD_ROWS) {
            out.put("vacuum", "skipped:below-threshold");
            out.put("skipped_reason",
                "rows_affected=" + totalRowsAffected + " below threshold=" + VACUUM_THRESHOLD_ROWS);
            return;
        }
        if (!vacuumInProgress.compareAndSet(false, true)) {
            out.put("vacuum", "already-running");
            out.put("skipped_reason",
                "a purge-trash VACUUM is already in progress for this engine instance; "
                + "re-run later rather than retrying now — this call did NOT start a second sweep");
            log.info("event=purge_trash_vacuum_already_running tenant={} older_than_days={} rows_affected={}",
                tenant, olderThanDays, totalRowsAffected);
            return;
        }
        try {
            vacuumExecutor.submit(() -> runPostPurgeVacuum(tenant, olderThanDays, totalRowsAffected));
            out.put("vacuum", "scheduled");
            out.put("skipped_reason", null);
        } catch (RuntimeException e) {
            // Submission itself failed (e.g. executor rejected the task after
            // shutdown) — never leave vacuumInProgress stuck true for a sweep that
            // never actually started.
            vacuumInProgress.set(false);
            log.error("event=purge_trash_vacuum_submit_failed tenant={} older_than_days={} rows_affected={}",
                tenant, olderThanDays, totalRowsAffected, e);
            out.put("vacuum", "skipped:submit-failed");
            out.put("skipped_reason", "vacuum_submit_failed: " + e.getMessage());
        }
    }

    /**
     * The actual VACUUM work, run on {@link #vacuumExecutor} — never on the HTTP
     * request thread (nexus-tyxnh). Per-table durations and the permission-skip
     * detection ({@link TenantScope#vacuumAnalyze}'s {@code getWarnings()} check,
     * surfaced via {@link TenantScope.TableVacuumResult#detail()}) land in the
     * structured log lines below; this is the ONLY place that information is
     * reported — the synchronous HTTP response ({@link #applyPostPurgeVacuum}) never
     * sees it, since it returns before this method runs. Always releases {@link
     * #vacuumInProgress} in a {@code finally}, success or failure, so a single stuck
     * sweep can never wedge every future purge-trash call into {@code
     * already-running} forever.
     */
    private void runPostPurgeVacuum(String tenant, int olderThanDays, long totalRowsAffected) {
        try {
            Map<String, TenantScope.TableVacuumResult> results = tenantScope.vacuumAnalyze(PURGE_VACUUM_TABLES);
            boolean allVacuumed = results.values().stream().allMatch(TenantScope.TableVacuumResult::vacuumed);
            log.info("event=purge_trash_vacuum_complete tenant={} older_than_days={} rows_affected={} "
                    + "all_vacuumed={} results={}",
                tenant, olderThanDays, totalRowsAffected, allVacuumed, results);
        } catch (RuntimeException e) {
            log.error("event=purge_trash_vacuum_failed tenant={} older_than_days={} rows_affected={}",
                tenant, olderThanDays, totalRowsAffected, e);
        } finally {
            vacuumInProgress.set(false);
        }
    }

    /**
     * FTS search over title/author/corpus/file_path using OR'd tsquery.
     * Optionally filter by content_type. Returns up to limit results.
     */
    // TOMBSTONE-FILTER-WIDEN (nexus-mqd6t): the WHERE Condition is assembled
    // into a local variable in an earlier statement (folding the FTS match
    // with DELETED_AT.isNull()) and applied via .where(where) in the
    // ctx.select(...) initiator's own statement below. See
    // TombstoneFilterGateTest.WIDEN.
    public List<Map<String, Object>> searchDocuments(String tenant, String query,
                                                      String contentType, int limit) {
        if (query == null || query.isBlank()) return List.of();
        return tenantScope.withTenant(tenant, ctx -> {
            // nexus-8gue1: third leg matches the catalog-015 separator-
            // normalized segment — 'RDR-021' must find a doc whose only
            // searchable text is the basename 'rdr-021.md' / its path
            // (plainto_tsquery('RDR-021') = 'rdr' & '-021', which the
            // opaque filename lexeme never satisfies). Skipped when the
            // query has no separator: translate() would be a no-op and the
            // leg identical to the plain simple leg.
            //
            // nexus-23wlw census: EVERY leg is folded, because catalog-017
            // folds the STORED column. Both halves are required and a
            // one-sided change is silently wrong in a way tests without an
            // accented query would not catch — the stored lexemes become
            // folded, so an unfolded query stops matching accented input that
            // matched before. Injection safety is unchanged: fold_diacritics
            // and translate wrap the BIND PARAMETER, never the literal.
            boolean hasSeparator = query.chars()
                .anyMatch(c -> c == '/' || c == '.' || c == '-' || c == '_');
            Condition ftsMatch = hasSeparator
                ? DSL.condition(
                    "fts_vector @@ plainto_tsquery('english', nexus.fold_diacritics({0})) "
                    + "OR fts_vector @@ plainto_tsquery('simple', nexus.fold_diacritics({0})) "
                    + "OR fts_vector @@ plainto_tsquery('simple', "
                    + "nexus.fold_diacritics(translate({0}, '/.-_', '    ')))",
                    DSL.val(query))
                : DSL.condition(
                    "fts_vector @@ plainto_tsquery('english', nexus.fold_diacritics({0})) "
                    + "OR fts_vector @@ plainto_tsquery('simple', nexus.fold_diacritics({0}))",
                    DSL.val(query));
            Condition where = ftsMatch.and(CATALOG_DOCUMENTS.DELETED_AT.isNull());
            if (contentType != null && !contentType.isBlank()) {
                where = where.and(CATALOG_DOCUMENTS.CONTENT_TYPE.eq(contentType));
            }
            return ctx.select(documentFields())
                      .from(CATALOG_DOCUMENTS)
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
               .from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.DELETED_AT.isNull())
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit <= 0 ? 200 : limit)
               .offset(offset)
               .fetch()
               .map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Count all documents for this tenant. */
    public long countDocuments(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectCount().from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.DELETED_AT.isNull())
               .fetchOne(0, Long.class)
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
     *
     * <p>TOMBSTONE-EXEMPT note (nexus-mqd6t): this is a migration physical
     * row-count verify, deliberately including tombstones — it counts real
     * rows in the named relation, whatever that relation is, not "documents
     * visible to a reader." Not a {@code TombstoneFilterGateTest.TOMBSTONE_EXEMPT}
     * table entry: the table it selects from is resolved dynamically ({@code
     * DSL.table(DSL.name(...))} from the caller's relation string), so the
     * literal {@code CATALOG_DOCUMENTS} token this gate scans for never
     * appears here — structurally outside the gate's reach, not a suppressed
     * violation.
     */
    public Map<String, Long> relationCounts(String tenant, List<String> relations) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, Long> out = new LinkedHashMap<>();
            for (String rel : relations) {
                if (rel == null || !(VERIFY_RELATIONS.contains(rel) || COUNT_ONLY_RELATIONS.contains(rel))) {
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
            Long count = dev.nexus.service.jooq.nexus.Routines.manifestBackfill(ctx.configuration());
            return count != null ? count : 0L;
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
            long count = ctx.fetchCount(MANIFEST_ORPHANS.call(dim));
            var sample = ctx.selectFrom(MANIFEST_ORPHANS.call(dim))
                             .limit(limit)
                             .fetch()
                             .map(org.jooq.Record::intoMap);
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("count", count);
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
        return tenantScope.withTenant(tenant, ctx ->
            (long) ctx.fetchCount(MANIFEST_ORPHANS.call(dim)));
    }

    private static void requireSupportedDim(int dim) {
        if (!MANIFEST_DIMS.contains(dim)) {
            throw new IllegalArgumentException(
                "unsupported dim " + dim + " — supported values: 384, 768, 1024");
        }
    }

    /**
     * RDR-180 (bead nexus-du2dw): per-table chash width-conformance report for
     * one dim — the engine-route counterpart to the LOCAL-ONLY nexus_diag psql
     * probe ({@code nexus.db.diag_connection}, nexus-y3wuu), for managed/cloud
     * installs with no direct substrate access.
     *
     * <p>Invokes the {@code nexus.chash_conformance_report(dim)} stored
     * function (rdr180-021), which returns one row per covered table
     * ({@code chunks_<dim>}, and {@code catalog_document_chunks} filtered to
     * that dim's model-token collections — same IN-list routing caveat as
     * {@link #manifestOrphanReport}) with {@code total}, {@code
     * non_conformant} (octet_length(chash) &lt;&gt; 32 — the era-safe RDR-180
     * predicate), and {@code sample_chashes} (hex-encoded, capped at 20 by the
     * function itself).
     *
     * <p>SECURITY INVOKER + FORCE RLS: tenant-scoped, NOT the cross-tenant
     * BYPASSRLS view the local install-binary gate reads — see the
     * changeset's header for why that is the correct scoping for a
     * managed-mode tenant's own self-service check.
     *
     * <p>Returns {@code List<Map<String,Object>>} with keys {@code
     * table_name}/{@code total}/{@code non_conformant}/{@code
     * sample_chashes}. {@code dim} must be 384/768/1024 (validated here so an
     * unsupported dim is a clean IllegalArgumentException → 400, not a
     * PL/pgSQL RAISE → 500).
     */
    public List<Map<String, Object>> chashConformanceReport(String tenant, int dim) {
        requireSupportedDim(dim);
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectFrom(CHASH_CONFORMANCE_REPORT.call(dim))
               .fetch()
               .map(org.jooq.Record::intoMap));
    }

    /**
     * nexus-xoimv: {@code limit <= 0} means UNBOUNDED (no LIMIT clause
     * applied beyond this large sentinel) — the absent-from-query-string
     * default every {@code documentsBy*} filter method below preserves, so an
     * existing caller that never sent {@code limit} keeps its pre-xoimv
     * unbounded behaviour. An explicit positive {@code limit} is honored
     * verbatim. {@code offset} is applied unconditionally (0 is the standard
     * no-op). Kept as a real, very large {@code LIMIT} value (rather than
     * omitting the clause) so every filter method can share one
     * {@code .orderBy(TUMBLER).limit(n).offset(o)} shape — the same
     * ternary-into-limit() convention {@link #listDocuments} already uses.
     */
    private static final int NO_LIMIT = Integer.MAX_VALUE;

    /** Documents by physical_collection. {@code limit <= 0} is unbounded (nexus-xoimv). */
    public List<Map<String, Object>> documentsByCollection(
            String tenant, String collection, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(collection)
                   .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit > 0 ? limit : NO_LIMIT)
               .offset(offset)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /**
     * Documents by file_path (exact). {@code limit <= 0} is unbounded
     * (nexus-xoimv). Gained an explicit {@code ORDER BY tumbler} in the same
     * change — previously unordered, so paginating it had no stable cursor.
     */
    public List<Map<String, Object>> documentsByFilePath(
            String tenant, String filePath, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.FILE_PATH.eq(filePath).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit > 0 ? limit : NO_LIMIT)
               .offset(offset)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /**
     * Documents by source_uri (exact). {@code limit <= 0} is unbounded
     * (nexus-xoimv). Gained an explicit {@code ORDER BY tumbler} in the same
     * change — previously unordered, so paginating it had no stable cursor.
     */
    public List<Map<String, Object>> documentsBySourceUri(
            String tenant, String uri, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.SOURCE_URI.eq(uri).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit > 0 ? limit : NO_LIMIT)
               .offset(offset)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Documents by owner tumbler prefix. {@code limit <= 0} is unbounded (nexus-xoimv). */
    public List<Map<String, Object>> documentsByOwner(
            String tenant, String ownerPrefix, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.TUMBLER.like(ownerPrefix + ".%").and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit > 0 ? limit : NO_LIMIT)
               .offset(offset)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /**
     * Documents by owner tumbler prefix AND file_path (exact). GH #1350 Fix B.
     *
     * <p>The combined predicate is the correct behaviour for
     * {@code GET /list?owner=X&file_path=Y}: the owner-only path returns the
     * full owner list, which caused {@code HttpCatalogClient.by_file_path} to
     * mis-attribute a new file to an unrelated doc (silent manifest overwrite).
     *
     * <p>{@code limit <= 0} is unbounded (nexus-xoimv).
     */
    public List<Map<String, Object>> documentsByOwnerAndFilePath(
            String tenant, String ownerPrefix, String filePath, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.TUMBLER.like(ownerPrefix + ".%").and(CATALOG_DOCUMENTS.FILE_PATH.eq(filePath))
                   .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit > 0 ? limit : NO_LIMIT)
               .offset(offset)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Documents by content_type. {@code limit <= 0} is unbounded (nexus-xoimv). */
    public List<Map<String, Object>> documentsByContentType(
            String tenant, String contentType, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.CONTENT_TYPE.eq(contentType).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit > 0 ? limit : NO_LIMIT)
               .offset(offset)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Documents by corpus. {@code limit <= 0} is unbounded (nexus-xoimv). */
    public List<Map<String, Object>> documentsByCorpus(
            String tenant, String corpus, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.CORPUS.eq(corpus).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .limit(limit > 0 ? limit : NO_LIMIT)
               .offset(offset)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /** Descendants: all documents with tumbler starting with prefix + "." */
    public List<Map<String, Object>> descendants(String tenant, String prefix) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.TUMBLER.like(prefix + ".%").and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
               .fetch().map(r -> docRowFromRecord(r.intoMap()))
        );
    }

    /**
     * Update physical_collection for one document.
     *
     * <p>nexus-eldyi: guarded — silent 0-row no-op on a tombstoned target
     * (unlike the manifest writers above, no typed refusal here: this method
     * already returns the real {@code int} rows-affected, so a caller reading
     * 0 gets the honest answer without needing an exception to surface it).
     *
     * <p>nexus-11gh6 rev 2 §3.2 (Hal Q1): does NOT take the sweep gate.
     * This touches ONLY {@code catalog_documents.physical_collection} — it
     * never writes {@code catalog_document_chunks} or any {@code
     * chunks_<dim>} row, so it cannot itself make a chash newly
     * "referenced" in a collection's scope. The stale denormalized stamp
     * on any EXISTING manifest row (nexus-x6kdz: {@code
     * catalog_document_chunks.collection}) is corrected only by a
     * SUBSEQUENT manifest write ({@link #writeManifestRows} / {@link
     * #appendManifestChunks} / the import paths), which DO take the gate.
     * Contrast {@link #renameCollectionTxn}, which bulk-repoints {@code
     * catalog_document_chunks.collection} (and, on its canonical branch,
     * {@code chunks_<dim>.collection}) directly and therefore needs it.
     */
    public int updateDocumentCollection(String tenant, String tumbler, String newCollection) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, newCollection)
               .where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())).execute()
        );
    }

    /** Update physical_collection for many documents. Guarded like the
     *  single-doc form above (nexus-eldyi). See {@link
     *  #updateDocumentCollection}'s javadoc (nexus-11gh6 rev 2 §3.2) for
     *  why this needs no sweep gate either. */
    public int updateDocumentsCollectionBatch(String tenant, List<String> tumblers, String newCollection) {
        if (tumblers.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, newCollection)
               .where(CATALOG_DOCUMENTS.TUMBLER.in(tumblers).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())).execute()
        );
    }

    /** Set alias_of for a document. Guarded — silent 0-row no-op on a tombstoned target (nexus-eldyi; see updateDocumentCollection). */
    public int setAlias(String tenant, String tumbler, String aliasOf) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.ALIAS_OF, nne(aliasOf))
               .where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())).execute()
        );
    }

    /**
     * Look up tumbler by (physical_collection, file_path). Returns null if not found.
     *
     * <p>nexus-h77a2: restores the retired local arm's {@code (file_path = ? OR
     * title = ?)} probe — the engine had narrowed to {@code file_path} only, so
     * a doc registered with {@code title == abs_path} (the aspect worker's
     * {@code _canonicalize_source_path} live path) never resolved.
     *
     * <p><b>wji11 disposition (settled, do not re-litigate):</b> the retired
     * local arm ALSO preferred {@code metadata.doc_id} over the tumbler in its
     * return value. That preference is NOT restored here. nexus-wji11 (Hal,
     * 2026-07-26) settled the tumbler as the ONLY document identity, and this
     * method already returns {@code TUMBLER} alone — under that ruling, that is
     * correct, not a gap. Legacy 16-char {@code doc_id} callers migrate to the
     * tumbler; they do not get a doc_id back from this lookup.
     *
     * <p>Also restores the local arm's {@code LIMIT 1} semantics: {@code
     * fetchOne()} on a duplicate (collection, file_path) throws {@code
     * TooManyRowsException} where the local arm quietly returned one row.
     * {@code orderBy(TUMBLER)} makes the "one row" choice deterministic
     * (lowest tumbler wins) rather than an arbitrary row order.
     */
    public String lookupDocByCollectionAndPath(String tenant, String collection, String filePath) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
                       .where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(collection)
                           .and(CATALOG_DOCUMENTS.FILE_PATH.eq(filePath).or(CATALOG_DOCUMENTS.TITLE.eq(filePath)))
                           .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                       .orderBy(CATALOG_DOCUMENTS.TUMBLER)
                       .limit(1)
                       .fetchOne();
            return r != null ? r.value1() : null;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // LINKS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Upsert a link. Returns {@code true} when the row was newly INSERTed (created),
     * {@code false} when the ON CONFLICT path merged into an existing link — the
     * created-vs-merged signal the local {@code Catalog.link} returns (RDR-168
     * nexus-njrcn.3). The {@code (xmax = 0)} RETURNING predicate is the standard Postgres
     * idiom: a freshly inserted row has {@code xmax = 0}; a row reached via DO UPDATE does not.
     */
    public boolean upsertLink(String tenant, Map<String, Object> lnk) {
        String metaJson = jsonOrNull(lnk.get("metadata"));
        boolean allowDangling = Boolean.TRUE.equals(lnk.get("allow_dangling"))
            || "true".equalsIgnoreCase(String.valueOf(lnk.get("allow_dangling")));
        return tenantScope.withTenant(tenant, ctx -> {
            if (!allowDangling) {
                requireLiveEndpoints(ctx, tenant, s(lnk, "from_tumbler"), s(lnk, "to_tumbler"));
            }
            var rec = ctx.insertInto(CATALOG_LINKS,
                    CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE,
                    CATALOG_LINKS.FROM_SPAN, CATALOG_LINKS.TO_SPAN, CATALOG_LINKS.CREATED_BY, CATALOG_LINKS.CREATED_AT, F_LNK_META)
               .values(DSL.val(tenant),
                       DSL.val(s(lnk,"from_tumbler")), DSL.val(s(lnk,"to_tumbler")), DSL.val(s(lnk,"link_type")),
                       DSL.val(nne(s(lnk,"from_span"))), DSL.val(nne(s(lnk,"to_span"))),
                       DSL.val(nne(s(lnk,"created_by"))), DSL.val(createdAtOrNow(s(lnk,"created_at"))),
                       jsonbVal(metaJson))
               .onConflict(CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE)
               .doUpdate()
               .set(CATALOG_LINKS.FROM_SPAN, EX_LNK_FSPAN)
               .set(CATALOG_LINKS.TO_SPAN, EX_LNK_TSPAN)
               // nexus-s4e1n: created_by is DELIBERATELY NOT SET on the merge
               // path. A second creator of the same edge does not take over the
               // attribution — it is folded into meta['co_discovered_by'] below.
               .set(F_LNK_META,  LNK_META_FOLD)
               // nexus-xtmtf: CATALOG_LINKS (generated) carries a real CatalogLinksRecord
               // shape, unlike the old hand-built Table<?>. .returning(Field...) on a
               // recognized table returns the table's OWN record shape with the extra
               // expression appended, so position 0 is no longer our boolean expression
               // (jOOQ logs "API misuse ... not present in table" and get(0,...) silently
               // reads the wrong column). .returningResult(...) requests EXACTLY this
               // field and nothing else, independent of the table's real column list.
               .returningResult(DSL.field("(xmax = 0)", Boolean.class))
               .fetchOne();
            return rec != null && Boolean.TRUE.equals(rec.value1());
        });
    }

    /**
     * nexus-9ssih — a link whose endpoint does not resolve to a LIVE document.
     *
     * <p>Extends {@link IllegalArgumentException} so the handler's existing
     * ladder already maps it to 400; {@link #missing()} lets the wire response
     * name which side dangles, which is what the client needs to distinguish
     * this from any other 400 (the auto-linker counts
     * {@code skipped_missing_endpoint} off exactly this signal — the local
     * {@code link_if_absent} raised {@code ValueError} for it, and the service
     * path silently wrote the dangling edge instead).
     */
    public static final class DanglingEndpointException extends IllegalArgumentException {
        private static final long serialVersionUID = 1L;
        private final transient List<String> missing;

        DanglingEndpointException(List<String> missing, String message) {
            super(message);
            this.missing = List.copyOf(missing);
        }

        /** Which endpoint fields dangle: {@code from_tumbler}, {@code to_tumbler}, or both. */
        public List<String> missing() {
            return missing;
        }
    }

    /**
     * Reject a link whose {@code from}/{@code to} does not resolve to a LIVE
     * (non-tombstoned) document in this tenant (nexus-9ssih).
     *
     * <p>Applies to {@link #upsertLink} — the interactive/auto-linker write
     * path — and NOT to the {@code import*} family, which legitimately writes
     * edges for documents whose live state the ETL leg does not control (same
     * carve-out {@code physicalCollectionOf} already documents for the manifest
     * write path). Callers that genuinely want an unvalidated edge pass
     * {@code allow_dangling: true}, the parity of the local {@code link}'s own
     * {@code allow_dangling} flag.
     */
    private static void requireLiveEndpoints(DSLContext ctx, String tenant, String fromT, String toT) {
        List<String> missing = new ArrayList<>(2);
        if (!liveDocument(ctx, tenant, fromT)) missing.add("from_tumbler");
        if (!liveDocument(ctx, tenant, toT))   missing.add("to_tumbler");
        if (missing.isEmpty()) return;
        throw new DanglingEndpointException(missing,
            "dangling link endpoint: " + String.join(", ", missing)
            + " does not resolve to a live catalog document"
            + " (from_tumbler=" + fromT + " to_tumbler=" + toT + ")."
            + " Pass allow_dangling=true to write the edge anyway.");
    }

    private static boolean liveDocument(DSLContext ctx, String tenant, String tumbler) {
        if (tumbler == null || tumbler.isBlank()) return false;
        return ctx.fetchExists(
            ctx.selectOne().from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                      .and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                      .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())));
    }

    /** Delete a link by (from, to, type). Returns deleted count. */
    public int deleteLink(String tenant, String fromT, String toT, String linkType) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(CATALOG_LINKS)
               .where(CATALOG_LINKS.FROM_TUMBLER.eq(fromT).and(CATALOG_LINKS.TO_TUMBLER.eq(toT)).and(CATALOG_LINKS.LINK_TYPE.eq(linkType)))
               .execute()
        );
    }

    /** Links from a tumbler, optionally filtered by link_type. */
    /**
     * Links from a tumbler, optionally filtered by a SET of link types (server-side IN).
     * RDR-168 nexus-njrcn.5: lets multi-type callers filter in SQL instead of fetching
     * every edge and filtering client-side (the high-fan-out over-fetch). Pass {@code null}
     * (or empty) for no type filter, a singleton list for one type.
     */
    public List<Map<String, Object>> linksFrom(String tenant, String fromTumbler, List<String> linkTypes) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition where = CATALOG_LINKS.FROM_TUMBLER.eq(fromTumbler);
            if (linkTypes != null && !linkTypes.isEmpty()) where = where.and(CATALOG_LINKS.LINK_TYPE.in(linkTypes));
            return ctx.select(CATALOG_LINKS.ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE,
                               CATALOG_LINKS.FROM_SPAN, CATALOG_LINKS.TO_SPAN, CATALOG_LINKS.CREATED_BY, CATALOG_LINKS.CREATED_AT, F_LNK_META)
                      .from(CATALOG_LINKS).where(where).fetch()
                      .map(r -> linkRow(r.value1(), r.value2(), r.value3(), r.value4(),
                                        r.value5(), r.value6(), r.value7(), r.value8(), r.value9()));
        });
    }

    /** Links to a tumbler, optionally filtered by a SET of link types (RDR-168 njrcn.5). */
    public List<Map<String, Object>> linksTo(String tenant, String toTumbler, List<String> linkTypes) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition where = CATALOG_LINKS.TO_TUMBLER.eq(toTumbler);
            if (linkTypes != null && !linkTypes.isEmpty()) where = where.and(CATALOG_LINKS.LINK_TYPE.in(linkTypes));
            return ctx.select(CATALOG_LINKS.ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE,
                               CATALOG_LINKS.FROM_SPAN, CATALOG_LINKS.TO_SPAN, CATALOG_LINKS.CREATED_BY, CATALOG_LINKS.CREATED_AT, F_LNK_META)
                      .from(CATALOG_LINKS).where(where).fetch()
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
            if (fromT != null && !fromT.isBlank())         cond = cond.and(CATALOG_LINKS.FROM_TUMBLER.eq(fromT));
            if (toT != null && !toT.isBlank())             cond = cond.and(CATALOG_LINKS.TO_TUMBLER.eq(toT));
            if (linkType != null && !linkType.isBlank())   cond = cond.and(CATALOG_LINKS.LINK_TYPE.eq(linkType));
            if (createdBy != null && !createdBy.isBlank()) cond = cond.and(CATALOG_LINKS.CREATED_BY.eq(createdBy));
            if (createdAtBefore != null && !createdAtBefore.isBlank())
                // nexus-4j80w: '' rows (pre-fix service-written links with no
                // stamped timestamp) must be UNMATCHABLE by a before-filter —
                // '' < any-date is TRUE under TEXT comparison, and without this
                // guard every such row matched every before-filter. Fail-safe:
                // they can still be reached by non-temporal filters. Mirrors the
                // local arm's guard (catalog_links.py: "created_at != '' AND
                // created_at < ?"). No backfill of existing '' rows — stamping
                // them with now() would lie about age, and stamping a sentinel
                // epoch would make them match every before-filter, i.e. the
                // exact hazard this guard exists to close.
                cond = cond.and(CATALOG_LINKS.CREATED_AT.ne(""))
                           .and(CATALOG_LINKS.CREATED_AT.lessThan(createdAtBefore));
            // direction + tumbler: filter by tumbler in the appropriate column(s)
            if (tumbler != null && !tumbler.isBlank()) {
                String dir = direction != null ? direction : "both";
                Condition tCond;
                if ("out".equals(dir)) {
                    tCond = CATALOG_LINKS.FROM_TUMBLER.eq(tumbler);
                } else if ("in".equals(dir)) {
                    tCond = CATALOG_LINKS.TO_TUMBLER.eq(tumbler);
                } else {
                    tCond = CATALOG_LINKS.FROM_TUMBLER.eq(tumbler).or(CATALOG_LINKS.TO_TUMBLER.eq(tumbler));
                }
                cond = cond.and(tCond);
            }
            return ctx.select(CATALOG_LINKS.ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE,
                               CATALOG_LINKS.FROM_SPAN, CATALOG_LINKS.TO_SPAN, CATALOG_LINKS.CREATED_BY, CATALOG_LINKS.CREATED_AT, F_LNK_META)
                      .from(CATALOG_LINKS).where(cond).orderBy(CATALOG_LINKS.ID)
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
            if (fromT != null && !fromT.isBlank())         cond = cond.and(CATALOG_LINKS.FROM_TUMBLER.eq(fromT));
            if (toT != null && !toT.isBlank())             cond = cond.and(CATALOG_LINKS.TO_TUMBLER.eq(toT));
            if (linkType != null && !linkType.isBlank())   cond = cond.and(CATALOG_LINKS.LINK_TYPE.eq(linkType));
            if (createdBy != null && !createdBy.isBlank()) cond = cond.and(CATALOG_LINKS.CREATED_BY.eq(createdBy));
            if (createdAtBefore != null && !createdAtBefore.isBlank())
                // nexus-4j80w: same non-empty guard as queryLinks — see that
                // call site for the full rationale. This is the destructive
                // twin (bulk_unlink); without the guard it deleted the entire
                // link graph on any --created-at-before call.
                cond = cond.and(CATALOG_LINKS.CREATED_AT.ne(""))
                           .and(CATALOG_LINKS.CREATED_AT.lessThan(createdAtBefore));
            return ctx.deleteFrom(CATALOG_LINKS).where(cond).execute();
        });
    }

    /**
     * The default graph-traversal link-type allow-list (nexus-ybj1b).
     *
     * <p>A faithful port of {@code catalog_links._filter_link_types}'s default
     * branch — the SERVER is the owner now, because the Python local path it
     * mirrors is RDR-158 P4 retirement debt.
     *
     * <p>Note this is an ALLOW-list, not "everything except
     * implements-heuristic". That distinction is load-bearing and is the local
     * contract: a CUSTOM link type is also excluded from default traversal,
     * and callers who want it must name it in {@code link_types} (or pass
     * {@code include_heuristic}). Implementing this as a deny-list would look
     * equivalent on the heuristic repro and silently diverge on custom types.
     *
     * <p>Why any of it exists (nexus-6ppk): {@code implements-heuristic} is
     * auto-emitted whenever a code chunk's symbols match an RDR's terminology,
     * so high-traffic infrastructure RDRs accumulate 500-660 inbound heuristic
     * edges each. The 2026-05-08 production probe measured 15,490 of them —
     * 66% of all 23,582 links — which drowns the ~6% hand-curated edges and
     * makes the node cap return mostly noise.
     */
    private static final List<String> DEFAULT_GRAPH_LINK_TYPES = List.of(
        "cites", "implements", "relates", "contains",
        "supersedes", "describes", "quotes", "comments",
        "formalizes", "same-as");

    /**
     * Port-parity sweep D8 (nexus-t7m8e comment, 2026-08-01): the local arm's
     * {@code _MAX_GRAPH_NODES} cap (catalog_links.py, pre-RDR-158-P4-deletion),
     * ported verbatim. graphBFS had no node cap — an unbounded BFS on a large
     * graph. Depth cap (see {@code Math.min(maxDepth, 3)} in {@link #graphBFS})
     * is applied BEFORE this node limit so the lowest-depth nodes always
     * survive truncation; truncation itself is ordered by (min_depth, tumbler)
     * so the surviving set is deterministic across repeated calls.
     */
    private static final int MAX_GRAPH_NODES = 500;

    /**
     * BFS graph traversal from seed tumblers.
     * Mirrors Catalog.graph() / Catalog.graph_many(): breadth-first up to maxDepth hops.
     *
     * @param seeds      starting tumblers
     * @param linkTypes  empty = server default (see below); non-empty = only these types
     * @param direction  "out"=from only, "in"=to only, "both"=both
     * @param maxDepth   BFS depth cap (1-3)
     * @return map with "nodes" (list of tumblers) and "edges" (list of link maps)
     */
    public Map<String, Object> graphBFS(String tenant, List<String> seeds,
                                         List<String> linkTypes, String direction, int maxDepth) {
        return graphBFS(tenant, seeds, linkTypes, direction, maxDepth, false);
    }

    /**
     * BFS graph traversal, honouring {@code includeHeuristic} (nexus-ybj1b).
     *
     * <p>THE DEFECT THIS CLOSES. {@code Catalog.graph}/{@code graph_many}
     * exclude {@code implements-heuristic} by default and let callers opt back
     * in. The HTTP client sent the flag with the comment "forwarded to service
     * for future support; currently informational" — and the server read only
     * {@code link_types}, so {@code include_heuristic} appeared NOWHERE in
     * this module. Both directions were broken: the default did not exclude
     * (the 2:1 flood was silently reinstated for every service-mode user, on
     * the 6.0 default backend) and the opt-in was indistinguishable from it.
     *
     * <p>A flag the client sends and the server ignores is a silent contract
     * break, which is why this is fixed server-side where the traversal and
     * the semantics both live, rather than by having the client enumerate
     * types — that stopgap only works when the caller supplied none, and turns
     * "all types except one" into a list that goes stale as link types are
     * added.
     *
     * <p>Three cases, matching {@code _filter_link_types} exactly:
     * <ul>
     *   <li>caller named types — they win untouched, heuristic included if
     *       they asked for it. The caller knows what they want.</li>
     *   <li>no types, {@code includeHeuristic} — no filter at all, every type.</li>
     *   <li>no types, default — {@link #DEFAULT_GRAPH_LINK_TYPES}.</li>
     * </ul>
     */
    public Map<String, Object> graphBFS(String tenant, List<String> seeds,
                                         List<String> linkTypes, String direction, int maxDepth,
                                         boolean includeHeuristic) {
        if (seeds == null || seeds.isEmpty()) return Map.of("nodes", List.of(), "edges", List.of());
        int depth = Math.min(Math.max(maxDepth, 1), 3);
        final List<String> effectiveTypes =
            (linkTypes != null && !linkTypes.isEmpty()) ? linkTypes
            : includeHeuristic                          ? List.of()
            :                                             DEFAULT_GRAPH_LINK_TYPES;

        return tenantScope.withTenant(tenant, ctx -> {
            // nexus-t7m8e leg (a)/(b): both endpoints of every traversed edge
            // must be a LIVE document. INNER-joining CATALOG_DOCUMENTS on both
            // from_tumbler and to_tumbler with deleted_at IS NULL closes two
            // defects at once: (i) an edge naming a tumbler absent from the
            // final node set (tombstoned OR never registered — a dangling
            // reference) is never emitted, and (ii) a tombstoned document can
            // no longer act as a live RELAY (A -> D(tombstoned) -> B is no
            // longer reachable at depth 2 just because D is invisible — the
            // join excludes the A->D and D->B edges alike, so D never enters
            // the frontier).
            var fromDocs = CATALOG_DOCUMENTS.as("cd_bfs_from");
            var toDocs   = CATALOG_DOCUMENTS.as("cd_bfs_to");

            Map<String, Integer> depthOf = new LinkedHashMap<>();
            for (String s : seeds) depthOf.put(s, 0);
            Set<String> visited = new LinkedHashSet<>(seeds);
            List<Map<String, Object>> edges = new ArrayList<>();
            Set<String> frontier = new LinkedHashSet<>(seeds);

            for (int d = 0; d < depth && !frontier.isEmpty(); d++) {
                Set<String> next = new LinkedHashSet<>();
                List<String> fl = new ArrayList<>(frontier);

                Condition dirCond;
                if ("out".equals(direction)) {
                    dirCond = CATALOG_LINKS.FROM_TUMBLER.in(fl);
                } else if ("in".equals(direction)) {
                    dirCond = CATALOG_LINKS.TO_TUMBLER.in(fl);
                } else {
                    dirCond = CATALOG_LINKS.FROM_TUMBLER.in(fl).or(CATALOG_LINKS.TO_TUMBLER.in(fl));
                }
                if (!effectiveTypes.isEmpty()) {
                    dirCond = dirCond.and(CATALOG_LINKS.LINK_TYPE.in(effectiveTypes));
                }

                var rows = ctx.select(CATALOG_LINKS.ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE,
                                       CATALOG_LINKS.FROM_SPAN, CATALOG_LINKS.TO_SPAN, CATALOG_LINKS.CREATED_BY, CATALOG_LINKS.CREATED_AT, F_LNK_META)
                              .from(CATALOG_LINKS)
                              .join(fromDocs).on(fromDocs.TUMBLER.eq(CATALOG_LINKS.FROM_TUMBLER))
                              .join(toDocs).on(toDocs.TUMBLER.eq(CATALOG_LINKS.TO_TUMBLER))
                              .where(dirCond
                                     .and(fromDocs.DELETED_AT.isNull())
                                     .and(toDocs.DELETED_AT.isNull()))
                              .fetch();
                for (var r : rows) {
                    Map<String, Object> lm = linkRow(r.value1(), r.value2(), r.value3(), r.value4(),
                                                      r.value5(), r.value6(), r.value7(), r.value8(), r.value9());
                    edges.add(lm);
                    String fromT = (String) lm.get("from_tumbler");
                    String toT   = (String) lm.get("to_tumbler");
                    if (!visited.contains(fromT)) { next.add(fromT); visited.add(fromT); depthOf.put(fromT, d + 1); }
                    if (!visited.contains(toT))   { next.add(toT);   visited.add(toT);   depthOf.put(toT, d + 1); }
                }
                frontier = next;
            }

            // nexus-t7m8e leg (c): the 500-node cap, ported from the local arm
            // (_MAX_GRAPH_NODES). Depth cap already applied above (the BFS ran
            // at most `depth` rounds); ordering the FULL reachable set by
            // (min_depth, tumbler) before truncating means the lowest-depth
            // nodes always survive and the surviving 500 are deterministic
            // across repeated calls on the same graph.
            List<String> ordered = visited.stream()
                .sorted(Comparator
                    .comparingInt((String t) -> depthOf.getOrDefault(t, Integer.MAX_VALUE))
                    .thenComparing(Comparator.naturalOrder()))
                .toList();
            boolean atOrOverCap = ordered.size() >= MAX_GRAPH_NODES;
            Set<String> surviving = new LinkedHashSet<>(
                atOrOverCap ? ordered.subList(0, MAX_GRAPH_NODES) : ordered);
            if (atOrOverCap) {
                log.warn("event=graph_node_limit tenant={} visited={} max_nodes={}",
                          tenant, ordered.size(), MAX_GRAPH_NODES);
            }

            List<Map<String, Object>> survivingEdges = edges.stream()
                .filter(e -> surviving.contains((String) e.get("from_tumbler"))
                          && surviving.contains((String) e.get("to_tumbler")))
                .toList();

            List<Map<String, Object>> nodes = new ArrayList<>();
            if (!surviving.isEmpty()) {
                nodes = ctx.select(documentFields()).from(CATALOG_DOCUMENTS)
                           .where(CATALOG_DOCUMENTS.TUMBLER.in(new ArrayList<>(surviving)).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                           .fetch().map(r -> docRowFromRecord(r.intoMap()));
            }
            return Map.of("nodes", nodes, "edges", survivingEdges);
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // DOCUMENT CHUNKS MANIFEST
    // ══════════════════════════════════════════════════════════════════════════

    /** Replace manifest for docId with the provided rows (atomic delete + insert). */
    public void writeManifest(String tenant, String docId, List<Map<String, Object>> rows) {
        tenantScope.withTenant(tenant, ctx -> {
            writeManifestRows(ctx, tenant, docId, rows);
            return null;
        });
    }

    /**
     * Shared REPLACE body (delete all rows for docId, then insert the provided
     * rows) used by both {@link #writeManifest} (one doc per transaction) and
     * {@link #writeManifestMany} (N docs, one transaction each). Assumes
     * {@code ctx} is already scoped to {@code tenant}. Folds
     * {@code documents.chunk_count = rows.size()} in the SAME transaction
     * (nexus-b6enc F5 — previously only {@code writeManifestMany} folded the
     * count, so the single-doc REPLACE left a stale count behind).
     */
    /**
     * nexus-x6kdz: the doc's physical_collection, stamped onto every manifest
     * row AT WRITE TIME. The combined-query functions (catalog-006/-008/-012)
     * join {@code m.collection = c.collection}; before this stamp NO writer
     * populated the column — only the migration-leg {@code manifest_backfill()}
     * ever did, and the REPLACE writers wiped it again on re-index, leaving
     * every post-migration manifest row invisible to the combined queries
     * (silent-empty, found by the 6.5.0 live shakeout). Returns null when the
     * doc has no physical_collection (ghost/sourceless docs) — those rows stay
     * NULL, same as the backfill's own skip semantics.
     *
     * <p>nexus-23wlw: DELIBERATELY NOT tombstone-filtered. Every other read
     * gained {@code deleted_at IS NULL} because a tombstone must be invisible
     * to readers; this is not a reader. Its only callers are the manifest
     * WRITE paths ({@code writeManifestRows}, {@code appendManifestChunks},
     * and the two import-chunk paths), where filtering would silently stamp
     * NULL onto rows being written for a tombstoned doc — re-introducing the
     * exact silent-empty class the nexus-x6kdz stamp above exists to fix, and
     * doing it on the ETL/import leg, which legitimately writes manifests for
     * documents whose live state it does not control. If a future caller uses
     * this as a read, filter it there rather than here.
     *
     * <p>nexus-eg5gx: this is NOT "the ONE unfiltered read in this class" —
     * that claim went stale the moment other deliberate exemptions were
     * documented ({@code highestChildSeq}, {@code upsertDocument}, {@code
     * manifestRowCount}, {@code renameCollectionTxn}). The authoritative,
     * exhaustive list is {@link TombstoneFilterGateTest#TOMBSTONE_EXEMPT} —
     * consult that table, not a count claimed in any one method's javadoc.
     */
    // TOMBSTONE-EXEMPT (nexus-mqd6t): manifest WRITE-path helper, not a
    // reader -- see the javadoc above. See TombstoneFilterGateTest.TOMBSTONE_EXEMPT.
    private static String physicalCollectionOf(DSLContext ctx, String tenant, String docId) {
        String pc = ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                       .from(CATALOG_DOCUMENTS)
                       .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
                       .and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                       .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION);
        return (pc == null || pc.isEmpty()) ? null : pc;
    }

    /**
     * nexus-p5qk8 (GH #1397 field report): manifest writes must refresh the
     * parent document's indexed_at. Before this, a chunk backfill (nx index
     * --force repairing chunk_count=0 ghosts) left indexed_at frozen at the
     * original ghost registration date — misleading repair provenance.
     */
    private static final java.time.format.DateTimeFormatter INDEXED_AT_FMT =
        java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSSSSxxx");

    private static void stampIndexedAt(DSLContext ctx, String tenant, String docId) {
        // Fixed-width micros + "+00:00", byte-identical shape to Python's
        // datetime.now(UTC).isoformat(): indexed_at is TEXT and MAX()'d
        // lexicographically (catalog-009 collection_health_meta) — mixed
        // widths/suffixes would break sortability at second-boundary ties.
        //
        // nexus-eldyi: guarded with deleted_at IS NULL — the non-resurrection
        // rule buildUpdateDocumentQuery enforces was bypassed here, so a
        // manifest write against a tombstoned doc_id still stamped its
        // indexed_at. ADVISORY write (a provenance timestamp, not data the
        // manifest count depends on): a 0-row no-op is the correct outcome,
        // no typed refusal needed — unlike the manifest ROW writers below,
        // which fail loud (see writeManifestRows / appendManifestChunks /
        // purgeManifest).
        ctx.update(CATALOG_DOCUMENTS)
           .set(CATALOG_DOCUMENTS.INDEXED_AT,
                java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC).format(INDEXED_AT_FMT))
           .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
           .and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
           .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
           .execute();
    }

    /**
     * Refused write against a tombstoned document (nexus-eldyi). Thrown by the
     * manifest ROW writers ({@link #writeManifestRows}, {@link
     * #appendManifestChunks}, {@link #purgeManifest}) when their guarded
     * CATALOG_DOCUMENTS update affects 0 rows BECAUSE the target is
     * tombstoned — distinct from "tumbler unknown", which stays a silent 0/no-op
     * (the long-standing contract for a doc_id that was never registered).
     * These three are void or return an UNRELATED count (deleted
     * catalog_document_chunks rows, not the guarded documents-row update), so a
     * silent no-op here would misreport success on a data-bearing write; a
     * per-int-return-value writer (updateDocumentCollection,
     * updateDocumentsCollectionBatch, setAlias) does not need this — the
     * caller already sees the honest 0.
     */
    public static final class TombstonedDocumentException extends IllegalStateException {
        private static final long serialVersionUID = 1L;

        TombstonedDocumentException(String docId, String message) {
            super(message);
        }
    }

    /**
     * Does *docId* exist AND carry a non-null deleted_at (nexus-eldyi)? Used
     * ONLY after a guarded UPDATE already returned 0 rows — an extra read on
     * the rare refusal path, never on the common live-doc path.
     */
    private static boolean isTombstonedDocument(DSLContext ctx, String tenant, String docId) {
        return ctx.fetchExists(ctx.selectOne().from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
            .and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
            .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull()));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-11gh6 rev 2: per-(tenant, collection) manifest-insert-vs-sweep
    // advisory gate. T2 nexus/design-11gh6-sweep-write-skew-closure [21768]
    // (critic-approved, T2 nexus/critique-design-11gh6-sweep-gate-2026-08-08
    // [21774]). See {@link #runSweepTransaction} for what this closes and
    // what it does not.
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * SWEEPER-side acquisition-wait bound (nexus-11gh6 rev 2 §5.2): how long
     * a sweep transaction waits to ACQUIRE the EXCLUSIVE gate before giving
     * up with a catchable {@code 55P03} — fail-open (the chunk survives,
     * the doc is counted in {@code sweep_skipped}). Distinct from {@link
     * #SWEEP_STATEMENT_TIMEOUT_MS} below, which bounds how long the gate is
     * HELD once granted — rev 1 of the design conflated the two (critic
     * Significant finding, resolved in rev 2). An adjustable backstop, not
     * a load-bearing constant.
     */
    private static final String SWEEP_GATE_LOCK_TIMEOUT_MS = "2000";

    /**
     * SWEEPER-side per-DELETE-statement HOLD bound (nexus-11gh6 rev 2 §5.2,
     * corrected post-implementation-review 2026-08-08 — T2 nexus/review-
     * 11gh6-gate-2026-08-08 [21797] Significant finding, independently
     * confirmed empirically by the critic against live PostgreSQL 17: {@code
     * statement_timeout} resets its clock at the START of EACH statement,
     * not once per transaction).
     *
     * <p>{@link #runSweepTransaction} issues up to THREE sequential DELETE
     * statements ({@link #sweepChunks384}/{@link #sweepChunks768}/{@link
     * #sweepChunks1024}) under ONE {@code set_config} call, so this constant
     * bounds each of the three INDIVIDUALLY, not their sum: the true
     * worst-case EXCLUSIVE-hold duration for one sweeping doc is up to
     * {@code 3 * SWEEP_STATEMENT_TIMEOUT_MS} (~15s at the default), not 1x —
     * and the worst-case writer STALL is therefore {@code (sweeps queued
     * ahead, bounded by flush_concurrency) * 3 * SWEEP_STATEMENT_TIMEOUT_MS},
     * not the {@code queued * SWEEP_STATEMENT_TIMEOUT_MS} formula rev 2 §5.2
     * and the rev-2-delta critique (T2 nexus/critique-design-11gh6-sweep-
     * gate-2026-08-08 [21774] §1(c)) both stated. This does NOT change the
     * SAFETY property (still finite, still fail-open on {@code 57014}) — it
     * corrects the documented MAGNITUDE of that finite bound.
     *
     * <p>Tempered in practice, not a reason to lower the constant: RDR-103
     * collection naming ties one collection to one embedding model/dim, so
     * for any given sweeping doc only ONE of the three dim tables ever holds
     * real candidate rows — the other two DELETEs are near-instant indexed
     * no-ops against an empty candidate set. The realistic worst case stays
     * close to ONE table's cost; the 3x figure is the correct WORST-CASE
     * bound to document, not the expected one.
     */
    private static final String SWEEP_STATEMENT_TIMEOUT_MS = "5000";

    /**
     * SHARED half of the gate. Every transaction that inserts into, or
     * bulk-repoints the collection of, {@code catalog_document_chunks} takes
     * this before doing so — before {@link #acquireIndexRunLock} too, on the
     * sites that also take that lock (future-proofing ordering — see each
     * call site):
     *
     * <table>
     *   <caption>writer sites</caption>
     *   <tr><th>Site</th><th>Serves</th></tr>
     *   <tr><td>{@link #writeManifestRows}</td><td>{@link #writeManifest}, {@link #writeManifestMany}</td></tr>
     *   <tr><td>{@link #appendManifestChunks}</td><td>continuation-slice appends</td></tr>
     *   <tr><td>{@link #importChunksBatch}</td><td>RDR-176 ETL leg</td></tr>
     *   <tr><td>{@link #doImportChunk}</td><td>RDR-176 ETL leg</td></tr>
     *   <tr><td>{@link #renameCollectionTxn}</td><td>bulk re-homes existing rows into a new collection scope — nexus-11gh6 rev 2 §3.2</td></tr>
     *   <tr><td>{@code ChashRepository.renameCollection}</td><td>a SECOND, independently-reachable
     *       (via {@code /v1/chash/*}) collection-rename implementation doing the SAME re-home
     *       mutation as {@link #renameCollectionTxn} — missed by the design's own coverage audit
     *       and by round 1 of this gate's implementation; added post-review (T2 nexus/review-
     *       11gh6-gate-2026-08-08 [21797] Important finding).</td></tr>
     *   <tr><td>{@code StagingPromoteOps.finalizeTenant}</td><td>RDR-180 land-then-transform's
     *       tenant-wide manifest promote, raw SQL, HTTP-reachable via {@code POST
     *       /v1/staging/finalize} — the design's grep-based coverage audit and the original
     *       {@code ManifestInsertGateTest} (jOOQ-typed pattern, one file) were both structurally
     *       blind to it. Added post-review (T2 nexus/critique-11gh6-gate-impl-2026-08-08 [21798]
     *       Critical finding). Gates once per DISTINCT target collection resolved by joining the
     *       INSERT's own candidate chashes against {@code chunks_384/768/1024} directly (round 3
     *       fix — resolving via the referencing doc's {@code physical_collection} instead, as
     *       round 2 did, could diverge from where the content actually lives, since chunk rows are
     *       duplicated per collection and the method's own {@code canonExists} check is
     *       deliberately collection-agnostic; see that method's own comment for the full
     *       reasoning).</td></tr>
     *   <tr><td>{@code StagingPromoteOps.promoteCollection}</td><td>RDR-180 land-then-transform's
     *       per-collection content landing — added round 3 (T2 nexus/critique-11gh6-gate-impl-
     *       2026-08-08 [21798] REWORK DELTA Critical finding): the round-2 exemption argument for
     *       this method ("a chash with no live manifest reference can never become a sweep
     *       candidate") is true only for a chash that has NEVER had any manifest reference —
     *       it does not cover a SHARED chash already referenced by a live, unrelated document,
     *       which can be dropped (and swept) by that document's own ordinary write while this
     *       method is landing the SAME content fresh for the migration. Single-collection
     *       parameter, no multi-collection resolution needed — see that method's own comment for
     *       the residual this narrows but does not fully close (structurally the same
     *       promote-then-later-finalize gap as nexus-kl2z6, one level up the RDR-180
     *       pipeline).</td></tr>
     * </table>
     *
     * <p>Package-private, not {@code private}: {@code ChashRepository} and {@code
     * StagingPromoteOps} are siblings in this package that independently mutate
     * {@code catalog_document_chunks} and need the SAME gate — no public surface required.
     *
     * <p>Shared locks never conflict with each other, so writers never
     * contend with writers — only with a concurrent EXCLUSIVE sweep
     * ({@link #acquireSweepGateExclusive}) for the SAME {@code (tenant,
     * collection)}. Deliberately NO {@code lock_timeout} here: a timeout on
     * the writer side would fail a load-bearing manifest write (fail-CLOSED)
     * purely because a sweep for the same collection happens to be running —
     * exactly the failure direction this gate exists to avoid. The wait is
     * bounded transitively: a pending EXCLUSIVE sweep is itself bounded by
     * {@link #SWEEP_GATE_LOCK_TIMEOUT_MS} / {@link #SWEEP_STATEMENT_TIMEOUT_MS}
     * on the sweeper side.
     *
     * <p>A {@code null} collection (ghost/sourceless docs, see {@link
     * #physicalCollectionOf}) takes NO gate: those manifest rows carry a
     * NULL collection, and both {@code manifest_verify} and the sweep
     * exclude NULL-collection rows entirely, so they never participate in
     * the invariant this gate protects.
     *
     * <p>Typed jOOQ {@code function(...)} composition (house rule —
     * {@code RawSqlGateTest}), mirroring {@code TaxonomyRepository.
     * lockTaxonomyCollection}'s idiom exactly: no {@code
     * RawSqlGateTest.SANCTIONED_METHODS} entry needed, unlike {@link
     * #acquireIndexRunLock}'s raw {@code ctx.execute}.
     */
    static void acquireSweepGateShared(DSLContext ctx, String tenant, String collection) {
        ctx.select(DSL.function("pg_advisory_xact_lock_shared", Object.class,
                   DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + tenant + "/" + collection))))
           .fetch();
    }

    /**
     * EXCLUSIVE half of the gate, taken ONLY by the sweep's own transaction
     * ({@link #runSweepTransaction}) — never inside a manifest-insert
     * transaction, which would deadlock a shared-to-exclusive upgrade
     * against a second, concurrent {@code sweep=true} writer doing the
     * same thing (see {@link #runSweepTransaction}'s javadoc). Sets a
     * {@code lock_timeout} (bounding the ACQUISITION wait) and a {@code
     * statement_timeout} (bounding the HOLD once granted, i.e. the
     * subsequent DELETE's own execution time) in one {@code set_config}
     * call, then acquires the key EXCLUSIVE. Both timeouts raise a
     * catchable PostgreSQL error ({@code 55P03} / {@code 57014}
     * respectively) that {@link #runSweepTransaction} maps to a fail-open
     * skip — this method itself does not catch anything.
     */
    private static void acquireSweepGateExclusive(DSLContext ctx, String tenant, String collection) {
        ctx.select(DSL.function("set_config", String.class,
                                DSL.val("lock_timeout"), DSL.val(SWEEP_GATE_LOCK_TIMEOUT_MS), DSL.val(true)),
                   DSL.function("set_config", String.class,
                                DSL.val("statement_timeout"), DSL.val(SWEEP_STATEMENT_TIMEOUT_MS), DSL.val(true)))
           .fetch();
        ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
                   DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + tenant + "/" + collection))))
           .fetch();
    }

    /** Which {@code ON CONFLICT} shape {@link #insertManifestChunkRows} applies. */
    private enum ManifestInsertMode {
        /** Bare insert, no conflict handling — the caller ({@link #writeManifestRows}) has
         *  already deleted every row for this doc_id in the SAME transaction, so no
         *  conflict is possible in practice. */
        PLAIN,
        /** Mirrors {@link #appendManifestChunks}' pre-existing minimal upsert: only
         *  {@code chash}/{@code collection} are updated on conflict (chunk_index/line/char
         *  positions are NOT touched — this method's long-standing contract, unchanged). */
        UPSERT_APPEND,
        /** Mirrors the two RDR-176 ETL import paths' full-column upsert. */
        UPSERT_IMPORT
    }

    /**
     * nexus-11gh6 §7d: the ONLY method in this file that references {@code
     * insertInto(CATALOG_DOCUMENT_CHUNKS} — single-homed so a structural
     * test can assert that invariant directly (exactly one call site)
     * rather than scanning each of the four manifest-insert sites for an
     * inline gate call, which rev 1 of this design tried and the
     * substantive-critic correctly flagged as blind to a future
     * helper-indirection refactor.
     *
     * <p>Every caller MUST already have taken {@link #acquireSweepGateShared}
     * for {@code coll} (when non-null) BEFORE calling this — see the table
     * on that method's javadoc. Deliberately NOT taken here: the ordering
     * rule ("gate, then {@link #acquireIndexRunLock}") needs to stay visible
     * at each call site, not buried inside a helper that runs after both
     * locks already exist.
     *
     * @param rows one or more manifest rows written as ONE multi-row
     *             {@code INSERT ... VALUES (...), (...), ...} statement.
     *             PostgreSQL conflict-checks each VALUES row of a
     *             multi-row INSERT independently, so passing more than one
     *             row only changes round-trip count, never ON CONFLICT
     *             semantics — but a caller whose rows can carry duplicate
     *             conflict keys within one call (e.g. {@link
     *             #appendManifestChunks}, which does not dedupe by
     *             position) MUST pass singleton lists to preserve its
     *             pre-existing per-row failure/round-trip shape: PostgreSQL
     *             refuses "ON CONFLICT DO UPDATE command cannot affect row
     *             a second time" for two VALUES rows sharing a conflict key
     *             in ONE statement. {@link #importChunksBatch} already
     *             dedupes by position before calling this, so its
     *             multi-row batches are safe as-is.
     */
    private static void insertManifestChunkRows(DSLContext ctx, String tenant, String docId, String coll,
                                                 List<Map<String, Object>> rows, ManifestInsertMode mode) {
        if (rows == null || rows.isEmpty()) return;
        var insert = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CHK_CHASH_HEX, CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX,
                CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END, CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION);
        for (var row : rows) {
            insert = insert.values(tenant, docId, i(row,"position"), s(row,"chash"), i(row,"chunk_index"),
                    i(row,"line_start"), i(row,"line_end"), i(row,"char_start"), i(row,"char_end"), coll);
        }
        switch (mode) {
            case PLAIN -> insert.execute();
            case UPSERT_APPEND -> insert
                .onConflict(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION)
                .doUpdate()
                .set(CHK_CHASH_HEX, EX_CHK_CHASH)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, EX_CHK_COLL)
                .execute();
            case UPSERT_IMPORT -> insert
                .onConflict(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION)
                .doUpdate()
                .set(CHK_CHASH_HEX, EX_CHK_CHASH)
                .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, EX_CHK_IDX)
                .set(CATALOG_DOCUMENT_CHUNKS.LINE_START, EX_CHK_LST)
                .set(CATALOG_DOCUMENT_CHUNKS.LINE_END, EX_CHK_LEN)
                .set(CATALOG_DOCUMENT_CHUNKS.CHAR_START, EX_CHK_CST)
                .set(CATALOG_DOCUMENT_CHUNKS.CHAR_END, EX_CHK_CEN)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, EX_CHK_COLL)
                .execute();
        }
    }

    private static String writeManifestRows(DSLContext ctx, String tenant, String docId,
                                          List<Map<String, Object>> rows) {
        // nexus-11gh6 rev 2 §2.2: the sweep gate's SHARED half is taken
        // BEFORE acquireIndexRunLock (table in acquireSweepGateShared's
        // javadoc) — future-proofing ordering, not load-bearing under the
        // CURRENT lock set (writers only ever hold the gate SHARED, and the
        // sweep is a wait-sink, so no deadlock is possible either order).
        String coll = physicalCollectionOf(ctx, tenant, docId);
        if (coll != null) {
            acquireSweepGateShared(ctx, tenant, coll);
        }
        // nexus-5xn3k.2 (stacked-review item 1): the WRITE side of the
        // completeIndexRun/writeManifestRows advisory-lock pair — see
        // acquireIndexRunLock's javadoc. Acquired BEFORE the delete+insert below
        // so a concurrent completeIndexRun for the same doc either runs entirely
        // before or entirely after this manifest mutation, never interleaved.
        acquireIndexRunLock(ctx, tenant, docId);
        ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId)).execute();
        if (!rows.isEmpty()) {
            stampIndexedAt(ctx, tenant, docId);
        }
        // nexus-11gh6 §7d: routed through the single-homed insert helper —
        // one row per call, preserving this method's pre-existing per-row
        // execute() cadence (a PLAIN insert following a full DELETE above,
        // so no ON CONFLICT is needed or applied).
        for (var row : rows) {
            insertManifestChunkRows(ctx, tenant, docId, coll, List.of(row), ManifestInsertMode.PLAIN);
        }
        // nexus-b6enc F5: the manifest IS the count's source of truth — fold
        // it in the same transaction so no REPLACE can leave a stale count.
        // nexus-eldyi: guarded — a tombstoned doc_id must not have its
        // CATALOG_DOCUMENTS row mutated. FAIL LOUD (not the stampIndexedAt
        // silent-noop shape): this is a data-bearing manifest writer with a
        // VOID public signature (writeManifest/writeManifestMany), so a
        // silent 0-row update here would misreport success to the caller
        // while quietly discarding the just-written manifest rows above (this
        // throw rolls back the whole per-doc transaction, including the
        // delete+insert above — TenantScope.withTenant wraps this in one txn).
        int updated = ctx.update(CATALOG_DOCUMENTS)
           .set(CATALOG_DOCUMENTS.CHUNK_COUNT, rows.size())
           .where(CATALOG_DOCUMENTS.TUMBLER.eq(docId)
                  .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
           .execute();
        if (updated == 0 && isTombstonedDocument(ctx, tenant, docId)) {
            throw new TombstonedDocumentException(docId,
                "writeManifest refused: document is tombstoned: " + docId);
        }
        return coll;
    }

    /**
     * Batch REPLACE manifest for multiple docs (bead nexus-u2kwq). Each doc is
     * replaced in its OWN {@link TenantScope#withTenant} transaction that folds
     * together the {@link #writeManifest} REPLACE (delete all rows + insert) AND
     * a {@code documents.chunk_count = rows.size()} UPDATE — collapsing the
     * client's separate {@code /manifest/write} + chunk_count {@code /update}
     * round-trips into one atomic per-doc write. A failure on one doc rolls back
     * only that doc and its doc_id is collected in {@code failed_doc_ids}; sibling
     * docs are unaffected (per-doc atomicity, cross-doc isolation). Empty list is
     * a no-op.
     *
     * @param docs each entry {@code {"doc_id": "...", "rows": [<manifest row>...]}}.
     * @return {@code {docs: <int ok>, rows: <int total written>, failed_doc_ids: [...]}}.
     */
    public Map<String, Object> writeManifestMany(String tenant, List<Map<String, Object>> docs) {
        return writeManifestMany(tenant, docs, null);
    }

    /**
     * Overload adding the optional {@code complete} map (nexus-5xn3k.2, memo
     * §3.3): {@code {doc_id: content_hash}} for docs whose manifest write in
     * THIS call is also the run's completion — the flush-grain repo path
     * ({@code ChunkBatcher}, file-atomic, position 0 always present) so the
     * hot path stamps completion inside the transaction it already runs, no
     * extra round trip. Docs not present in {@code complete} (or when
     * {@code complete} is null) behave exactly as the two-arg overload.
     *
     * <p>A doc whose completion stamp is refused (fail-closed verify:
     * missing&gt;0 or referenced!=rows.size()) does NOT fail the doc's
     * manifest write — that write already succeeded and is correct
     * (over-work-never-under-work, memo §3.5). It is reported in the
     * response's {@code complete_refused} list instead of
     * {@code failed_doc_ids}.
     *
     * <p>{@code sweep} defaults to {@code false} — BACKWARD COMPATIBLE with
     * every existing caller of this overload (see the 4-arg overload below).
     *
     * @return {@code {docs, rows, failed_doc_ids, failed, complete_refused, complete_refused_count}}
     */
    public Map<String, Object> writeManifestMany(String tenant, List<Map<String, Object>> docs,
                                                  Map<String, String> complete) {
        return writeManifestMany(tenant, docs, complete, false);
    }

    /**
     * Overload adding the optional {@code sweep} flag (nexus-eslkl / T2
     * nexus/design-eslkl-hook-lock-narrowing §8.1). Defaults to {@code false}
     * on every OTHER overload above — a caller that does not opt in gets
     * byte-for-byte the same behaviour as before this flag existed
     * (BACKWARD COMPATIBLE, deliberately, so an in-flight client build
     * against the 3-arg shape keeps working unchanged).
     *
     * <p>When {@code true}, nexus-39upx's superseded-vector sweep runs for
     * each doc that drops at least one chash — its DELETE firing in a
     * SEPARATE transaction immediately after the doc's own manifest
     * transaction commits (nexus-11gh6 rev 2 §2.3; see {@link
     * #runSweepTransaction} for why it cannot share that transaction). A
     * per-{@code (tenant, collection)} advisory gate ({@link
     * #acquireSweepGateShared} / {@link #acquireSweepGateExclusive}) makes
     * the sweep's {@code NOT EXISTS} guard authoritative for the duration
     * of its DELETE: every manifest writer for the same collection takes
     * the gate SHARED before inserting, and a pending or granted EXCLUSIVE
     * sweep blocks new SHARED grants, so the guard can never miss an
     * in-flight writer. This CLOSES the engine-side manifest-insert-vs-
     * sweep-delete write-skew nexus-11gh6 documents — see {@link
     * #runSweepTransaction}'s javadoc for the precise scope of what is (and
     * is not) closed.
     *
     * <p>What this does NOT close: the CLIENT-side upload-window race
     * (nexus-kl2z6), where a concurrent flush worker's T3 vector write for
     * a shared chash can land before its OWN manifest write reaches the
     * engine — no engine-side lock can gate an event the engine has not
     * yet been told about. That is a separate, bounded residual; the
     * client's own sweep ({@code _sweep_superseded_vectors_many}) remains
     * the required safety net for THAT axis until nexus-kl2z6 resolves
     * (nexus-wxjr6).
     *
     * <p>Response gains {@code swept} (total T3 rows deleted across every
     * doc in this call), {@code sweep_skipped} (docs where the sweep was
     * attempted but could not run to completion — before-read or delete
     * failure; fail-open, NEVER fails the doc's manifest write) and
     * {@code sweep_detail} (per-doc {@code {doc_id, dropped, swept, kept}}
     * for every doc where at least one chash dropped out of the manifest).
     * All three are present and zero/empty when {@code sweep=false}.
     *
     * @return {@code {docs, rows, failed_doc_ids, failed, complete_refused,
     *         complete_refused_count, swept, sweep_skipped, sweep_detail}}
     */
    public Map<String, Object> writeManifestMany(String tenant, List<Map<String, Object>> docs,
                                                  Map<String, String> complete, boolean sweep) {
        int okDocs = 0;
        int totalRows = 0;
        int totalSwept = 0;
        int sweepSkipped = 0;
        List<String> failed = new ArrayList<>();
        List<Map<String, Object>> failedDetail = new ArrayList<>();
        List<Map<String, Object>> completeRefused = new ArrayList<>();
        List<Map<String, Object>> sweepDetail = new ArrayList<>();
        if (docs != null) {
            for (Map<String, Object> d : docs) {
                String docId = s(d, "doc_id");
                Object rawRows = d.get("rows");
                @SuppressWarnings("unchecked")
                List<Map<String, Object>> rows = rawRows instanceof List<?> l
                    ? (List<Map<String, Object>>) l
                    : List.of();
                String completeHash = (complete != null && docId != null) ? complete.get(docId) : null;
                // Per-doc sweep outcome, populated either inside the txn lambda
                // below (the beforeReadFailed case) or AFTER it returns (the
                // normal case — nexus-11gh6 rev 2 §2.3 moved the sweep DELETE
                // to its own transaction, fired post-commit; see
                // runSweepTransaction). Effectively-final array cells — a
                // local var cannot be assigned from within the lambda.
                Map<String, Object>[] sweepOutcome = new Map[1];
                String[] collHolder = new String[1];
                Set<String>[] beforeHolder = new Set[1];
                try {
                    if (docId == null || docId.isBlank()) {
                        throw new IllegalArgumentException("'doc_id' required");
                    }
                    // (chunk_count folds inside writeManifestRows — nexus-b6enc
                    // F5 unified the fold for the single-doc and batch paths.)
                    tenantScope.withTenant(tenant, ctx -> {
                        // nexus-eslkl: the sweep's "before" read MUST happen before
                        // writeManifestRows deletes doc_id's current rows below —
                        // it is the only way to learn which chashes THIS write drops.
                        // Savepoint-guarded (fail-open): a before-read failure must
                        // not abort the manifest write that follows it in the SAME
                        // transaction (TenantScope.withTenant rolls back the WHOLE
                        // transaction on any propagated exception).
                        //
                        // code-review-expert Important-2: the fallback sentinel is
                        // `null`, NOT Set.of() — a legitimately EMPTY manifest (a
                        // brand-new doc with nothing to drop) and a FAILED read must
                        // be distinguishable, because the accounting below routes a
                        // failed read into sweep_skipped/sweep_detail same as a
                        // failed delete. Collapsing both into Set.of() silently
                        // dropped a before-read failure on the floor — not counted
                        // anywhere, not in sweep_skipped, not in sweep_detail — the
                        // exact "swallowed failure" class nexus-fhhwf already fixed
                        // once for the doc-level catch a few lines up.
                        Set<String> beforeRead = sweep
                            ? withSavepointFailOpen(ctx, "write_manifest_many_sweep_before_read_failed",
                                  tenant, docId, () -> currentManifestChashes(ctx, tenant, docId), null)
                            : Set.of();
                        boolean beforeReadFailed = sweep && beforeRead == null;
                        collHolder[0] = writeManifestRows(ctx, tenant, docId, rows);
                        if (beforeReadFailed) {
                            // Nothing to compute — the before-read itself is what
                            // failed, so `dropped` was never determined. Reported
                            // as an honest errored=true outcome, never silently
                            // absorbed into "nothing to sweep".
                            sweepOutcome[0] = Map.of("doc_id", docId, "dropped", 0,
                                "swept", 0, "kept", 0, "errored", true);
                        } else if (sweep) {
                            // nexus-11gh6 rev 2 §2.3: capture `before` for the
                            // POST-COMMIT dropped-chash computation below — this
                            // lambda is the only place that has it. The actual
                            // sweep DELETE no longer runs in this transaction.
                            beforeHolder[0] = beforeRead;
                        }
                        if (completeHash != null) {
                            stampCompleteIfVerified(ctx, tenant, docId, completeHash, rows.size(), completeRefused);
                        }
                        return null;
                    });
                    okDocs++;
                    totalRows += rows.size();
                    // nexus-11gh6 rev 2 §2.3: fired ONLY after the manifest
                    // transaction above has COMMITTED (this line is unreachable
                    // if that transaction threw or failed to commit — control
                    // would have jumped to the catch below instead). Runs in
                    // its OWN transaction: the one above already holds the
                    // sweep gate SHARED (via writeManifestRows), and a
                    // shared-to-exclusive upgrade on the SAME key within one
                    // transaction deadlocks deterministically against a
                    // second, concurrent sweep=true writer doing the same —
                    // routine, not hypothetical, under ChunkBatcher's
                    // flush_concurrency=3. This also closes nexus-3wtku by
                    // construction: a doc that lands in `failed` below never
                    // reaches this line, so a rolled-back manifest write can
                    // no longer contribute a "swept" count to the response.
                    if (sweep && beforeHolder[0] != null) {
                        List<String> dropped = computeDroppedChashes(beforeHolder[0], rows);
                        if (!dropped.isEmpty() && collHolder[0] != null) {
                            sweepOutcome[0] = runSweepTransaction(tenant, docId, collHolder[0], dropped);
                        }
                    }
                } catch (Exception e) {
                    // nexus-fhhwf: the per-doc swallow used to discard the
                    // CAUSE — a chash CHECK violation surfaced as a bare id
                    // in failed_doc_ids and took 3 deploy-gate iterations to
                    // diagnose. Classify and return a structured reason.
                    Map<String, Object> detail = failureDetail(docId, e);
                    log.debug("event=write_manifest_many_doc_failed tenant={} doc_id={} reason={} sqlstate={}",
                              tenant, docId, detail.get("reason"), detail.get("sqlstate"));
                    failed.add(docId);
                    failedDetail.add(detail);
                }
                if (sweep) {
                    Map<String, Object> outcome = sweepOutcome[0];
                    if (outcome != null) {
                        sweepDetail.add(outcome);
                        totalSwept += (Integer) outcome.get("swept");
                        if (Boolean.TRUE.equals(outcome.get("errored"))) {
                            sweepSkipped++;
                        }
                    }
                }
            }
        }
        if (!failedDetail.isEmpty()) {
            // One aggregate WARN per request (review: a systemic failure
            // across a 1000-doc batch must not emit 1000 WARN lines); the
            // full per-doc detail rides the RESPONSE + per-doc debug above.
            log.warn("event=write_manifest_many_failures tenant={} failed={} of={} sample={}",
                     tenant, failedDetail.size(), docs == null ? 0 : docs.size(),
                     failedDetail.subList(0, Math.min(3, failedDetail.size())));
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("docs", okDocs);
        result.put("rows", totalRows);
        result.put("failed_doc_ids", failed);   // back-compat
        result.put("failed", failedDetail);     // nexus-fhhwf: the diagnosable form
        result.put("complete_refused", completeRefused); // nexus-5xn3k.2: fail-closed stamp refusals
        // stacked-review item 2: a scalar sibling to the list — the ocf52-style
        // "ignorable list" problem (a caller that only checks docs/rows success
        // counts can silently never look at complete_refused; a non-zero scalar
        // is harder to miss in a log line or a quick response-shape glance).
        result.put("complete_refused_count", completeRefused.size());
        // nexus-eslkl: same "scalar sibling to a list" discipline — swept/
        // sweep_skipped are hard to miss even if a caller never inspects
        // sweep_detail (mirrors _record_superseded_swept /
        // _record_superseded_sweep_skip's CLI-summary contract client-side).
        result.put("swept", totalSwept);
        result.put("sweep_skipped", sweepSkipped);
        result.put("sweep_detail", sweepDetail);
        return result;
    }

    /**
     * Fail-open wrapper for a sweep sub-step (nexus-eslkl). Runs {@code body}
     * under a JDBC SAVEPOINT taken on the transaction's own connection and
     * rolls back to it on ANY exception, so a sweep failure can never leave
     * the surrounding transaction in PostgreSQL's aborted state — which
     * would otherwise silently kill the manifest write, chunk_count update,
     * and completion stamp that run AFTER it in the same transaction
     * ({@link TenantScope#withTenant} rolls back the WHOLE transaction on
     * any exception that escapes the lambda it wraps; a plain Java
     * try/catch around the sweep call, with no savepoint, does NOT undo
     * Postgres's server-side "current transaction is aborted" state once a
     * statement inside it has errored).
     *
     * <p>Logs at WARNING and returns {@code fallback} on any failure — never
     * rethrows. This is the mechanism, not the policy: "sweep errors are
     * fail-open" is the memo's requirement (over-retention is recoverable,
     * over-deletion is not); this method is what makes that true even
     * though the sweep now shares a transaction with load-bearing writes.
     *
     * <p>code-review-expert Suggestion (round 2): no explicit
     * {@code RELEASE SAVEPOINT} on the success path. Deliberate, not an
     * oversight — PostgreSQL auto-discards a savepoint at its enclosing
     * transaction's COMMIT/ROLLBACK regardless, and every call site here
     * takes at most one savepoint per per-doc transaction (no nesting, no
     * savepoint-count buildup within a single {@code withTenant} call), so
     * an explicit release buys nothing beyond what the transaction's own
     * end already does.
     */
    private static <T> T withSavepointFailOpen(DSLContext ctx, String event, String tenant, String docId,
                                                java.util.concurrent.Callable<T> body, T fallback) {
        java.sql.Connection conn = ctx.configuration().connectionProvider().acquire();
        java.sql.Savepoint sp = null;
        try {
            sp = conn.setSavepoint();
            return body.call();
        } catch (Exception e) {
            log.warn("event={} tenant={} doc_id={} error={}", event, tenant, docId, e.toString());
            if (sp != null) {
                try {
                    conn.rollback(sp);
                } catch (java.sql.SQLException se) {
                    log.error("event=write_manifest_many_sweep_savepoint_rollback_failed tenant={} doc_id={}",
                              tenant, docId, se);
                }
            }
            return fallback;
        } finally {
            ctx.configuration().connectionProvider().release(conn);
        }
    }

    /**
     * Read doc_id's CURRENT manifest chashes — the sweep's "before" set,
     * read BEFORE {@link #writeManifestRows} deletes them (nexus-eslkl).
     *
     * <p>{@link #liveParentDoc}-filtered (TombstoneFilterGateTest): a
     * tombstoned doc_id cannot reach the sweep in practice anyway —
     * {@code writeManifestRows} throws {@link TombstonedDocumentException}
     * for one, and the surrounding per-doc catch already routes that case
     * to {@code failed_doc_ids} with no sweep ever running — but the filter
     * costs nothing here and keeps this read honest with every other
     * manifest read in the file (getManifest/getManifestMany) rather than
     * relying on a downstream throw to make an unguarded read harmless.
     */
    private static Set<String> currentManifestChashes(DSLContext ctx, String tenant, String docId) {
        Set<String> out = new LinkedHashSet<>();
        var rows = ctx.select(CHK_CHASH_HEX).from(CATALOG_DOCUMENT_CHUNKS)
                      .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                             .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
                             .and(liveParentDoc(ctx, tenant)))
                      .fetch();
        for (var r : rows) {
            String c = r.value1();
            if (c != null && !c.isBlank()) out.add(c);
        }
        return out;
    }

    /**
     * Pure (no DB access) computation of which chashes THIS write drops
     * from {@code docId}'s manifest — present in {@code before} (its
     * manifest chashes read before this write, inside the manifest
     * transaction) but absent from this write's own {@code rows}.
     *
     * <p>Split out of the pre-nexus-11gh6-rev-2 {@code sweepSupersededVectors}
     * so it can run AFTER the manifest transaction has committed, without
     * needing that transaction's {@link DSLContext} — {@link
     * #runSweepTransaction} is what now does the DB-touching half, in its
     * OWN, later transaction.
     */
    private static List<String> computeDroppedChashes(Set<String> before, List<Map<String, Object>> rows) {
        Set<String> after = new LinkedHashSet<>();
        for (var row : rows) {
            String c = s(row, "chash");
            if (c != null && !c.isBlank()) after.add(c);
        }
        List<String> dropped = new ArrayList<>();
        for (String c : before) {
            if (!after.contains(c)) dropped.add(c);
        }
        return dropped;
    }

    /**
     * nexus-39upx's superseded-vector sweep's DELETE half — nexus-11gh6
     * rev 2 §2.3: fired in its OWN, freshly opened transaction, called ONLY
     * after the doc's manifest transaction ({@link #writeManifestRows} via
     * {@link #writeManifestMany}) has ALREADY COMMITTED.
     *
     * <p>Cannot run inside that transaction: it already holds the sweep
     * gate SHARED (via {@link #acquireSweepGateShared}), and a
     * shared-to-exclusive upgrade on the SAME advisory-lock key within one
     * transaction deadlocks deterministically against a second, concurrent
     * {@code sweep=true} writer doing the same thing — routine, not
     * hypothetical, under {@code ChunkBatcher}'s {@code flush_concurrency=3}.
     *
     * <p><b>What this closes, precisely</b> (nexus-11gh6 rev 2 §0/§2.4): the
     * engine-side manifest-insert-vs-sweep-delete write-skew. At the
     * instant this method's two {@code NOT EXISTS} guards (unchanged, see
     * {@link #sweepChunks384}) are evaluated, it holds the gate EXCLUSIVE
     * for {@code (tenant, collection)}, so every manifest INSERT for THAT
     * collection has either already committed (visible to this statement's
     * READ COMMITTED snapshot, so the guard sees it and skips) or has not
     * yet been admitted (a pending or granted EXCLUSIVE blocks new SHARED
     * grants — empirically verified against live PostgreSQL {@code
     * pg_locks} behaviour, T2 nexus/critique-design-11gh6-sweep-gate-
     * 2026-08-08). The window the original nexus-11gh6 finding described —
     * a concurrent writer's manifest INSERT committing strictly between
     * this guard's snapshot and its own commit — cannot exist under this
     * gate.
     *
     * <p><b>What this does NOT close</b> (nexus-11gh6 rev 2 §3.1): the
     * CLIENT-side upload-window race (nexus-kl2z6) — a concurrent flush
     * worker's T3 vector write for a shared chash can land before its OWN
     * manifest write reaches the engine, and no engine-side lock can gate
     * an event the engine has not yet been told about. Bounded residual;
     * the client's own sweep ({@code _sweep_superseded_vectors_many})
     * remains required for THAT axis until nexus-kl2z6 resolves
     * (nexus-wxjr6).
     *
     * <p>Two independent {@code NOT EXISTS} guards, both required, run
     * unchanged inside the exclusive-gated DELETE (see {@link
     * #sweepChunks384} for the exact SQL): the shared-chash union guard
     * (a live manifest reference to the same chash in ANY collection
     * proves "someone references this") and the nl3fn notes guard (a live,
     * note-shaped {@code catalog_documents} row whose identity chash
     * matches the candidate — a manifest-less chunk that must never be
     * swept regardless of manifest state).
     *
     * <p>Fail-open on ANY failure to acquire the gate within {@link
     * #SWEEP_GATE_LOCK_TIMEOUT_MS} ({@code 55P03}) or to complete the
     * DELETE within {@link #SWEEP_STATEMENT_TIMEOUT_MS} ({@code 57014}):
     * the chunk row survives, this doc is counted in {@code sweep_skipped},
     * and nothing here can ever fail the doc's manifest write, which has
     * ALREADY COMMITTED by the time this method is even called. This also
     * closes nexus-3wtku BY CONSTRUCTION: a doc whose manifest transaction
     * rolled back never reaches this method at all (see the call site in
     * {@link #writeManifestMany}), so a rolled-back write can no longer
     * report a nonzero {@code swept} count.
     */
    private Map<String, Object> runSweepTransaction(String tenant, String docId, String collection,
                                                     List<String> dropped) {
        try {
            return tenantScope.withTenant(tenant, ctx -> {
                acquireSweepGateExclusive(ctx, tenant, collection);
                int swept = sweepChunks384(ctx, tenant, collection, dropped)
                          + sweepChunks768(ctx, tenant, collection, dropped)
                          + sweepChunks1024(ctx, tenant, collection, dropped);
                if (swept > 0) {
                    log.info("event=write_manifest_many_swept tenant={} doc_id={} collection={} dropped={} swept={}",
                              tenant, docId, collection, dropped.size(), swept);
                }
                Map<String, Object> out = new LinkedHashMap<>();
                out.put("doc_id", docId);
                out.put("dropped", dropped.size());
                out.put("swept", swept);
                out.put("kept", dropped.size() - swept);
                out.put("errored", false);
                return out;
            });
        } catch (Exception e) {
            log.warn("event=write_manifest_many_sweep_gate_failed tenant={} doc_id={} collection={} dropped={} error={}",
                      tenant, docId, collection, dropped.size(), e.toString());
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("doc_id", docId);
            out.put("dropped", dropped.size());
            out.put("swept", 0);
            out.put("kept", dropped.size());
            out.put("errored", true);
            return out;
        }
    }

    /**
     * Per-dim sweep DELETE against {@code chunks_384} — see
     * {@link #runSweepTransaction} for the full guard rationale (nl3fn:
     * TWO independent {@code NOT EXISTS} clauses, shared-chash union guard
     * AND notes guard — neither alone is sufficient). The nested selects
     * reference the OUTER delete target's own row ({@code CHUNKS_384.TENANT_ID}/
     * {@code CHUNKS_384.CHASH}) — a standard correlated-subquery DELETE, the
     * same shape {@code purge_trash}'s raw SQL uses with an explicit alias.
     */
    private static int sweepChunks384(DSLContext ctx, String tenant, String collection, List<String> dropped) {
        return ctx.deleteFrom(CHUNKS_384)
            .where(CHUNKS_384.TENANT_ID.eq(tenant))
            .and(CHUNKS_384.COLLECTION.eq(collection))
            .and(C384_CHASH_HEX.in(dropped))
            .and(DSL.notExists(ctx.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
                .join(CATALOG_DOCUMENTS)
                  .on(CATALOG_DOCUMENTS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
                      .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID)))
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(CHUNKS_384.TENANT_ID))
                .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(CHUNKS_384.CHASH))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())))
            // nl3fn NOTES GUARD: is this chash a live note's OWN identity
            // chash (is_note_shaped mirror)? Collection-scoped, matching
            // catalog_documents_for_collection's scope client-side.
            .and(DSL.notExists(ctx.selectOne().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
                .and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(collection))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
                .and(CATALOG_DOCUMENTS.FILE_PATH.isNull().or(CATALOG_DOCUMENTS.FILE_PATH.eq("")))
                .and(DOC_META_DOC_ID.eq(DSL.field("encode({0}, 'hex')", String.class, CHUNKS_384.CHASH)))))
            .execute();
    }

    /** Per-dim sweep DELETE against {@code chunks_768} — see {@link #sweepChunks384}. */
    private static int sweepChunks768(DSLContext ctx, String tenant, String collection, List<String> dropped) {
        return ctx.deleteFrom(CHUNKS_768)
            .where(CHUNKS_768.TENANT_ID.eq(tenant))
            .and(CHUNKS_768.COLLECTION.eq(collection))
            .and(C768_CHASH_HEX.in(dropped))
            .and(DSL.notExists(ctx.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
                .join(CATALOG_DOCUMENTS)
                  .on(CATALOG_DOCUMENTS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
                      .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID)))
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(CHUNKS_768.TENANT_ID))
                .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(CHUNKS_768.CHASH))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())))
            .and(DSL.notExists(ctx.selectOne().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
                .and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(collection))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
                .and(CATALOG_DOCUMENTS.FILE_PATH.isNull().or(CATALOG_DOCUMENTS.FILE_PATH.eq("")))
                .and(DOC_META_DOC_ID.eq(DSL.field("encode({0}, 'hex')", String.class, CHUNKS_768.CHASH)))))
            .execute();
    }

    /** Per-dim sweep DELETE against {@code chunks_1024} — see {@link #sweepChunks384}. */
    private static int sweepChunks1024(DSLContext ctx, String tenant, String collection, List<String> dropped) {
        return ctx.deleteFrom(CHUNKS_1024)
            .where(CHUNKS_1024.TENANT_ID.eq(tenant))
            .and(CHUNKS_1024.COLLECTION.eq(collection))
            .and(C1024_CHASH_HEX.in(dropped))
            .and(DSL.notExists(ctx.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
                .join(CATALOG_DOCUMENTS)
                  .on(CATALOG_DOCUMENTS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
                      .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID)))
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(CHUNKS_1024.TENANT_ID))
                .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(CHUNKS_1024.CHASH))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())))
            .and(DSL.notExists(ctx.selectOne().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
                .and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(collection))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
                .and(CATALOG_DOCUMENTS.FILE_PATH.isNull().or(CATALOG_DOCUMENTS.FILE_PATH.eq("")))
                .and(DOC_META_DOC_ID.eq(DSL.field("encode({0}, 'hex')", String.class, CHUNKS_1024.CHASH)))))
            .execute();
    }

    /**
     * Compact, client-safe failure classification for the write_many per-doc
     * catch (nexus-fhhwf). SQLState + PostgreSQL constraint NAME are schema
     * metadata (safe to return); the raw driver message stays server-side.
     * Non-DB failures (our own IllegalArgumentException validation) return
     * their message verbatim — those strings are ours.
     */
    private static Map<String, Object> failureDetail(String docId, Throwable e) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("doc_id", docId == null ? "" : docId);
        Throwable c = e;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof java.sql.SQLException se && se.getSQLState() != null) {
                String state = se.getSQLState();
                String constraint = null;
                if (c instanceof org.postgresql.util.PSQLException pse
                        && pse.getServerErrorMessage() != null) {
                    constraint = pse.getServerErrorMessage().getConstraint();
                }
                String base = switch (state) {
                    case "23503" -> "foreign key violation (doc_id not registered?)";
                    case "23514" -> "check constraint violation";
                    case "23505" -> "unique violation";
                    case "23502" -> "not-null violation";
                    default -> "database error";
                };
                out.put("reason", constraint != null ? base + " [" + constraint + "]" : base);
                out.put("sqlstate", state);
                return out;
            }
        }
        // Non-SQL failure. ALLOWLIST, not exclusion (review M-a): verbatim
        // messages only for our own validation exceptions — an arbitrary
        // runtime exception's message can carry internal state (e.g. the
        // TenantScope admission-queue message wraps a null-SQLState
        // SQLTransientConnectionException and would fall through here).
        if ((e instanceof IllegalArgumentException || e instanceof TombstonedDocumentException)
                && e.getMessage() != null) {
            out.put("reason", e.getMessage());
        } else {
            out.put("reason", "internal error (" + e.getClass().getSimpleName() + ")");
        }
        return out;
    }

    /** Append manifest rows (upsert by position). */
    public void appendManifestChunks(String tenant, String docId, List<Map<String, Object>> rows) {
        tenantScope.withTenant(tenant, ctx -> {
            // nexus-11gh6 rev 2 §2.2: gate BEFORE acquireIndexRunLock — see
            // acquireSweepGateShared's javadoc (writer-site table) and
            // writeManifestRows for the identical ordering rationale.
            String coll = physicalCollectionOf(ctx, tenant, docId);
            if (coll != null) {
                acquireSweepGateShared(ctx, tenant, coll);
            }
            // Same manifest-mutation class as writeManifestRows: serialize
            // against completeIndexRun's verify-then-stamp (see
            // acquireIndexRunLock's javadoc) — this was the one mutation path
            // left outside the lock when it landed.
            acquireIndexRunLock(ctx, tenant, docId);
            if (!rows.isEmpty()) {
                stampIndexedAt(ctx, tenant, docId);
            }
            // nexus-11gh6 §7d: routed through the single-homed insert helper,
            // one row per call — this method does NOT dedupe by position
            // (unlike importChunksBatch), so a caller sending duplicate
            // positions in one call must keep failing exactly as before
            // (each row its own statement), not surface a NEW "ON CONFLICT
            // DO UPDATE command cannot affect row a second time" error from
            // a batched multi-row statement.
            for (var row : rows) {
                insertManifestChunkRows(ctx, tenant, docId, coll, List.of(row), ManifestInsertMode.UPSERT_APPEND);
            }
            // nexus-e4gel: fold documents.chunk_count in the SAME transaction,
            // exactly as writeManifestRows does (nexus-b6enc F5). The REPLACE
            // path folded and the APPEND path did not, so an append-grown
            // manifest carried a stale count until someone ran /manifest/resync.
            // count(*) rather than += rows.size(): appends upsert BY POSITION,
            // so an append that overwrites existing positions adds no rows.
            if (!rows.isEmpty()) {
                // nexus-eldyi: guarded, fail loud — same reasoning as
                // writeManifestRows (data-bearing, VOID public signature; the
                // throw rolls back the append written above in this same txn).
                int updated = ctx.update(CATALOG_DOCUMENTS)
                   .set(CATALOG_DOCUMENTS.CHUNK_COUNT, manifestRowCount(ctx, tenant, docId))
                   .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                          .and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                          .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                   .execute();
                if (updated == 0 && isTombstonedDocument(ctx, tenant, docId)) {
                    throw new TombstonedDocumentException(docId,
                        "appendManifestChunks refused: document is tombstoned: " + docId);
                }
            }
            return null;
        });
    }

    /**
     * nexus-mqd6t: the manifest reads below are READS, so a tombstoned parent
     * document must be invisible through them — the same rule nexus-23wlw
     * applied to the {@code catalog_documents} reads. {@code catalog_document_chunks}
     * carries no {@code deleted_at} of its own (the soft delete is on the parent
     * and the fk-001 CASCADE deliberately does not fire, RDR-156 P1.2), so the
     * filter has to be an EXISTS against a live parent rather than a column
     * predicate. Without it a soft-deleted document's manifest stayed publicly
     * readable via {@code /manifest/get} while {@code /show} returned 404.
     *
     * <p>Deliberately an EXISTS and not a JOIN: {@code catalog_document_chunks}
     * has no FK-guaranteed 1:1 to {@code catalog_documents} (import legs write
     * manifest rows for documents they do not own), so a JOIN could both drop
     * rows and — with a duplicated tumbler — multiply them. EXISTS filters
     * without touching cardinality.
     */
    private static Condition liveParentDoc(DSLContext ctx, String tenant) {
        return DSL.exists(
            ctx.selectOne().from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)
                      .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID))
                      .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())));
    }

    /** Get manifest rows for docId, ordered by position. Tombstoned docs read empty (nexus-mqd6t). */
    public List<Map<String, Object>> getManifest(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CHK_CHASH_HEX, CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX,
                       CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END, CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END)
               .from(CATALOG_DOCUMENT_CHUNKS)
               .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId).and(liveParentDoc(ctx, tenant)))
               .orderBy(CATALOG_DOCUMENT_CHUNKS.POSITION)
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

    /**
     * Purge all manifest rows for a document, zeroing the parent's
     * {@code chunk_count} in the SAME transaction (nexus-b6enc F5 — a purge
     * that leaves the count standing manufactures a ghost: a document
     * claiming chunks with no manifest rows behind it).
     */
    public int purgeManifest(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> {
            int deleted = ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                             .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId)).execute();
            // nexus-eldyi: guarded, fail loud. *deleted* counts
            // catalog_document_chunks rows (a DIFFERENT table with no
            // deleted_at of its own), so it stays an honest count regardless
            // of tombstone status — but it does NOT reflect whether this
            // CATALOG_DOCUMENTS fold succeeded, so a caller reading
            // deleted > 0 could otherwise believe the parent row's
            // chunk_count was zeroed when the guard silently refused it.
            int updated = ctx.update(CATALOG_DOCUMENTS)
               .set(CATALOG_DOCUMENTS.CHUNK_COUNT, 0)
               .where(CATALOG_DOCUMENTS.TUMBLER.eq(docId)
                      .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .execute();
            if (updated == 0 && isTombstonedDocument(ctx, tenant, docId)) {
                throw new TombstonedDocumentException(docId,
                    "purgeManifest refused: document is tombstoned: " + docId);
            }
            return deleted;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // INDEX RUN FENCE (RUNFENCE, nexus-5xn3k.2) — engine half step 2 of 2.
    //
    // Design of record: T2 nexus memory "5xn3k-design-2026-08-02" §3.3 + §3.5,
    // schema/SQL primitive landed in step 1 (nexus-5xn3k.1, catalog-020). Core
    // finding: no comparison of two artifacts BOTH written by the same run can
    // detect that the run did not finish (manifest == T3, both truncated, is a
    // CONSISTENT truncation every derived diff reads clean on). The fence is a
    // record of intent (index_state='indexing', stamped BEFORE the first chunk
    // upsert) vs completion (index_state='complete', stamped AFTER the last
    // manifest write and ONLY once verified) that a derived comparison cannot
    // fake.
    //
    // FENCE IS NOT A LOCK (memo §5, nexus-lcmbp non-goal): 'indexing' always
    // means "re-index"; concurrency remains the pipeline row's job. Nothing
    // here refuses a /begin against an already-'indexing' doc.
    // ══════════════════════════════════════════════════════════════════════════

    /** {@code nexus.manifest_verify}/{@code manifest_verify_all} result — see catalog-020-3/-4. */
    public record ManifestVerifyCounts(long referenced, long present, long missing) {}

    /**
     * Refused completion (nexus-5xn3k.2, HARD spec amendment T2 21350 —
     * substantive-critic on .1): {@code completeIndexRun} refuses when EITHER
     * {@code missing > 0} OR {@code referenced != chunkCount} (the run's own
     * claimed count). {@code missing > 0} alone is fail-OPEN: a run whose
     * manifest writes ALL failed while chunks landed in T3 yields
     * referenced=0/missing=0 — the memo's own §1 empty-manifest case
     * recreated one layer up — and would stamp a zero-content document
     * 'complete' if only {@code missing} were checked.
     */
    public static final class IndexRunVerifyRefused extends IllegalStateException {
        private static final long serialVersionUID = 1L;
        public final String docId;
        public final long referenced;
        public final long present;
        public final long missing;
        public final int chunkCount;

        IndexRunVerifyRefused(String docId, long referenced, long present, long missing, int chunkCount) {
            super("completeIndexRun refused: doc_id=" + docId + " referenced=" + referenced
                  + " present=" + present + " missing=" + missing + " claimed_chunk_count=" + chunkCount);
            this.docId = docId;
            this.referenced = referenced;
            this.present = present;
            this.missing = missing;
            this.chunkCount = chunkCount;
        }
    }

    /** Ctx-level {@code nexus.manifest_verify(docId)} call — shared by {@link #manifestVerify},
     *  {@link #completeIndexRun}, and {@code writeManifestMany}'s complete-map stamp so all
     *  three read the SAME SQL primitive under the SAME transaction semantics. */
    private static ManifestVerifyCounts manifestVerifyCtx(DSLContext ctx, String docId) {
        var r = ctx.selectFrom(MANIFEST_VERIFY.call(docId)).fetchOne();
        return new ManifestVerifyCounts(
            r.get(MANIFEST_VERIFY.REFERENCED), r.get(MANIFEST_VERIFY.PRESENT), r.get(MANIFEST_VERIFY.MISSING));
    }

    /**
     * nexus-5xn3k.2 (stacked-review item 1): per-(tenant, doc_id) advisory xact
     * lock serializing the index-run fence's verify-then-stamp
     * ({@link #completeIndexRun}, {@link #stampCompleteIfVerified}) against a
     * CONCURRENT manifest write ({@link #writeManifestRows}, hence {@link
     * #writeManifest}/{@link #writeManifestMany}/{@link #appendManifestChunks})
     * for the SAME document. Follows the {@code pg_advisory_xact_lock(hashtext(...))}
     * idiom in {@code RekeyOps.rekey}/{@code StagingPromoteOps.promoteCollection}
     * (RekeyOps.java:101, StagingPromoteOps.java:126) — xact-scoped (auto-released
     * at commit/rollback, no explicit unlock needed) and safely re-entrant: two
     * acquisitions of the same key by the SAME transaction never self-deadlock.
     *
     * <p><strong>What this protects against.</strong> {@code manifest_verify()}'s
     * SELECT and {@code completeIndexRun}'s final UPDATE run under READ COMMITTED,
     * so a concurrent manifest write that commits BETWEEN the verify read and the
     * stamp is invisible to the verify count that the stamp is conditioned on —
     * without this lock, {@code completeIndexRun} could stamp {@code 'complete'}
     * against a manifest state a concurrent write has already superseded.
     * Acquiring this lock BEFORE the verify read on BOTH sides forces total
     * ordering: whichever side gets there first runs its entire critical section
     * (read + write) to completion (transaction commit releases the lock) before
     * the other side's acquisition succeeds — so the loser's verify read always
     * reflects the winner's fully-committed effect. This is defense-in-depth: the
     * client's begin-before-first-upsert / complete-after-last-write ordering
     * (memo §3.5) already closes the window in the intended single-writer-per-doc
     * pipeline; this lock is the belt for a MIS-sequenced or out-of-band writer.
     */
    private static void acquireIndexRunLock(DSLContext ctx, String tenant, String docId) {
        // SANCTIONED RAW (nexus-5xn3k.2): advisory-lock primitive, no jOOQ
        // DSL form — registered in RawSqlGateTest.SANCTIONED_METHODS.
        ctx.execute("SELECT pg_advisory_xact_lock(hashtext('indexrun:' || ? || ':' || ?))",
                    tenant, docId);
    }

    /** GET /v1/catalog/manifest/verify?doc_id=X primitive — per-document referenced/present/missing. */
    public ManifestVerifyCounts manifestVerify(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> manifestVerifyCtx(ctx, docId));
    }

    /**
     * GET /v1/catalog/manifest/verify_all primitive — every live document in the
     * tenant, grouped by collection (nexus-ac4id part 2: replaces client-side
     * per-collection T3 paging with one engine-side anti-join).
     */
    public List<Map<String, Object>> manifestVerifyAll(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectFrom(MANIFEST_VERIFY_ALL.call()).fetch().map(r -> {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("collection", r.get(MANIFEST_VERIFY_ALL.COLLECTION));
                m.put("referenced", r.get(MANIFEST_VERIFY_ALL.REFERENCED));
                m.put("present",    r.get(MANIFEST_VERIFY_ALL.PRESENT));
                m.put("missing",    r.get(MANIFEST_VERIFY_ALL.MISSING));
                return m;
            }));
    }

    /**
     * POST /v1/catalog/index-run/begin — idempotent. Stamps
     * {@code index_state='indexing'} BEFORE any chunk work (memo §3.5 T0):
     * the client calls this first, so the fence is committed before the
     * first byte of content and cleared only after the last (§3.5's
     * "no gap" argument). Calling it again (retry, or a second concurrent
     * run — the fence is NOT a lock) simply re-stamps the same shape; there
     * is no conflict/skip behaviour here by design (nexus-lcmbp non-goal).
     *
     * <p>{@code collection} is accepted for observability only — the fence
     * columns (catalog-020) carry no collection of their own (that lives on
     * {@code physical_collection}, set by the register/update path); it is
     * logged so a begin/complete/fail triple can be correlated in the logs
     * without a DB round-trip.
     */
    public void beginIndexRun(String tenant, String docId, String contentHash, String runId, String collection) {
        log.debug("event=index_run_begin tenant={} doc_id={} run_id={} collection={}",
                   tenant, docId, runId, collection);
        tenantScope.withTenant(tenant, ctx -> {
            int updated = ctx.update(CATALOG_DOCUMENTS)
               .set(CATALOG_DOCUMENTS.INDEX_STATE, "indexing")
               .set(CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, nne(contentHash))
               .set(CATALOG_DOCUMENTS.INDEX_RUN_ID, nne(runId))
               .set(CATALOG_DOCUMENTS.INDEX_STARTED_AT,
                    java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC).format(INDEXED_AT_FMT))
               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                      .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .execute();
            if (updated == 0) {
                if (isTombstonedDocument(ctx, tenant, docId)) {
                    throw new TombstonedDocumentException(docId,
                        "beginIndexRun refused: document is tombstoned: " + docId);
                }
                // nexus-5xn3k.2 (stacked-review item 3, the critic's identity-mismatch
                // class): a 0-row update that is NOT a tombstone refusal means doc_id
                // was never registered — a silent no-op here would hide a client/engine
                // doc_id mismatch. Signal it; still a no-op (the long-standing contract
                // for an unknown tumbler stays a no-op, not a thrown exception).
                log.warn("event=index_run_begin_unknown_doc tenant={} doc_id={}", tenant, docId);
            }
            return null;
        });
    }

    /**
     * POST /v1/catalog/index-run/begin-many — batch {@code index_state='indexing'}
     * stamp for N documents in ONE HTTP round trip (nexus-vw594 F1). Same
     * idempotent, NOT-a-lock semantics as {@link #beginIndexRun} — each doc
     * gets its own {@link #beginIndexRun} call (its own transaction), so a
     * single bad {@code doc_id} in the batch does not abort the rest
     * (writeManifestMany's per-doc isolation pattern). The round trip this
     * closes is the HTTP one: the ChunkBatcher flush-grain repo path
     * (indexer.py) stages an entire upload batch's worth of files and would
     * otherwise pay one {@code /index-run/begin} round trip PER FILE instead
     * of once per FLUSH.
     *
     * <p>{@code collection} is batch-wide (one ChunkBatcher flush is always a
     * single collection) and, like {@link #beginIndexRun}'s parameter, is
     * accepted for log correlation only.
     *
     * @return {@code {docs: <succeeded count>, failed_doc_ids: [...]}}
     */
    public Map<String, Object> beginIndexRunMany(String tenant, List<Map<String, Object>> docs, String collection) {
        int ok = 0;
        List<String> failed = new ArrayList<>();
        if (docs != null) {
            for (Map<String, Object> d : docs) {
                String docId = s(d, "doc_id");
                String contentHash = s(d, "content_hash");
                String runId = s(d, "run_id");
                try {
                    if (docId == null || docId.isBlank()) {
                        throw new IllegalArgumentException("'doc_id' required");
                    }
                    beginIndexRun(tenant, docId, contentHash, runId, collection);
                    ok++;
                } catch (Exception e) {
                    log.warn("event=index_run_begin_many_doc_failed tenant={} doc_id={} error={}",
                              tenant, docId, e.getMessage());
                    failed.add(docId == null ? "" : docId);
                }
            }
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("docs", ok);
        result.put("failed_doc_ids", failed);
        return result;
    }

    /**
     * POST /v1/catalog/index-run/complete — the load-bearing FAIL-CLOSED
     * verify-then-stamp (memo §3.3, amended by the .1 critique, T2 21350).
     * In ONE transaction: run {@code nexus.manifest_verify(docId)}; refuse
     * ({@link IndexRunVerifyRefused}, mapped to HTTP 409) leaving
     * {@code index_state} UNTOUCHED when {@code missing > 0} OR
     * {@code referenced != chunkCount} (the caller's claimed count). Only
     * when BOTH hold does the UPDATE stamp
     * {@code index_state='complete', index_content_hash, chunk_count}.
     *
     * <p>A {@code /complete} with no prior {@code /begin} is ACCEPTED but
     * FLAGGED (memo §3.3 / bead): legacy documents and out-of-band writers
     * must be able to converge. Flagged means the prior {@code index_state}
     * was not {@code 'indexing'} (NULL/unknown, 'complete', or 'failed') —
     * logged at WARNING and surfaced in the response as {@code "flagged"}.
     *
     * @return {@code {referenced, present, missing, flagged}} on success
     * @throws IndexRunVerifyRefused on the fail-closed refusal (409)
     */
    public Map<String, Object> completeIndexRun(String tenant, String docId, String contentHash, int chunkCount) {
        return tenantScope.withTenant(tenant, ctx -> {
            // nexus-5xn3k.2 (stacked-review item 1): serialize against a concurrent
            // manifest write for the SAME doc BEFORE the verify read — see
            // acquireIndexRunLock's javadoc for the exact race this closes.
            acquireIndexRunLock(ctx, tenant, docId);

            // nexus-mqd6t-class tombstone read guard (TombstoneFilterGateTest): a
            // tombstoned doc reads priorState=null here (same as "not found") — the
            // guarded UPDATE below still refuses the whole call for a tombstoned
            // target via TombstonedDocumentException, so this filter changes no
            // observable outcome, only satisfies the read-guard invariant.
            String priorState = ctx.select(CATALOG_DOCUMENTS.INDEX_STATE)
                .from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                       .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                .fetchOne(CATALOG_DOCUMENTS.INDEX_STATE);
            ManifestVerifyCounts counts = manifestVerifyCtx(ctx, docId);
            if (counts.missing() > 0 || counts.referenced() != chunkCount) {
                throw new IndexRunVerifyRefused(docId, counts.referenced(), counts.present(), counts.missing(), chunkCount);
            }
            boolean flagged = !"indexing".equals(priorState);
            if (flagged) {
                log.warn("event=index_run_complete_without_begin tenant={} doc_id={} prior_state={}",
                          tenant, docId, priorState);
            }
            int updated = ctx.update(CATALOG_DOCUMENTS)
               .set(CATALOG_DOCUMENTS.INDEX_STATE, "complete")
               .set(CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, nne(contentHash))
               .set(CATALOG_DOCUMENTS.CHUNK_COUNT, chunkCount)
               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                      .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .execute();
            if (updated == 0) {
                if (isTombstonedDocument(ctx, tenant, docId)) {
                    throw new TombstonedDocumentException(docId,
                        "completeIndexRun refused: document is tombstoned: " + docId);
                }
                // nexus-5xn3k.2 (stacked-review item 3): doc_id was never registered
                // (priorState read above was also null for the same reason) — signal
                // the identity mismatch rather than silently reporting success.
                log.warn("event=index_run_complete_unknown_doc tenant={} doc_id={}", tenant, docId);
            }
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("referenced", counts.referenced());
            out.put("present",    counts.present());
            out.put("missing",    counts.missing());
            out.put("flagged",    flagged);
            return out;
        });
    }

    /**
     * POST /v1/catalog/index-run/fail — stamps {@code index_state='failed'}.
     * The error string is NOT persisted on the row (catalog-020 added no
     * error column — the fence's four columns are state/hash/run_id/started_at
     * only); house style for this is a structured log line, recorded
     * unconditionally (even if the DB stamp itself is refused below) so the
     * failure reason is never silently dropped.
     */
    public void failIndexRun(String tenant, String docId, String error) {
        log.warn("event=index_run_failed tenant={} doc_id={} error={}", tenant, docId, nne(error));
        tenantScope.withTenant(tenant, ctx -> {
            int updated = ctx.update(CATALOG_DOCUMENTS)
               .set(CATALOG_DOCUMENTS.INDEX_STATE, "failed")
               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                      .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .execute();
            if (updated == 0) {
                if (isTombstonedDocument(ctx, tenant, docId)) {
                    throw new TombstonedDocumentException(docId,
                        "failIndexRun refused: document is tombstoned: " + docId);
                }
                // nexus-5xn3k.2 (stacked-review item 3): identity-mismatch signal,
                // same reasoning as beginIndexRun/completeIndexRun above.
                log.warn("event=index_run_fail_unknown_doc tenant={} doc_id={}", tenant, docId);
            }
            return null;
        });
    }

    /**
     * {@code writeManifestMany}'s optional per-doc completion stamp (memo
     * §3.3): the SAME fail-closed check {@link #completeIndexRun} runs
     * (missing==0 AND referenced==chunkCount), but run INSIDE the per-doc
     * transaction {@code writeManifestRows} already opened for the hot
     * flush-grain repo path — no extra round trip. Unlike
     * {@code completeIndexRun}, a failed verify here does NOT throw (which
     * would roll back the manifest rows just written, which ARE correct —
     * over-work-never-under-work, memo §3.5): it logs a WARNING, skips the
     * stamp (index_state is left whatever it was), and the caller collects
     * it into the response's {@code complete_refused} list.
     */
    private static void stampCompleteIfVerified(DSLContext ctx, String tenant, String docId,
                                                 String contentHash, int chunkCount,
                                                 List<Map<String, Object>> refusedOut) {
        // nexus-5xn3k.2 (stacked-review item 1): same lock as completeIndexRun.
        // Redundant in the ONLY current call site (writeManifestMany calls this
        // in the SAME ctx/transaction as writeManifestRows, which acquires the
        // same key first — re-acquiring within one transaction is a safe no-op),
        // but self-contained here so this method stays correct if ever called
        // from a path that does not run writeManifestRows first.
        acquireIndexRunLock(ctx, tenant, docId);
        ManifestVerifyCounts counts = manifestVerifyCtx(ctx, docId);
        if (counts.missing() > 0 || counts.referenced() != chunkCount) {
            log.warn("event=write_manifest_many_complete_refused tenant={} doc_id={} referenced={} "
                      + "present={} missing={} chunk_count={}",
                      tenant, docId, counts.referenced(), counts.present(), counts.missing(), chunkCount);
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("doc_id", docId);
            r.put("referenced", counts.referenced());
            r.put("missing", counts.missing());
            r.put("chunk_count", chunkCount);
            refusedOut.add(r);
            return;
        }
        int updated = ctx.update(CATALOG_DOCUMENTS)
           .set(CATALOG_DOCUMENTS.INDEX_STATE, "complete")
           .set(CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, nne(contentHash))
           .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
                  .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
           .execute();
        // DEFENSIVE / UNREACHABLE in practice (stacked-review item 7): the ONLY
        // caller (writeManifestMany) always runs writeManifestRows in this SAME
        // transaction FIRST, and writeManifestRows's own eldyi guard already
        // throws TombstonedDocumentException for a tombstoned docId before
        // control ever reaches here — so `updated == 0` from a tombstoned target
        // cannot actually occur at this call site. Kept for safety in case a
        // future caller invokes this method without writeManifestRows preceding
        // it in the same transaction.
        if (updated == 0 && isTombstonedDocument(ctx, tenant, docId)) {
            throw new TombstonedDocumentException(docId,
                "writeManifestMany complete-map refused: document is tombstoned: " + docId);
        }
    }

    /**
     * Get chashes for a physical_collection via manifest join.
     *
     * <p>nexus-mqd6t BUG 1: this is the T3 GC ALIVE-SET (commands/t3.py). It
     * joined {@code catalog_documents} but filtered only on
     * {@code physical_collection}, so a tombstoned document's chunks stayed in
     * the returned set and {@code nx t3 gc} treated their vectors as still
     * referenced — a permanent, silent under-collection.
     */
    public Set<String> chashesForCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.selectDistinct(CHK_CHASH_HEX)
                          .from(CATALOG_DOCUMENT_CHUNKS)
                          .join(CATALOG_DOCUMENTS).on(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(CATALOG_DOCUMENTS.TENANT_ID)
                                           .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(CATALOG_DOCUMENTS.TUMBLER)))
                          .where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(collection)
                                 .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                          .fetch();
            Set<String> result = new LinkedHashSet<>();
            for (var r : rows) result.add(r.value1());
            return result;
        });
    }

    /**
     * Get document tumblers that contain any of the given chashes.
     *
     * <p>nexus-mqd6t BUG 2 (the user-visible one): this backs search-hit
     * attribution (search_engine.py maps result chunks back to documents). It
     * did not reference {@code catalog_documents} at all, so a hit could be
     * attributed to a document the user had deleted.
     */
    public List<String> docsForChashes(String tenant, List<String> chashes) {
        if (chashes.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectDistinct(CATALOG_DOCUMENT_CHUNKS.DOC_ID).from(CATALOG_DOCUMENT_CHUNKS)
               .where(CHK_CHASH_HEX.in(chashes).and(liveParentDoc(ctx, tenant)))
               .fetch().map(r -> r.value1())
        );
    }

    /**
     * Batch-fetch manifest rows for multiple doc_ids (nexus-7lm3q).
     *
     * <p>Executes {@code SELECT ... FROM catalog_document_chunks WHERE doc_id IN (?)}
     * once for all requested doc_ids, returning a per-doc-id map of manifest rows.
     * Doc_ids with no rows are absent from the result map. Mirrors the shape of
     * {@link #getManifest} but for N docs in one DB round-trip instead of N round-trips.
     *
     * @return {@code {docId -> [manifest rows ordered by position]}}
     */
    public Map<String, List<Map<String, Object>>> getManifestMany(String tenant, List<String> docIds) {
        if (docIds == null || docIds.isEmpty()) return Map.of();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CHK_CHASH_HEX, CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX,
                                  CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END, CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END)
                          .from(CATALOG_DOCUMENT_CHUNKS)
                          // nexus-mqd6t: batch twin of getManifest's tombstone filter.
                          // Load-bearing beyond parity — HttpCatalogClient.docs_for_chashes
                          // reconstructs its chash -> doc_id map from THIS read.
                          .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.in(docIds).and(liveParentDoc(ctx, tenant)))
                          .orderBy(CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION)
                          .fetch();
            Map<String, List<Map<String, Object>>> result = new LinkedHashMap<>();
            for (var r : rows) {
                String docId = r.value1();
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("doc_id",      docId);
                m.put("position",    r.value2());
                m.put("chash",       r.value3());
                m.put("chunk_index", r.value4());
                m.put("line_start",  r.value5());
                m.put("line_end",    r.value6());
                m.put("char_start",  r.value7());
                m.put("char_end",    r.value8());
                result.computeIfAbsent(docId, k -> new ArrayList<>()).add(m);
            }
            return result;
        });
    }

    /**
     * Batch-fetch just the chash list for multiple doc_ids, ordered by position
     * (nexus-eslkl / T2 nexus/design-eslkl-hook-lock-narrowing §8.1). The
     * batched twin of the superseded-vector sweep's per-doc "before" read
     * ({@code reader.get_chunk_chashes(doc_id)}, {@code mcp_infra.py:1465}):
     * collapses the client's N before-reads (one per flushed doc) into ONE
     * round trip per flush, mirroring {@link #getManifestMany}'s shape but
     * chash-only — the sweep's before-read needs nothing else (no position,
     * no line/char offsets).
     *
     * @return {@code {docId -> [chash, ...]}} ordered by position; doc_ids with
     *         no manifest rows are absent from the result map.
     */
    public Map<String, List<String>> getChunkChashesMany(String tenant, List<String> docIds) {
        if (docIds == null || docIds.isEmpty()) return Map.of();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(CATALOG_DOCUMENT_CHUNKS.DOC_ID, CHK_CHASH_HEX)
                          .from(CATALOG_DOCUMENT_CHUNKS)
                          // Same tombstone filter as getManifestMany/getManifest — a
                          // sweep's before-read must not see a tombstoned doc's rows
                          // as "still live" candidates.
                          .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.in(docIds).and(liveParentDoc(ctx, tenant)))
                          .orderBy(CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION)
                          .fetch();
            Map<String, List<String>> result = new LinkedHashMap<>();
            for (var r : rows) {
                String docId = r.value1();
                String chash = r.value2();
                if (chash == null || chash.isBlank()) continue;
                result.computeIfAbsent(docId, k -> new ArrayList<>()).add(chash);
            }
            return result;
        });
    }

    /**
     * Batch-resolve multiple doc_ids to full document entries (nexus-7lm3q).
     *
     * <p>Executes {@code SELECT ... FROM catalog_documents WHERE tumbler IN (?)}
     * once for all requested doc_ids, returning a per-doc-id map of document rows
     * (same shape as {@link #getDocument}). Doc_ids with no matching document are
     * absent from the result map.
     *
     * @return {@code {docId -> document row dict}}
     */
    public Map<String, Map<String, Object>> resolveMany(String tenant, List<String> docIds) {
        if (docIds == null || docIds.isEmpty()) return Map.of();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(documentFields())
                          .from(CATALOG_DOCUMENTS)
                          .where(CATALOG_DOCUMENTS.TUMBLER.in(docIds).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                          .fetch();
            Map<String, Map<String, Object>> result = new LinkedHashMap<>();
            for (var r : rows) {
                Map<String, Object> doc = docRowFromRecord(r.intoMap());
                String tumbler = (String) doc.get("tumbler");
                if (tumbler != null) result.put(tumbler, doc);
            }
            return result;
        });
    }

    /**
     * Resync chunk_count on catalog_documents from manifest row count.
     *
     * <p>nexus-mqd6t (review M1 / critique C3): both halves are tombstone-scoped.
     * The COUNT carries {@code liveParentDoc} for consistency with every other
     * manifest-rooted read; the UPDATE carries {@code deleted_at IS NULL} so a
     * resync cannot write through to a tombstoned row — the same
     * non-resurrection rule {@code buildUpdateDocumentQuery} enforces. Without
     * the write guard this was the one remaining path that mutated a
     * soft-deleted document.
     */
    public int resyncChunkCount(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> {
            int count = ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                           .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId).and(liveParentDoc(ctx, tenant)))
                           .fetchOne(0, Integer.class);
            return ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.CHUNK_COUNT, count)
                      .where(CATALOG_DOCUMENTS.TUMBLER.eq(docId)
                             .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())).execute();
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // SPAN / CHASH RESOLUTION  (nexus-njrcn.4)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Resolve a chash within a specific collection to chunk_text + metadata.
     *
     * <p>Queries {@code nexus.chunks_768}, {@code nexus.chunks_384}, and
     * {@code nexus.chunks_1024} in sequence (first match wins). The chash
     * must be the full-digest natural ID (chunk_text_hash, RDR-180) — the same
     * convention used by the catalog_document_chunks manifest (RDR-108 D1).
     *
     * <p>RLS auto-scopes to the caller's tenant via {@code TenantScope.withTenant}.
     *
     * @param tenant     tenant identifier
     * @param collection physical collection name (e.g. {@code knowledge__o__bge-768__v1})
     * @param chash      64-hex chash (the full chunk_text_hash, RDR-180)
     * @return {@code {chunk_text, metadata, chunk_hash}} or {@code null} on miss
     */
    public Map<String, Object> resolveSpan(String tenant, String collection, String chash) {
        return tenantScope.withTenant(tenant, ctx -> {
            // Query the three dim tables in order; stop at first hit.
            // Raw SQL UNION ALL would need casting across schemas; sequential jOOQ
            // selects with early-return is cleaner and avoids cross-table JOIN complexity.
            var r768 = ctx.select(CHUNKS_768.CHUNK_TEXT, CHUNKS_768.METADATA)
                          .from(CHUNKS_768)
                          .where(CHUNKS_768.COLLECTION.eq(collection).and(C768_CHASH_HEX.eq(chash)))
                          .limit(1).fetchOne();
            if (r768 != null) return chunkRow(chash, r768.value1(), r768.value2());

            var r384 = ctx.select(CHUNKS_384.CHUNK_TEXT, CHUNKS_384.METADATA)
                          .from(CHUNKS_384)
                          .where(CHUNKS_384.COLLECTION.eq(collection).and(C384_CHASH_HEX.eq(chash)))
                          .limit(1).fetchOne();
            if (r384 != null) return chunkRow(chash, r384.value1(), r384.value2());

            var r1024 = ctx.select(CHUNKS_1024.CHUNK_TEXT, CHUNKS_1024.METADATA)
                           .from(CHUNKS_1024)
                           .where(CHUNKS_1024.COLLECTION.eq(collection).and(C1024_CHASH_HEX.eq(chash)))
                           .limit(1).fetchOne();
            if (r1024 != null) return chunkRow(chash, r1024.value1(), r1024.value2());
            return null;
        });
    }

    /**
     * Resolve a chash globally (across all collections), with optional tie-break.
     *
     * <p>Executes a {@code UNION ALL} across the three dim tables filtering on
     * {@code chash = ?}, ordered so {@code prefer_collection} sorts first, then
     * newest {@code created_at}. Takes the winning row, then looks up
     * {@code doc_id} from {@code catalog_document_chunks}.
     *
     * <p>RLS auto-scopes to the caller's tenant via {@code TenantScope.withTenant}.
     *
     * @param tenant            tenant identifier
     * @param chash             64-hex chash (the full digest, RDR-180)
     * @param preferCollection  preferred collection name (may be null)
     * @return {@code {chash, chunk_hash, physical_collection, doc_id, chunk_text,
     *         metadata}} or {@code null} on miss
     */
    public Map<String, Object> resolveChash(String tenant, String chash, String preferCollection) {
        return tenantScope.withTenant(tenant, ctx -> {
            // UNION ALL across the three dim tables, wrapped as a derived table so the
            // outer ORDER BY can reference expressions (PostgreSQL rejects expressions
            // in a bare UNION's ORDER BY; a derived-table FROM avoids that restriction).
            // prefer_collection is a bind parameter via the typed .eq(pref) predicate below.
            String pref = preferCollection != null ? preferCollection : "";

            var sub = ctx.select(CHUNKS_768.COLLECTION, CHUNKS_768.CHUNK_TEXT, CHUNKS_768.METADATA, CHUNKS_768.CREATED_AT)
                          .from(CHUNKS_768).where(C768_CHASH_HEX.eq(chash))
                       .unionAll(
                          ctx.select(CHUNKS_384.COLLECTION, CHUNKS_384.CHUNK_TEXT, CHUNKS_384.METADATA, CHUNKS_384.CREATED_AT)
                             .from(CHUNKS_384).where(C384_CHASH_HEX.eq(chash)))
                       .unionAll(
                          ctx.select(CHUNKS_1024.COLLECTION, CHUNKS_1024.CHUNK_TEXT, CHUNKS_1024.METADATA, CHUNKS_1024.CREATED_AT)
                             .from(CHUNKS_1024).where(C1024_CHASH_HEX.eq(chash)))
                       .asTable("sub");

            Field<String>         col       = sub.field("collection", String.class);
            Field<String>         text      = sub.field("chunk_text", String.class);
            Field<org.jooq.JSONB> meta      = sub.field("metadata", org.jooq.JSONB.class);
            Field<java.time.OffsetDateTime> createdAt = sub.field("created_at", java.time.OffsetDateTime.class);

            var row = ctx.select(col, text, meta, createdAt)
                          .from(sub)
                          // Third key `collection ASC` matches the canonical _sort_key
                          // (preferred, newest created_at, deterministic name) so a chash in two
                          // collections with equal created_at resolves stably (njrcn.4 review).
                          .orderBy(col.eq(pref).desc(), createdAt.desc(), col.asc())
                          .limit(1)
                          .fetchOne();
            if (row == null) return null;

            String colVal      = row.value1();
            String textVal     = row.value2();
            org.jooq.JSONB metaVal = row.value3();

            // Lookup doc_id from catalog_document_chunks. ORDER BY doc_id for a
            // deterministic winner when a chash is referenced by multiple docs (dedup).
            //
            // nexus-mqd6t (review H1 / critique C3): the FIFTH sibling of the
            // docsForChashes class, and the same user-visible shape — without
            // liveParentDoc this attributes chunk content to a document the
            // user DELETED. Worse than the plain read case: a chash shared by a
            // live and a tombstoned doc has no live-preference in `ORDER BY
            // doc_id ASC`, so the dead doc could win the tie outright.
            String docId = "";
            var docRow = ctx.select(CATALOG_DOCUMENT_CHUNKS.DOC_ID).from(CATALOG_DOCUMENT_CHUNKS)
                            .where(CHK_CHASH_HEX.eq(chash).and(liveParentDoc(ctx, tenant)))
                            .orderBy(CATALOG_DOCUMENT_CHUNKS.DOC_ID.asc()).limit(1).fetchOne();
            if (docRow != null) docId = docRow.value1();

            Map<String, Object> m = new LinkedHashMap<>();
            m.put("chash",               chash);
            m.put("chunk_hash",          chash);
            m.put("physical_collection", colVal);
            m.put("doc_id",              docId);
            m.put("chunk_text",          textVal);
            m.put("metadata",            metaVal != null ? parseMetaJson(metaVal.data()) : Map.of());
            return m;
        });
    }

    /** Build a span-resolution result map from chunk row values. */
    private static Map<String, Object> chunkRow(String chash, String chunkText,
                                                  org.jooq.JSONB metadata) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("chunk_text",  chunkText);
        m.put("metadata",    metadata != null ? parseMetaJson(metadata.data()) : Map.of());
        m.put("chunk_hash",  chash);
        return m;
    }

    /** Parse a JSON metadata string into a Map. Returns empty map on null/error. */
    private static Map<String, Object> parseMetaJson(String json) {
        if (json == null || json.isBlank()) return Map.of();
        try {
            return MAPPER.readValue(json, MAP_TYPE);
        } catch (Exception e) {
            return Map.of();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // COLLECTIONS
    // ══════════════════════════════════════════════════════════════════════════

    /** Upsert a collection. */
    public void upsertCollection(String tenant, Map<String, Object> coll) {
        tenantScope.withTenant(tenant, ctx -> {
            // nexus-xtmtf: superseded_at / created_at are timestamptz NULL columns after
            // catalog-002-1-temporal-typing (RDR-156 P0.2). Parse the ISO-8601-or-empty
            // strings to OffsetDateTime in Java (blank -> NULL) and bind the generated
            // typed fields — no ?::timestamptz cast, no raw SQL.
            ctx.insertInto(CATALOG_COLLECTIONS,
                    CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                    CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                    CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                    CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED,
                    CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_AT,
                    CATALOG_COLLECTIONS.CREATED_AT)
               .values(tenant,
                       s(coll, "name"), nne(s(coll, "content_type")),
                       nne(s(coll, "owner_id")), nne(s(coll, "embedding_model")),
                       nne(s(coll, "model_version")), nne(s(coll, "display_name")),
                       ni(i(coll, "legacy_grandfathered"), 0),
                       nne(s(coll, "superseded_by")), tsOrNull(s(coll, "superseded_at")),
                       tsOrNull(s(coll, "created_at")))
               .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .doUpdate()
               .set(CATALOG_COLLECTIONS.CONTENT_TYPE,         DSL.excluded(CATALOG_COLLECTIONS.CONTENT_TYPE))
               .set(CATALOG_COLLECTIONS.OWNER_ID,             DSL.excluded(CATALOG_COLLECTIONS.OWNER_ID))
               .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL,      DSL.excluded(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
               .set(CATALOG_COLLECTIONS.MODEL_VERSION,        DSL.excluded(CATALOG_COLLECTIONS.MODEL_VERSION))
               .set(CATALOG_COLLECTIONS.DISPLAY_NAME,         DSL.excluded(CATALOG_COLLECTIONS.DISPLAY_NAME))
               .set(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, DSL.excluded(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED))
               // nexus-cecqy: an explicit registration REVIVES a tombstone. Since a rename
               // now retires the old name instead of deleting it, re-creating a collection
               // under that name would otherwise land on a row still marked superseded —
               // and superseded rows are excluded from collectionForTuple, so the live
               // collection would be unreachable as a write target while `nx catalog
               // doctor --collections-drift` stayed quiet (it deliberately permits a
               // superseded row to be absent from T3). /collections/upsert is the caller
               // asserting "this collection exists and is current"; a caller that wants it
               // retired says so via /collections/supersede.
               .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "")
               .set(CATALOG_COLLECTIONS.SUPERSEDED_AT, (java.time.OffsetDateTime) null)
               .execute();
            return null;
        });
    }

    /** Get a collection by name. Returns null if not found. */
    public Map<String, Object> getCollection(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                               CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, CATALOG_COLLECTIONS.SUPERSEDED_BY, F_COL_SUPAT, F_COL_CRTAT)
                       .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne();
            return r != null ? collRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(),
                                        r.value6(), r.value7(), r.value8(), r.value9(), r.value10()) : null;
        });
    }

    /** List all collections. */
    public List<Map<String, Object>> listCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                       CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, CATALOG_COLLECTIONS.SUPERSEDED_BY, F_COL_SUPAT, F_COL_CRTAT)
               .from(CATALOG_COLLECTIONS).orderBy(CATALOG_COLLECTIONS.NAME).fetch()
               .map(r -> collRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(),
                                  r.value6(), r.value7(), r.value8(), r.value9(), r.value10()))
        );
    }

    /**
     * Atomically delete a collection and ALL its derived in-Postgres state in ONE
     * tenant-scoped transaction (RDR-164 P2). Replaces the SQLite-era client-side
     * {@code purge_collection_cascade} fan-out for the service path: the explicit
     * ordered DELETE removes every lifecycle table's rows in dependency order, with
     * the {@code catalog_collections} registry row LAST so the {@code ON DELETE
     * RESTRICT} child FKs (fk-002 / fk-003) act as a safety net rather than a blocker.
     *
     * <p>Order (children → registry): chunks_* → chash_index → topic_assignments →
     * topics → taxonomy_centroids_* → document_aspects → document_highlights →
     * aspect_extraction_queue → catalog_documents (fk-001 cascades any doc-rooted
     * aspect/highlight/queue/manifest remainder) → catalog_collections.
     *
     * <p>This is where RDR-164 closes <strong>nexus-tquoj</strong> (the client cascade
     * never purged {@code aspect_extraction_queue}; the explicit DELETE here catches it,
     * including doc-less {@code doc_id=''} rows the fk-001 document cascade cannot reach)
     * and the service-mode <strong>nexus-cugrk</strong> centroid leak ({@code
     * taxonomy_centroids_*} have no FK to {@code topics}, CA-6 — purged by explicit
     * {@code DELETE WHERE collection=?} in the same txn).
     *
     * <p>RLS scopes every DELETE to the caller's tenant via the {@code nexus.tenant}
     * GUC, so a same-named collection under another tenant is untouched. Returns a
     * per-table deleted-row count map (preserves the {@code CascadeCounts} / CLI-render
     * + telemetry contract); no {@code failures} list — the operation is all-or-nothing.
     *
     * <p>Out of scope (stays client-side, RDR-164 CA-4/CA-5): the {@code pipeline.db}
     * streaming buffer and the entire local-mode (sqlite/Chroma) cascade.
     */
    public Map<String, Integer> deleteCollection(String tenant, String name) {
        Map<String, Integer> counts = deleteCollectionTxn(tenant, name);
        // Post-commit (nexus-h8rf6 wave review): the registry row is gone; a stale
        // CollectionRegistry entry would make later writers silently skip
        // re-registration if the name is reused. Same post-commit discipline as
        // markKnown — see CollectionRegistry.evict.
        CollectionRegistry.evict(tenant, name);
        return counts;
    }

    private Map<String, Integer> deleteCollectionTxn(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, Integer> counts = new LinkedHashMap<>();
            // 1. T3 chunk vectors (registry children, fk-002 RESTRICT).
            counts.put("chunks_384",  ctx.deleteFrom(CHUNKS_384).where(CHUNKS_384.COLLECTION.eq(name)).execute());
            counts.put("chunks_768",  ctx.deleteFrom(CHUNKS_768).where(CHUNKS_768.COLLECTION.eq(name)).execute());
            counts.put("chunks_1024", ctx.deleteFrom(CHUNKS_1024).where(CHUNKS_1024.COLLECTION.eq(name)).execute());
            // 2. (chash_index leg RETIRED — RDR-187/nexus-piwya.9: the router
            //    table is dropped; conexus's rdr164 cascade EXPLAIN probe
            //    retargets in lockstep with this removal.)
            // 3. taxonomy: projection assignments by source_collection (fk-002-5 RESTRICT),
            //    then topics (fk-003 RESTRICT) — deleting topics cascades any remaining
            //    assignments via topic_assignments.topic_id -> topics(id) ON DELETE CASCADE.
            counts.put("topic_assignments", ctx.deleteFrom(TOPIC_ASSIGNMENTS).where(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(name)).execute());
            counts.put("topics", ctx.deleteFrom(TOPICS).where(TOPICS.COLLECTION.eq(name)).execute());
            // 3b. taxonomy_meta (fk-003-4 RESTRICT; PK (tenant_id, collection) — explicit DELETE).
            //     topic_links clears via topics(id) ON DELETE CASCADE in step 3, so it needs no row here.
            counts.put("taxonomy_meta", ctx.deleteFrom(TAXONOMY_META).where(TAXONOMY_META.COLLECTION.eq(name)).execute());
            // 4. centroids (CA-6: no FK to topics — explicit DELETE; the cugrk fix).
            counts.put("taxonomy_centroids_384",  ctx.deleteFrom(TAXONOMY_CENTROIDS_384).where(TAXONOMY_CENTROIDS_384.COLLECTION.eq(name)).execute());
            counts.put("taxonomy_centroids_768",  ctx.deleteFrom(TAXONOMY_CENTROIDS_768).where(TAXONOMY_CENTROIDS_768.COLLECTION.eq(name)).execute());
            counts.put("taxonomy_centroids_1024", ctx.deleteFrom(TAXONOMY_CENTROIDS_1024).where(TAXONOMY_CENTROIDS_1024.COLLECTION.eq(name)).execute());
            // 5. aspect family (fk-003 RESTRICT). Explicit collection delete catches
            //    doc-less (doc_id='') rows fk-001's document cascade cannot reach — the tquoj fix.
            counts.put("document_aspects",        ctx.deleteFrom(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.COLLECTION.eq(name)).execute());
            counts.put("document_highlights",     ctx.deleteFrom(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.COLLECTION.eq(name)).execute());
            counts.put("aspect_extraction_queue", ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE).where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(name)).execute());
            // 6. catalog documents for this physical collection; fk-001 cascades any
            //    doc-rooted aspect/highlight/queue/manifest rows still present.
            // The ONLY production HARD delete of document rows. fk-001 cascades
            // the four FK children; topic_assignments (no doc-rooted FK) is
            // cleaned explicitly by this method's collection-scoped taxonomy
            // delete — a per-DOC hard path would have to do its own purge
            // (nexus-7n553 tripwire at deleteDocument).
            counts.put("catalog_documents", ctx.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(name)).execute());
            // 7. registry row LAST (RESTRICT children are now gone).
            counts.put("catalog_collections", ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(name)).execute());
            return counts;
        });
    }

    /**
     * Supersede a collection.
     *
     * <p>nz() for superseded_at: '' is invalid in the timestamptz column after
     * catalog-002-1-temporal-typing.
     *
     * <p><b>nexus-g8z8n guard 4 — the supersession is self-timestamping.</b> This used
     * to bind superseded_at ONLY from the request body, and no caller ever sent one, so
     * every supersession landed with superseded_at NULL: recorded but undatable, hence
     * unauditable. A blank/absent value now stamps {@code now()} server-side (the DB
     * clock, not the client's), matching the retired local implementation which stamped
     * {@code datetime.now(UTC)}. An explicit value still wins — backfills and migrations
     * must be able to assert a historical instant.
     *
     * <p>The three PRECONDITION guards (old registered; not already superseded to a
     * DIFFERENT target; new registered) live in {@code CatalogHandler
     * .handleCollectionSupersede}, alongside the identical handler-side guards its
     * sibling verb {@code handleCollectionRename} already carries (nexus-hz785 404,
     * nexus-gaou3 409). This method stays a pure UPDATE.
     */
    public int supersedeCollection(String tenant, String name, String supersededBy, String supersededAt) {
        // superseded_at is timestamptz NULL after catalog-002-1-temporal-typing;
        // nexus-xtmtf: typed OffsetDateTime bind (blank -> NULL), no cast.
        Field<java.time.OffsetDateTime> stampedAt = supersededAt == null || supersededAt.isBlank()
            ? DSL.currentOffsetDateTime()
            : DSL.val(tsOrNull(supersededAt));
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(CATALOG_COLLECTIONS)
               .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy)
               // nexus-0svvu (a): the WHERE's same-target disjunct below (nexus-cecqy) is
               // deliberately permissive — re-asserting the SAME target must match the
               // row, or the canonical rename's paired supersede(X, Y) call would 409 on
               // its own tombstone. That permissiveness has a cost a bare SET does not
               // pay for: two concurrent supersedes to the SAME target both match, and an
               // unconditional SET would re-stamp superseded_at on the SECOND (loser's)
               // write too — moving the supersession's recorded instant on every retry,
               // exactly what the handler's serial-path idempotence comment (guard 2,
               // above) says must never happen. Do NOT fix this by weakening the WHERE:
               // it is the single source of the DIFFERENT-target refusal, and
               // renameCollectionTxn's documented emptiness/identity asymmetry
               // (nexus-2sovp) depends on this CAS staying exactly this strict. Move the
               // guard into the SET instead — stamp only on the transition OUT OF ''
               // (a genuine first supersede); a row that already carries THIS target
               // keeps whatever instant it already has.
               .set(CATALOG_COLLECTIONS.SUPERSEDED_AT,
                    DSL.when(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(""), stampedAt)
                       .otherwise(CATALOG_COLLECTIONS.SUPERSEDED_AT))
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                   .and(CATALOG_COLLECTIONS.NAME.eq(name))
                   // nexus-cecqy (review): the handler's guard 2 reads the row, decides in
                   // Java, then issues this UPDATE as a SEPARATE statement. Two concurrent
                   // supersedes of the same old_name to DIFFERENT targets can both observe
                   // superseded_by='' before either writes, both pass the guard, and the
                   // later write silently wins — the unaudited chain rewrite guard 2 exists
                   // to prevent, just moved from serial to concurrent. Carrying the
                   // precondition in the WHERE closes that window: the loser matches zero
                   // rows and the handler's rowcount check reports it. LOAD-BEARING: do not
                   // weaken or remove this conjunct (nexus-0svvu, nexus-2sovp) — it is what
                   // makes renameCollectionTxn's own identity re-check (further below)
                   // unnecessary in the common case, since superseded_by cannot move to a
                   // THIRD value underneath an observed read once this CAS is in place.
                   .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")
                       .or(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(supersededBy))))
               .execute()
        );
    }

    /**
     * nexus-pzdol: {@code model_version} is stored as TEXT ({@code "v1"}..{@code "vN"}).
     * Ordering by {@code NAME} (or by {@code model_version} itself) lexically puts
     * {@code v10} above {@code v9} — a double-digit version silently regresses
     * max-version resolution. This strips the {@code v} prefix and casts the digits
     * to INTEGER so ordering is numeric, mirroring the retired local arm's
     * {@code MAX(CAST(SUBSTR(model_version,2) AS INTEGER))} (catalog_docs.py).
     * Malformed/legacy values (empty, no leading {@code v}) fall back to 0 rather
     * than failing the cast — {@code LEGACY_GRANDFATHERED.eq(0)} already excludes
     * the rows expected to be non-conformant, but this keeps the ORDER BY itself
     * total rather than erroring on an unexpected shape.
     */
    private static final Field<Integer> COL_VERSION_NUM = DSL.field(
        "CASE WHEN {0} ~ '^v[0-9]+$' THEN CAST(substring({0} from 2) as integer) ELSE 0 END",
        Integer.class, CATALOG_COLLECTIONS.MODEL_VERSION);

    /** Find highest-versioned collection for (content_type, owner_id, embedding_model). */
    public Map<String, Object> collectionForTuple(String tenant, String contentType,
                                                    String ownerId, String embeddingModel) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                               CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, CATALOG_COLLECTIONS.SUPERSEDED_BY, F_COL_SUPAT, F_COL_CRTAT)
                       .from(CATALOG_COLLECTIONS)
                       .where(CATALOG_COLLECTIONS.CONTENT_TYPE.eq(contentType)
                              .and(CATALOG_COLLECTIONS.OWNER_ID.eq(ownerId))
                              .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(embeddingModel))
                              .and(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED.eq(0))
                              .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")))
                       .orderBy(COL_VERSION_NUM.desc(), CATALOG_COLLECTIONS.NAME.desc())
                       .limit(1).fetchOne();
            return r != null ? collRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(),
                                        r.value6(), r.value7(), r.value8(), r.value9(), r.value10()) : null;
        });
    }

    /** True if a (tenant, name) collection registry row exists — INCLUDING a tombstone. RLS-scoped. */
    public boolean collectionExists(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx -> ctx.fetchExists(
            ctx.selectOne().from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(name))));
    }

    // nexus-v6za0: liveCollectionExists(tenant, name) lived here between 1232585d and this
    // commit. It answered a BOOLEAN — live or not — and that turned out to be the wrong
    // shape: "not live" conflates an EMPTY rename tombstone (revivable) with a POPULATED
    // supersede tombstone (merging onto it destroys data). Its two callers now read the row
    // once via getCollection and branch on superseded_by itself, which is strictly more
    // information from strictly fewer round-trips. Do not reintroduce the boolean form.

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
     *   <li>RETIRES the old registry row X as a superseded tombstone —
     *       {@code superseded_by = Y}, {@code superseded_at = now()} (nexus-cecqy; this
     *       step DELETEd X until 2026-07-31, which destroyed the rename's audit trail
     *       and made the CLI's follow-up supersede a zero-row no-op it nonetheless
     *       announced).</li>
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
        return renameCollection(tenant, oldName, newName, null);
    }

    /**
     * Same as {@link #renameCollection(String, String, String)}, plus an ADDITIVE identity
     * belt (nexus-2sovp) for callers that can supply what they observed the target's
     * {@code superseded_by} to be at their own read.
     *
     * <p>{@code expectedTargetSupersededBy} is the caller's OWN observation, not a value this
     * method derives — {@link dev.nexus.service.http.CatalogHandler#handleCollectionRename}
     * passes the value it just read (its {@code tgtSuperseded}) so the transaction verifies
     * the SAME fact the handler's identity gate already checked, in its own snapshot, rather
     * than trusting time to not have passed between the two. {@code null} means "no
     * expectation to verify" (the 3-arg overload's contract, unchanged) — existing callers
     * that never observed the target keep exactly their current behavior.
     */
    public Map<String, Integer> renameCollection(String tenant, String oldName, String newName,
                                                   String expectedTargetSupersededBy) {
        Map<String, Integer> counts = renameCollectionTxn(tenant, oldName, newName, expectedTargetSupersededBy);
        // Post-commit (nexus-h8rf6 wave review): the canonical branch RETIRES the old
        // registry row — evict it so a later same-named collection re-registers. The
        // cross-model COPY branch leaves both registry rows untouched (no key in
        // counts), so nothing is evicted there.
        //
        // nexus-cecqy: the key is catalog_collections_superseded since the retire stopped
        // being a DELETE. The eviction is still right — the row survives, so the skipped
        // INSERT ... ON CONFLICT DO NOTHING would be a no-op either way, but a caller
        // re-registering the old name must reach upsertCollection rather than be short-
        // circuited by a stale KNOWN entry.
        if (counts.containsKey("catalog_collections_superseded")) {
            CollectionRegistry.evict(tenant, oldName);
            CollectionRegistry.markKnown(tenant, newName);
        }
        return counts;
    }

    /**
     * EVERY table that carries a denormalized collection name, as (count key, table, field).
     *
     * <p>THIS LIST IS THE SINGLE SOURCE OF TRUTH for two operations that must never disagree:
     * {@code renameCollectionTxn} step 2 re-homes each entry, and {@link #collectionIsEmpty}
     * asks whether any entry holds a row. They were separate literals for one commit and
     * immediately drifted — the re-home covered 17 tables while the emptiness check covered 5,
     * so a collection holding only taxonomy or aspect rows read as EMPTY and was merged
     * (nexus-v6za0 attempt 3). Adding a table to one and not the other is now unexpressible.
     *
     * <p>Adding a denorm-collection table? Add it HERE and both operations pick it up.
     *
     * <p><strong>Equality with the SCHEMA is gated, and not from Java.</strong> Sharing this
     * list makes the two consumers equal to EACH OTHER; it does nothing about a table that is
     * missing from the list entirely — un-re-homed and invisible to the emptiness check at the
     * same time, consistent and wrong. That is not theoretical: {@code gc_audit} landed
     * 2026-07-30 and this list, written the next day, omitted it. The gate is
     * {@code tests/catalog/test_collection_scoped_tables_schema_parity.py}, which asks
     * {@code information_schema} directly. It lives in pytest because {@code service-ci} is
     * NOT a required check on develop or main (nexus-hq9na) — a Java test of this invariant
     * would be advisory at merge, which for this defect class is no gate at all (nexus-20890).
     *
     * <p>Tables deliberately NOT here, each documented with a reason in that gate's
     * {@code _DOCUMENTED_EXCLUSIONS}: {@code pdf_pipeline} (transient work queue),
     * {@code chash_remap} (permanent RF-186 ledger whose collection is inside its PK —
     * nexus-4nll0), and {@code migration_jobs} (JSONB collection SETS, which a scalar UPDATE
     * cannot rewrite — nexus-rvr1n). An exclusion that is not written down is
     * indistinguishable from an omission, so the gate fails on an undocumented one.
     */
    private static final List<CollectionScopedTable> COLLECTION_SCOPED_TABLES = List.of(
        new CollectionScopedTable("chunks_384",              CHUNKS_384,              CHUNKS_384.COLLECTION),
        new CollectionScopedTable("chunks_768",              CHUNKS_768,              CHUNKS_768.COLLECTION),
        new CollectionScopedTable("chunks_1024",             CHUNKS_1024,             CHUNKS_1024.COLLECTION),
        new CollectionScopedTable("catalog_document_chunks", CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.COLLECTION),
        new CollectionScopedTable("topic_assignments",       TOPIC_ASSIGNMENTS,       TOPIC_ASSIGNMENTS.SOURCE_COLLECTION),
        new CollectionScopedTable("topics",                  TOPICS,                  TOPICS.COLLECTION),
        new CollectionScopedTable("taxonomy_meta",           TAXONOMY_META,           TAXONOMY_META.COLLECTION),
        new CollectionScopedTable("taxonomy_centroids_384",  TAXONOMY_CENTROIDS_384,  TAXONOMY_CENTROIDS_384.COLLECTION),
        new CollectionScopedTable("taxonomy_centroids_768",  TAXONOMY_CENTROIDS_768,  TAXONOMY_CENTROIDS_768.COLLECTION),
        new CollectionScopedTable("taxonomy_centroids_1024", TAXONOMY_CENTROIDS_1024, TAXONOMY_CENTROIDS_1024.COLLECTION),
        new CollectionScopedTable("document_aspects",        DOCUMENT_ASPECTS,        DOCUMENT_ASPECTS.COLLECTION),
        new CollectionScopedTable("document_highlights",     DOCUMENT_HIGHLIGHTS,     DOCUMENT_HIGHLIGHTS.COLLECTION),
        new CollectionScopedTable("aspect_extraction_queue", ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.COLLECTION),
        new CollectionScopedTable("catalog_documents",       CATALOG_DOCUMENTS,       CATALOG_DOCUMENTS.PHYSICAL_COLLECTION),
        new CollectionScopedTable("relevance_log",           RELEVANCE_LOG,           RELEVANCE_LOG.COLLECTION),
        new CollectionScopedTable("search_telemetry",        SEARCH_TELEMETRY,        SEARCH_TELEMETRY.COLLECTION),
        new CollectionScopedTable("hook_failures",           HOOK_FAILURES,           HOOK_FAILURES.COLLECTION),
        // nexus-jqvzk, added 2026-07-30 — and MISSED by the first version of this list,
        // written 2026-07-31. Both reviewers found it independently by reading the
        // changelogs; nothing mechanical did, which is what nexus-20890's gate now fixes.
        // Same audit family as the three above, and re-homed for the same RDR-164 reason:
        // "what happened to THIS collection" (idx_gc_audit_collection) must keep answering
        // after a rename, or incident triage silently loses the collection's GC history.
        new CollectionScopedTable("gc_audit",                GC_AUDIT,                GC_AUDIT.COLLECTION));

    /** One denorm-collection table: its rename-count key, the table, and its collection column. */
    private record CollectionScopedTable(String countKey, Table<?> table, Field<String> collection) {}

    /**
     * The rename transaction refused to merge a populated retired target.
     *
     * <p>TYPED so the handler can map it to a 409 with this message intact. It was an
     * {@code IllegalStateException} for one commit, which the handler's generic catch turned
     * into {@code 500 {"error":"internal server error"}} — discarding a message that names the
     * remedy. A refusal that reads as a server bug is only half of FAIL LOUD.
     */
    public static final class CollectionMergeRefused extends RuntimeException {
        public CollectionMergeRefused(String message) { super(message); }
    }

    /** RLS-scoped {@link #collectionIsEmpty(DSLContext, String)} for callers outside a txn. */
    public boolean collectionIsEmpty(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx -> collectionIsEmpty(ctx, name));
    }

    /**
     * nexus-34wrg option (c): the four audit tables in {@link #COLLECTION_SCOPED_TABLES}
     * ({@code relevance_log}, {@code search_telemetry}, {@code hook_failures},
     * {@code gc_audit}) hold no content — they are written as a SIDE EFFECT of ordinary
     * reads and maintenance, never by a caller depositing data on purpose. A rename's
     * empty-tombstone target can read as non-empty purely because a search happened to log
     * telemetry against it, a GC pass audited it, or a hook failed against it — the refusal
     * "it still holds data" is then misleading: there is no data and no merge hazard, only
     * an audit breadcrumb. {@link #blockingTable} names WHICH table blocked so a caller can
     * tell the two apart; this set is what turns that name into an actionable distinction.
     */
    private static final java.util.Set<String> AUDIT_ONLY_TABLES =
        java.util.Set.of("relevance_log", "search_telemetry", "hook_failures", "gc_audit");

    /**
     * nexus-34wrg option (c): which table blocked an emptiness check, and whether it is one
     * of the audit-only tables (no content — see {@link #AUDIT_ONLY_TABLES}). Self-describing
     * so a caller (the rename handler, {@link #CollectionMergeRefused}) never has to know the
     * audit-table set itself to render an accurate message.
     */
    public record BlockingTable(String table, boolean auditOnly) {
        /** Human-readable clause for a refusal message: "real data in 'x'" or "an audit trail entry in 'x' (no content)". */
        public String describe() {
            return auditOnly
                ? "an audit trail entry in '" + table + "' (no content — it is written as a "
                  + "side effect of reads/maintenance, not a merge hazard)"
                : "real data in '" + table + "'";
        }
    }

    /**
     * RLS-scoped {@link #blockingTable(DSLContext, String)} for callers outside a txn
     * (nexus-34wrg option (c)).
     */
    public java.util.Optional<BlockingTable> blockingTable(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx -> blockingTable(ctx, name));
    }

    /**
     * The first table in {@link #COLLECTION_SCOPED_TABLES} (in list order) holding a row
     * for {@code name}, or empty if none does — the diagnostic form {@link
     * #collectionIsEmpty} collapses to a boolean. nexus-34wrg option (c): a bare "not empty"
     * refusal cannot tell an operator whether a real-data table is populated or only an
     * audit table logged against the name in passing; naming the table lets the caller
     * decide.
     */
    private java.util.Optional<BlockingTable> blockingTable(DSLContext ctx, String name) {
        for (CollectionScopedTable t : COLLECTION_SCOPED_TABLES) {
            if (ctx.fetchExists(ctx.selectOne().from(t.table()).where(t.collection().eq(name)))) {
                return java.util.Optional.of(
                    new BlockingTable(t.countKey(), AUDIT_ONLY_TABLES.contains(t.countKey())));
            }
        }
        return java.util.Optional.empty();
    }

    /**
     * True if {@code name} holds no row in ANY table listed in {@link #COLLECTION_SCOPED_TABLES}
     * — which is, by construction, exactly the set {@code renameCollectionTxn} step 2 re-homes.
     *
     * <p>The scope is stated as "the re-home set", NOT as "no data of any kind". An earlier
     * javadoc made the universal claim while the body enumerated five of seventeen tables,
     * and a reader trusting the sentence would not have re-checked the list. Deliberate
     * exclusions are enumerated on {@link #COLLECTION_SCOPED_TABLES} and gated by
     * {@code test_collection_scoped_tables_schema_parity.py}.
     *
     * <p>RLS-scoped, and evaluated in the CALLER's {@code ctx} so it sees that transaction's
     * uncommitted writes. It does NOT freeze a snapshot: the engine runs READ COMMITTED
     * (no isolation override in {@code TenantScope}), where every statement takes a FRESH
     * snapshot. So a concurrent committed INSERT can land between this check and the step 2
     * UPDATEs that follow it. That window is narrow and intra-transaction, but it is real —
     * an earlier version of this javadoc claimed the check "shares the caller's snapshot",
     * which would have made it look airtight. Overstating a guarantee in a comment is the
     * documented tell of all three failed attempts at this guard; do not restore it.
     *
     * <p>nexus-v6za0: this is the real precondition for reviving a retired registry row.
     * Two earlier fixes tried to infer it from {@code superseded_by} — first via liveness,
     * then via identity — and both leaked, because {@code renameCollectionTxn} step 3 and
     * {@link #supersedeCollection} write that column identically. A rename tombstone is
     * empty because its children were re-homed first; a supersede tombstone keeps
     * everything, because supersede is a pure UPDATE. The column cannot tell them apart.
     * Ask the data instead.
     */
    private boolean collectionIsEmpty(DSLContext ctx, String name) {
        return blockingTable(ctx, name).isEmpty();
    }

    private Map<String, Integer> renameCollectionTxn(String tenant, String oldName, String newName,
                                                        String expectedTargetSupersededBy) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, Integer> counts = new LinkedHashMap<>();
            // nexus-11gh6 rev 2 §3.2 (Hal Q1: gate the Java collection-move
            // paths in this bead). Both branches below bulk-repoint
            // catalog_document_chunks.collection (and the canonical branch
            // additionally repoints chunks_<dim>.collection via
            // COLLECTION_SCOPED_TABLES) from oldName to newName -- the SAME
            // hazard class as a manifest INSERT: it can make a chash appear
            // referenced in newName's scope (or vanish from oldName's) while
            // a concurrent sweep of either collection is mid-guard. Gate
            // BOTH endpoints SHARED, sorted alphabetically so two concurrent
            // renames sharing an endpoint acquire in one consistent global
            // order (no lock-order deadlock between them).
            for (String c : java.util.stream.Stream.of(oldName, newName).sorted().distinct().toList()) {
                acquireSweepGateShared(ctx, tenant, c);
            }
            // nexus-cecqy: LIVE target, not merely "a row exists". Since step 3 retires X
            // as a superseded tombstone instead of deleting it, a round-trip rename
            // (X->Y then Y->X) finds X's tombstone sitting at the target. A tombstone is
            // a retired name, not an occupied one: routing it to the RDR-162 COPY branch
            // would repoint documents only and strand the chunks, so it must take the
            // canonical branch, where step 1's upsert REVIVES it.
            // THIS IS THE AUTHORITATIVE BRANCH SELECTOR, and it now MEASURES the hazard
            // instead of inferring it. Two prior fixes inferred, and both were wrong:
            //   nexus-u4e20  guarded on "a row exists"  — could not see that a tombstone
            //                is a retired name, not an occupied one. Undo unreachable.
            //   nexus-v6za0  guarded on LIVENESS, then on IDENTITY (superseded_by ==
            //                oldName). Both are proxies for EMPTINESS and both leak:
            //                supersedeCollection is a pure UPDATE, so a supersede
            //                tombstone is non-live AND populated, and nothing stops its
            //                superseded_by naming the rename source. step 3 and
            //                supersedeCollection write the column identically, so no
            //                predicate over it can recover PROVENANCE.
            // The canonical branch's actual precondition is that the target holds NO DATA,
            // because step 1 revives the row and step 2 re-homes the source's children ON
            // TOP of whatever is already there. So ask that question, in this transaction's
            // own snapshot. A populated non-live target takes NEITHER branch: the COPY
            // branch would strand its chunks, the canonical branch would merge them.
            boolean targetRowExists = ctx.fetchExists(
                ctx.selectOne().from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(newName)));
            boolean liveTargetExists = ctx.fetchExists(
                ctx.selectOne().from(CATALOG_COLLECTIONS)
                   .where(CATALOG_COLLECTIONS.NAME.eq(newName)
                       .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(""))));
            // WHY THIS RE-CHECKS EMPTINESS BUT NOT IDENTITY, which looks asymmetric.
            // The handler checks both; this transaction re-checks only the first. That is
            // deliberate, and the reason lives in a DIFFERENT method, so state it here or
            // the next reader re-derives it (both reviewers raised this; one withdrew it
            // after tracing the mechanism — nexus-2sovp carries the analysis).
            //
            //   EMPTINESS is a fact about ROWS IN OTHER TABLES. Nothing serialises those
            //   against this rename, so a concurrent write can land between the handler's
            //   look and this transaction. It must be re-measured HERE, in the snapshot
            //   that is about to act on it.
            //
            //   IDENTITY is a fact about superseded_by on THIS row, and
            //   supersedeCollection carries its own precondition IN THE WHERE CLAUSE
            //   (SUPERSEDED_BY = '' OR SUPERSEDED_BY = :target — a DB-level
            //   compare-and-swap added for nexus-cecqy). So the column cannot be moved to
            //   a THIRD value underneath us: a concurrent supersede either matches zero
            //   rows, or writes the value it already had. The handler's read stays true.
            //
            // If that CAS is ever weakened, this asymmetry becomes a hole and the identity
            // check has to be re-checked here too. The guard below is not self-defending;
            // it is defended by a precondition in another statement.
            //
            // nexus-2sovp, ADDITIVE BELT (2026-08-01, Hal-adjudicated): the paragraph above
            // is what makes the asymmetry SAFE TODAY — there is exactly one caller
            // (CatalogHandler.handleCollectionRename), and its identity gate cannot go stale
            // because of the CAS. That is a fact about the CURRENT caller graph, not about
            // this method. A future non-HTTP caller (a CLI path, a migration step, a
            // scheduled repair) that calls renameCollection without replicating the
            // handler's identity pre-check would inherit the emptiness protection below and
            // NONE of the identity protection — silently. The belt below is optional
            // (opt-in via a non-null expectedTargetSupersededBy) precisely so it can be
            // additive: it does not change behavior for any caller that passes null, and it
            // re-verifies — it does not replace — the emptiness check above.
            if (targetRowExists && !liveTargetExists) {
                var blocker = blockingTable(ctx, newName);
                if (blocker.isPresent()) {
                    // nexus-34wrg option (c): name WHICH table blocked so an operator can
                    // tell an audit breadcrumb from real data instead of purging on faith.
                    throw new CollectionMergeRefused(
                        "target collection " + newName + " is retired but still holds "
                        + blocker.get().describe() + "; renaming onto it would merge two "
                        + "collections. Purge or restore it first.");
                }
            }
            if (targetRowExists && !liveTargetExists && expectedTargetSupersededBy != null) {
                String actualTargetSupersededBy = ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY)
                    .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(newName))
                    .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY);
                if (!expectedTargetSupersededBy.equals(actualTargetSupersededBy)) {
                    throw new CollectionMergeRefused(
                        "target collection " + newName + " is retired by " + actualTargetSupersededBy
                        + ", not " + expectedTargetSupersededBy + " as observed at the caller's own read; "
                        + "refusing to revive a tombstone whose identity changed underneath this rename.");
                }
            }

            if (liveTargetExists) {
                // RDR-162 cross-model COPY branch: repoint catalog_documents; leave
                // both registry rows (renaming the source would collide on the name PK).
                // TOMBSTONE-EXEMPT (nexus-mqd6t): deliberately NOT filtered on
                // DELETED_AT.isNull() -- a rename must repoint ALL documents under
                // the old physical_collection name, tombstoned or not, so a
                // tombstoned document's physical_collection tracks the collection's
                // CURRENT name. Filtering here would leave a later restore pointing
                // at a retired collection name (found during TombstoneFilterGateTest
                // authorship; undocumented before this). See
                // TombstoneFilterGateTest.TOMBSTONE_EXEMPT.
                counts.put("catalog_documents",
                    ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, newName)
                       .where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(oldName)).execute());
                // nexus-x6kdz (critique HIGH): the manifest's denormalized
                // collection must re-home HERE TOO — cross-model re-embeds
                // preserve chunk text, hence chashes, so the target
                // collection's chunk rows carry the same chashes and the
                // combined-query join stays live under the new name. Leaving
                // this out was the THIRD door back into the silent-empty
                // state (docs pointed at the target, manifests at the source).
                counts.put("catalog_document_chunks",
                    ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, newName)
                       .where(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(oldName)).execute());
                return counts;
            }

            // 1. New registry row Y, copying X's metadata (so children can re-home onto it).
            //    UPSERT, not a bare INSERT (nexus-cecqy): the conflict that can reach here is
            //    Y's own tombstone from an earlier rename Y->X, since a LIVE Y took the COPY
            //    branch above. Renaming back onto that retired name REVIVES it — superseded_by/at
            //    cleared, metadata refreshed from X — which is exactly what the round trip means.
            //    A bare INSERT would collide on the (tenant_id, name) PK and abort the re-home.
            //
            //    nexus-v6za0 — an EARLIER VERSION OF THIS COMMENT CLAIMED Y's own rename
            //    tombstone was the ONLY conflict that could reach here. That was false: a
            //    supersede-marked Y is also non-live, also reaches this upsert, and unlike a
            //    rename tombstone it still holds every chunk. CatalogHandler now gates the
            //    revive on the tombstone's identity (superseded_by == oldName) so only the
            //    round-trip undo arrives here, but do not restore the stronger claim — this
            //    transaction is not itself the thing enforcing it.
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
                            // nexus-c29vr: the new row is LIVE by construction — never copy the
                            // source's tombstone markers. This select-list used to carry
                            // SUPERSEDED_BY/SUPERSEDED_AT straight through, and only the
                            // DO UPDATE arm below cleared them. On the fresh-insert arm that
                            // made a retired X rename into a BORN-DEAD Y (superseded_by copied
                            // from X), permanently invisible to collectionForTuple, with all of
                            // X's data re-homed onto it. The handler now refuses a retired
                            // source, but "explicit beats incidental" applies to both arms.
                            DSL.val("", CATALOG_COLLECTIONS.SUPERSEDED_BY),
                            DSL.val(null, CATALOG_COLLECTIONS.SUPERSEDED_AT),
                            CATALOG_COLLECTIONS.CREATED_AT)
                        .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(oldName)))
                    .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
                    .doUpdate()
                    .set(CATALOG_COLLECTIONS.CONTENT_TYPE,         DSL.excluded(CATALOG_COLLECTIONS.CONTENT_TYPE))
                    .set(CATALOG_COLLECTIONS.OWNER_ID,             DSL.excluded(CATALOG_COLLECTIONS.OWNER_ID))
                    .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL,      DSL.excluded(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
                    .set(CATALOG_COLLECTIONS.MODEL_VERSION,        DSL.excluded(CATALOG_COLLECTIONS.MODEL_VERSION))
                    .set(CATALOG_COLLECTIONS.DISPLAY_NAME,         DSL.excluded(CATALOG_COLLECTIONS.DISPLAY_NAME))
                    .set(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, DSL.excluded(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED))
                    // Revive: clear the tombstone markers rather than copying X's (which
                    // are '' / NULL anyway — the CLI refuses to rename an already-
                    // superseded row, so X is live). Explicit beats incidental here.
                    .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "")
                    .set(CATALOG_COLLECTIONS.SUPERSEDED_AT, (java.time.OffsetDateTime) null)
                    .execute());

            // 2. Re-home every child denorm-collection table X->Y (Y now exists, FK satisfied).
            //    Driven by COLLECTION_SCOPED_TABLES so this loop and collectionIsEmpty can
            //    never disagree about WHICH tables carry a collection name. They were two
            //    literals for exactly one commit and drifted 17-vs-5 immediately, which is
            //    how a taxonomy-only collection read as empty and got merged (nexus-v6za0).
            //    Covers: T3 chunk vectors (fk-002 RESTRICT); the manifest's denormalized
            //    collection, the combined-query join key that rename was the second door back
            //    into the silently-empty-join state for (nexus-x6kdz); taxonomy assignments /
            //    topics / meta (fk-002-5, fk-003, fk-003-4 RESTRICT); centroids (no FK to
            //    topics, so an explicit re-home); aspects, highlights and the extraction
            //    queue; catalog_documents itself; and the audit/telemetry tables.
            //    (chash_index leg RETIRED — RDR-187/nexus-piwya.9.)
            for (CollectionScopedTable t : COLLECTION_SCOPED_TABLES) {
                counts.put(t.countKey(),
                    ctx.update(t.table()).set(t.collection(), newName)
                       .where(t.collection().eq(oldName)).execute());
            }

            // 3. RETIRE the old registry row X as a superseded tombstone (nexus-cecqy).
            //
            // This used to DELETE X. The delete was never required — by this point every
            // RESTRICT child has been re-homed onto Y, so X is childless and free to
            // either go or stay — and it destroyed the rename's own audit trail: the CLI
            // called supersede_collection(X, Y) immediately afterwards, that UPDATE
            // matched ZERO rows, and the operator was told "Emitted
            // CollectionSuperseded(X -> Y)" about something that had not happened. X
            // ended ABSENT rather than marked-superseded and the rename became
            // unrecoverable history.
            //
            // Keeping X ALIVE-but-unmarked was the other candidate and is worse: X and Y
            // share the (content_type, owner_id, embedding_model) tuple (step 1 copies
            // the metadata), and collectionForTuple resolves that tuple with
            // `superseded_by = '' ORDER BY name DESC LIMIT 1`. An unmarked X wins that
            // race whenever it sorts ABOVE Y — e.g. renaming to fix a typo,
            // code__zold__... -> code__anew__... — handing every subsequent write the
            // now-empty old name. The tombstone carries superseded_by != '', so it is
            // filtered out of tuple resolution by construction.
            //
            // Renaming back INTO a tombstoned name is ALLOWED, and is the point: it is the
            // round-trip undo, and step 1's upsert revives the row (nexus-u4e20). An earlier
            // version of this comment said the handler 409'd on it via collectionExists —
            // true before nexus-cecqy introduced tombstones, false after, and it sat here
            // contradicting the fix 120 lines above. The handler now permits exactly one
            // revive: a tombstone whose superseded_by names the collection being renamed
            // (nexus-v6za0). Every other non-live target 409s.
            counts.put("catalog_collections_superseded",
                ctx.update(CATALOG_COLLECTIONS)
                   .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, newName)
                   .set(CATALOG_COLLECTIONS.SUPERSEDED_AT, DSL.currentOffsetDateTime())
                   .where(CATALOG_COLLECTIONS.NAME.eq(oldName))
                   .execute());
            return counts;
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // META
    // ══════════════════════════════════════════════════════════════════════════

    public void setMeta(String tenant, String key, String value) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(CATALOG_META, CATALOG_META.TENANT_ID, CATALOG_META.KEY, CATALOG_META.VALUE)
               .values(tenant, key, value)
               .onConflict(CATALOG_META.TENANT_ID, CATALOG_META.KEY)
               .doUpdate()
               .set(CATALOG_META.VALUE, EX_META_VAL)
               .execute();
            return null;
        });
    }

    public String getMeta(String tenant, String key) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(CATALOG_META.VALUE).from(CATALOG_META).where(CATALOG_META.KEY.eq(key)).fetchOne();
            return r != null ? r.value1() : null;
        });
    }

    /** Return ACTIVE owners filtered by owner_type. Used by repos.py:list_repos_dual (nexus-qnp5s). */
    public List<Map<String, Object>> ownersByType(String tenant, String ownerType) {
        return ownersByType(tenant, ownerType, false);
    }

    /**
     * Return owners filtered by owner_type (nexus-cw262: {@code includeDeactivated}
     * audit option — same default-exclusion contract as {@link #listOwners(String,
     * boolean)}). This is the exact read path the 7kl32 census (`nx catalog owners
     * --census`, via {@code list_owners_by_type("repo")}) and doctor's git-hooks
     * dead-owner attribution both use — excluding deactivated owners by default is
     * the entire point of the deactivate route.
     */
    public List<Map<String, Object>> ownersByType(String tenant, String ownerType, boolean includeDeactivated) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = CATALOG_OWNERS.OWNER_TYPE.eq(ownerType);
            if (!includeDeactivated) cond = cond.and(CATALOG_OWNERS.DEACTIVATED_AT.isNull());
            return ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH,
                       CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH, CATALOG_OWNERS.DEACTIVATED_AT)
               .from(CATALOG_OWNERS)
               .where(cond)
               .fetch()
               .map(r -> ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7(),
                                  r.value8()));
        });
    }

    /**
     * Deactivate one owner (nexus-cw262: the 7kl32 dead-owner GC mutation arm's engine
     * half). Sets {@code deactivated_at = NOW()}, mirroring {@link #deleteDocument}'s
     * tombstone shape exactly (plain UPDATE, not the {@code nexus.document_trash}/
     * {@code purge_trash} SQL-function idiom — owners have no purge/GC counterpart,
     * so there is nothing for a stored function to add over a direct UPDATE).
     * {@code AND deactivated_at IS NULL}: idempotent, double-deactivate does not
     * reset the timestamp. Returns the row count actually updated (0 = already
     * deactivated or the prefix does not exist in this tenant).
     */
    public int deactivateOwner(String tenant, String tumblerPrefix) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(CATALOG_OWNERS)
               .set(CATALOG_OWNERS.DEACTIVATED_AT, DSL.currentOffsetDateTime())
               .where(CATALOG_OWNERS.TUMBLER_PREFIX.eq(tumblerPrefix).and(CATALOG_OWNERS.DEACTIVATED_AT.isNull()))
               .execute()
        );
    }

    /**
     * Reactivate one owner (nexus-cw262) — clears {@code deactivated_at}. The
     * explicit manual-correction path (Hal undoing a batch deactivation without
     * re-registering); {@link #upsertOwner} already clears the flag automatically
     * on any live re-registration, so this route exists for the no-write case.
     * Returns the row count actually updated (0 = already active or the prefix
     * does not exist in this tenant).
     */
    public int reactivateOwner(String tenant, String tumblerPrefix) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(CATALOG_OWNERS)
               .set(CATALOG_OWNERS.DEACTIVATED_AT, (OffsetDateTime) null)
               .where(CATALOG_OWNERS.TUMBLER_PREFIX.eq(tumblerPrefix).and(CATALOG_OWNERS.DEACTIVATED_AT.isNotNull()))
               .execute()
        );
    }

    /** Return a single owner by tumbler_prefix. Returns null if not found. */
    public Map<String, Object> ownerByPrefix(String tenant, String tumblerPrefix) {
        return tenantScope.withTenant(tenant, ctx -> {
            var r = ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH,
                               CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH,
                               CATALOG_OWNERS.NEXT_SEQ)
                       .from(CATALOG_OWNERS)
                       .where(CATALOG_OWNERS.TUMBLER_PREFIX.eq(tumblerPrefix))
                       .fetchOne();
            return r != null
                ? ownerRow(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(), r.value7(),
                           r.value8())
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
            var rows = ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.CHUNK_COUNT)
                          .from(CATALOG_DOCUMENTS)
                          .where(CATALOG_DOCUMENTS.TUMBLER.in(docIds).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
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
            var rows = ctx.select(CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.LINK_TYPE)
                          .from(CATALOG_LINKS)
                          .where(CATALOG_LINKS.FROM_TUMBLER.in(tumblers))
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
            var s = ctx.selectFrom(CATALOG_STATS).fetchOne();
            long docCount  = s.get(CATALOG_STATS.DOC_COUNT);
            long lnkCount  = s.get(CATALOG_STATS.LINK_COUNT);
            long ownCount  = s.get(CATALOG_STATS.OWNER_COUNT);
            long collCount = s.get(CATALOG_STATS.COLLECTION_COUNT);
            long chkCount  = s.get(CATALOG_STATS.CHUNK_COUNT);
            // RDR-154 P1.2: the two GROUP-BY breakdowns also read views (completing
            // the "5+2" collapse, Gap 3). links_by_type ← links_by_type_counts;
            // by_content_type reuses coverage_by_content_type.total (same per-type
            // document count — eliminates the duplicate aggregate the critic flagged).
            var ltypes = ctx.selectFrom(LINKS_BY_TYPE_COUNTS).fetch();
            Map<String, Long> byType = new LinkedHashMap<>();
            for (var r : ltypes) byType.put(r.get(LINKS_BY_TYPE_COUNTS.LINK_TYPE),
                                            r.get(LINKS_BY_TYPE_COUNTS.LINK_COUNT));
            // by_content_type: key is "" for null/empty content_type (the view already
            // COALESCEs to ''), matching SQLite Catalog.stats().
            var ctypes = ctx.select(COVERAGE_BY_CONTENT_TYPE.CONTENT_TYPE, COVERAGE_BY_CONTENT_TYPE.TOTAL)
                            .from(COVERAGE_BY_CONTENT_TYPE).fetch();
            Map<String, Long> byContentType = new LinkedHashMap<>();
            for (var r : ctypes) {
                byContentType.put(r.value1(), r.value2());
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
            var r = ctx.select(COLLECTION_HEALTH_META.LAST_INDEXED,
                               COLLECTION_HEALTH_META.ORPHAN_COUNT,
                               COLLECTION_HEALTH_META.STALE_SOURCE_RATIO)
                       .from(COLLECTION_HEALTH_META)
                       .where(COLLECTION_HEALTH_META.COLLECTION.eq(collection))
                       .fetchOne();

            Map<String, Object> result = new LinkedHashMap<>();
            result.put("last_indexed", r == null ? null : r.value1());
            result.put("orphan_count", r == null ? 0L : r.value2());
            // nexus-agsq7: index-age staleness; null when no dated doc qualifies.
            result.put("stale_source_ratio", r == null ? null : r.value3());
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
            ctx.selectDistinct(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.ne("").and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .fetch(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
        );
    }

    /**
     * Return owners whose repo_root is non-empty, as
     * {@code [{tumbler_prefix, name, owner_type, repo_hash, description, repo_root, head_hash}]}.
     *
     * <p>Backs the Python {@code owners_with_roots()} HttpCatalogClient method.
     * Replaces direct SQLite:
     * {@code SELECT tumbler_prefix, repo_root FROM owners WHERE repo_root != ''}
     *
     * <p>nexus-cw262 (code-review, catalog owner soft-delete): deliberately
     * NOT filtered on {@code deactivated_at IS NULL}, unlike {@link
     * #listOwners(String, boolean)} / {@link #ownersByType(String, String,
     * boolean)}'s default exclusion. Same rationale as {@link
     * #sweepNextSeqDrift}'s deliberate all-owners coverage: this method backs
     * {@code reconcile-stale}'s {@code owner_roots} lookup, which resolves
     * zero-count DOCUMENTS' on-disk provenance (the {@code owner_root_gone}
     * classification) — a document can still be live and registered under an
     * owner that was independently deactivated (e.g. Hal ran {@code --execute
     * deactivate} on a debris owner that, unknown to that pass, still had a
     * stray live document). Filtering this method would make that
     * document's path resolution silently vanish, regressing reconcile-stale's
     * ability to classify it at all — the same failure mode excluding
     * deactivated owners here would reintroduce.
     */
    public List<Map<String, Object>> ownersWithRoots(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH,
                       CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH)
               .from(CATALOG_OWNERS)
               .where(CATALOG_OWNERS.REPO_ROOT.ne(""))
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
    /**
     * Links whose {@code from_tumbler} or {@code to_tumbler} resolves to no live
     * document (nexus-ysrwi, GH #1419 issue 7).
     *
     * <p>Steve Harris's backup held 5 of 52 links pointing at tumblers with no
     * document anywhere in the same {@code pg_dump} — orphans left by document
     * deletion without link cleanup. {@code catalog_links} carries a PK and a
     * UNIQUE constraint but NO foreign key to {@code catalog_documents}
     * (catalog-001-baseline.xml), so nothing structurally prevents them.
     *
     * <p>This is the DETECTION half. Enforcement (an FK with ON DELETE, or
     * delete-time cleanup) is nexus-tk070's FK-census decision — a check the
     * client can render is useful either way, and remains useful after an FK
     * lands because an FK does not retroactively clean rows already orphaned.
     *
     * <p>The client cannot compute this itself: {@code HttpCatalogClient.link_audit}
     * is a service-mode stub, and the only batch existence primitive available
     * ({@code chunk_counts_for_docs}) omits documents that merely lack a
     * chunk_count, which would report live documents as dangling.
     *
     * <p>{@code side} names which endpoint dangles ({@code "from"}, {@code "to"},
     * or {@code "both"}) so an operator can tell a deleted target from a
     * deleted source without a second query.
     */
    public List<Map<String, Object>> orphanedLinks(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            // NOT EXISTS rather than a LEFT JOIN: RLS-safe by construction (the
            // subquery is tenant-scoped by the same GUC) and it cannot multiply
            // rows when a tumbler somehow has duplicate document rows.
            var fromMissing = DSL.notExists(
                ctx.selectOne().from(CATALOG_DOCUMENTS)
                   .where(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_LINKS.FROM_TUMBLER).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
            );
            var toMissing = DSL.notExists(
                ctx.selectOne().from(CATALOG_DOCUMENTS)
                   .where(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_LINKS.TO_TUMBLER).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
            );
            return ctx.select(CATALOG_LINKS.ID, CATALOG_LINKS.FROM_TUMBLER,
                              CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE,
                              CATALOG_LINKS.CREATED_BY)
                      .from(CATALOG_LINKS)
                      .where(fromMissing.or(toMissing))
                      .orderBy(CATALOG_LINKS.ID)
                      .fetch()
                      .map(r -> {
                          boolean fm = r.get(CATALOG_LINKS.FROM_TUMBLER) != null
                              && !documentExists(ctx, r.get(CATALOG_LINKS.FROM_TUMBLER));
                          boolean tm = r.get(CATALOG_LINKS.TO_TUMBLER) != null
                              && !documentExists(ctx, r.get(CATALOG_LINKS.TO_TUMBLER));
                          Map<String, Object> m = new LinkedHashMap<>();
                          m.put("id", r.get(CATALOG_LINKS.ID));
                          m.put("from_tumbler", r.get(CATALOG_LINKS.FROM_TUMBLER));
                          m.put("to_tumbler", r.get(CATALOG_LINKS.TO_TUMBLER));
                          m.put("link_type", r.get(CATALOG_LINKS.LINK_TYPE));
                          m.put("created_by", r.get(CATALOG_LINKS.CREATED_BY));
                          m.put("side", fm && tm ? "both" : (fm ? "from" : "to"));
                          return m;
                      });
        });
    }

    /**
     * nexus-23wlw: LIVE-only, and it MUST stay in lockstep with the
     * {@code fromMissing}/{@code toMissing} predicates in {@link #orphanedLinks}.
     * That method uses those predicates to select the dangling links and this
     * one to label WHICH side dangles; if the two disagree about tombstones, a
     * link is reported as orphaned with {@code side} naming neither endpoint.
     */
    private boolean documentExists(org.jooq.DSLContext ctx, String tumbler) {
        return ctx.fetchExists(
            ctx.selectOne().from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)
                   .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
        );
    }

    public List<Map<String, Object>> orphanedDocs(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            // Documents with no outgoing links (from_tumbler not in links)
            // AND no incoming links (to_tumbler not in links).
            // Use NOT EXISTS subqueries for cross-tenant RLS safety.
            var noOut = DSL.notExists(
                ctx.selectOne().from(CATALOG_LINKS).where(CATALOG_LINKS.FROM_TUMBLER.eq(CATALOG_DOCUMENTS.TUMBLER))
            );
            var noIn = DSL.notExists(
                ctx.selectOne().from(CATALOG_LINKS).where(CATALOG_LINKS.TO_TUMBLER.eq(CATALOG_DOCUMENTS.TUMBLER))
            );
            return ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH)
                      .from(CATALOG_DOCUMENTS)
                      .where(noOut.and(noIn).and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                      .orderBy(CATALOG_DOCUMENTS.TUMBLER)
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
            ctx.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .from(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.FILE_PATH.startsWith("/").and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
               .orderBy(CATALOG_DOCUMENTS.TUMBLER)
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
            var r = ctx.select(CATALOG_COLLECTIONS.OWNER_ID, CATALOG_OWNERS.REPO_ROOT)
                       .from(CATALOG_COLLECTIONS)
                       .leftJoin(CATALOG_OWNERS)
                       .on(CATALOG_COLLECTIONS.OWNER_ID.eq(CATALOG_OWNERS.TUMBLER_PREFIX))
                       .where(CATALOG_COLLECTIONS.NAME.eq(name))
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
            var rows = ctx.select(COLLECTION_DOC_COUNTS.PHYSICAL_COLLECTION, COLLECTION_DOC_COUNTS.DOC_COUNT)
                          .from(COLLECTION_DOC_COUNTS)
                          .fetch();
            Map<String, Long> result = new LinkedHashMap<>();
            for (var r : rows) {
                result.put(r.value1(), r.value2());
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
            List<Map<String, Object>> result = new ArrayList<>();
            if (ownerPrefix == null || ownerPrefix.isBlank()) {
                var rows = ctx.select(COVERAGE_BY_CONTENT_TYPE.CONTENT_TYPE,
                                      COVERAGE_BY_CONTENT_TYPE.TOTAL,
                                      COVERAGE_BY_CONTENT_TYPE.LINKED)
                              .from(COVERAGE_BY_CONTENT_TYPE)
                              .fetch();
                for (var r : rows) {
                    Map<String, Object> row = new LinkedHashMap<>();
                    row.put("content_type", r.value1());
                    row.put("total",        r.value2());
                    row.put("linked",       r.value3());
                    result.add(row);
                }
            } else {
                String likePat = ownerPrefix.replaceAll("\\.$", "") + ".%";
                // DSL.inline("") (a constant, not a bind param) — reusing the SAME Field
                // object in both the SELECT list and GROUP BY only reads as the same
                // expression to Postgres's GROUP BY validity check when the fallback is an
                // inlined literal; a bound "?" parameter renders as a DIFFERENT placeholder
                // ($N vs $M) at each occurrence, so Postgres sees two distinct expressions
                // and rejects the query with "must appear in the GROUP BY clause" even
                // though both bind to "" at runtime (caught by CatalogRepositoryTest
                // coverageByContentType_ownerPrefixFilter).
                Field<String> contentType = DSL.coalesce(CATALOG_DOCUMENTS.CONTENT_TYPE, DSL.inline(""));
                Field<Long> linkedCount = DSL.count().filterWhere(
                    DSL.exists(DSL.selectOne().from(CATALOG_LINKS)
                        .where(CATALOG_LINKS.FROM_TUMBLER.eq(CATALOG_DOCUMENTS.TUMBLER)
                            .or(CATALOG_LINKS.TO_TUMBLER.eq(CATALOG_DOCUMENTS.TUMBLER)))))
                    .cast(Long.class);
                var rows = ctx.select(contentType, DSL.count().cast(Long.class), linkedCount)
                              .from(CATALOG_DOCUMENTS)
                              .where(CATALOG_DOCUMENTS.TUMBLER.like(likePat)
                                  .or(CATALOG_DOCUMENTS.TUMBLER.eq(ownerPrefix))
                                  .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                              .groupBy(contentType)
                              .fetch();
                for (var r : rows) {
                    Map<String, Object> row = new LinkedHashMap<>();
                    row.put("content_type", r.value1());
                    row.put("total",        r.value2());
                    row.put("linked",       r.value3());
                    result.add(row);
                }
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
            doImportOwner(ctx, tenant, o);
            return null;
        });
    }

    /**
     * nexus-1usso: GUC-once bulk owner import — ONE multi-row
     * {@code INSERT ... ON CONFLICT} statement (chunked at {@link
     * #MAX_BATCH_PARAMS} bind params), mirroring {@code
     * ChashRepository.doImportBatch} (f0ab406f). The RDR-176 P3 endpoint
     * already existed but still looped the per-row {@link #doImportOwner}
     * (N round-trips) — the plan-audit finding on nexus-1usso ("has the
     * endpoint" != "batches at the DB") applies to every Catalog import
     * method. Rows are deduped on {@code tumbler_prefix} (the conflict key)
     * within a chunk, last occurrence wins.
     */
    public int importOwnersBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        if (TenantConstants.isWildcard(tenant)) {
            throw new IllegalArgumentException(
                "tenant '*' is a reserved sentinel and cannot own catalog entries");
        }
        return tenantScope.withTenant(tenant, ctx -> {
            var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(rows.size());
            for (var o : rows) unique.put(s(o, "tumbler_prefix"), o);
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 9);
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                var insert = ctx.insertInto(CATALOG_OWNERS,
                        CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE,
                        CATALOG_OWNERS.REPO_HASH, CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH, CATALOG_OWNERS.NEXT_SEQ);
                for (var o : batch) {
                    // Same identity guard as doImportOwner (nexus-0ehwe arbiter class) —
                    // the batch form is not exempt from the keys its arbiter omits.
                    guardOwnerIdentity(ctx, tenant, s(o, "tumbler_prefix"),
                                       s(o, "name"), s(o, "owner_type"), s(o, "repo_hash"));
                    insert = insert.values(tenant,
                            s(o,"tumbler_prefix"), s(o,"name"), s(o,"owner_type"),
                            s(o,"repo_hash"), s(o,"description"), nne(s(o,"repo_root")),
                            s(o,"head_hash"), lng(o,"next_seq", 0L));
                }
                insert.onConflict(CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX)
                      .doUpdate()
                      .set(CATALOG_OWNERS.NAME, EX_OWN_NAME)
                      .set(CATALOG_OWNERS.OWNER_TYPE, EX_OWN_TYPE)
                      .set(CATALOG_OWNERS.REPO_HASH, EX_OWN_REPO)
                      .set(CATALOG_OWNERS.DESCRIPTION, EX_OWN_DESC)
                      .set(CATALOG_OWNERS.REPO_ROOT, EX_OWN_ROOT)
                      .set(CATALOG_OWNERS.HEAD_HASH, EX_OWN_HEAD)
                      .set(CATALOG_OWNERS.NEXT_SEQ,  EX_OWN_SEQ_GREATEST)
                      .execute();
            }
            return rows.size();
        });
    }

    /** PG Int16 bind-count limit is 32767; keep a safety margin (nexus-1usso). */
    private static final int MAX_BATCH_PARAMS = 30_000;

    private void doImportOwner(DSLContext ctx, String tenant, Map<String, Object> o) {
        // nexus-0ehwe arbiter class. The ETL replays an authoritative snapshot at an
        // EXPLICIT address, so the address wins — but the snapshot may still carry an
        // identity that now lives at a different prefix (SQLite permitted two owners to
        // share name+owner_type; PG's catalog_owners_unique_name_type forbids it, the
        // substrate disagreement nexus-aqbrk recorded). Converging would MERGE two
        // distinct owners, so this refuses by name instead of raising a raw 23505.
        guardOwnerIdentity(ctx, tenant, s(o, "tumbler_prefix"),
                           s(o, "name"), s(o, "owner_type"), s(o, "repo_hash"));
        ctx.insertInto(CATALOG_OWNERS,
                CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE,
                CATALOG_OWNERS.REPO_HASH, CATALOG_OWNERS.DESCRIPTION, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.HEAD_HASH, CATALOG_OWNERS.NEXT_SEQ)
           .values(tenant,
                   s(o,"tumbler_prefix"), s(o,"name"), s(o,"owner_type"),
                   s(o,"repo_hash"), s(o,"description"), nne(s(o,"repo_root")),
                   s(o,"head_hash"), lng(o,"next_seq", 0L))
           .onConflict(CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX)
           .doUpdate()
           .set(CATALOG_OWNERS.NAME, EX_OWN_NAME)
           .set(CATALOG_OWNERS.OWNER_TYPE, EX_OWN_TYPE)
           .set(CATALOG_OWNERS.REPO_HASH, EX_OWN_REPO)
           .set(CATALOG_OWNERS.DESCRIPTION, EX_OWN_DESC)
           .set(CATALOG_OWNERS.REPO_ROOT, EX_OWN_ROOT)
           .set(CATALOG_OWNERS.HEAD_HASH, EX_OWN_HEAD)
           .set(CATALOG_OWNERS.NEXT_SEQ,  EX_OWN_SEQ_GREATEST)
           .execute();
    }

    /** Fidelity-preserving document import. Uses GREATEST for source_mtime. */
    public void importDocument(String tenant, Map<String, Object> d) {
        tenantScope.withTenant(tenant, ctx -> {
            doImportDocument(ctx, tenant, d);
            return null;
        });
    }

    /**
     * nexus-1usso: GUC-once bulk document import — ONE multi-row
     * {@code INSERT ... ON CONFLICT} statement per chunk. Rows are deduped
     * on {@code tumbler} (the conflict key) within a chunk, last occurrence
     * wins.
     */
    public int importDocumentsBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(rows.size());
            for (var d : rows) unique.put(s(d, "tumbler"), d);
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            final int cols = 24;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                var insert = ctx.insertInto(CATALOG_DOCUMENTS,
                        CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.YEAR,
                        CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.CORPUS, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT,
                        CATALOG_DOCUMENTS.HEAD_HASH, CATALOG_DOCUMENTS.INDEXED_AT, F_DOC_META, CATALOG_DOCUMENTS.SOURCE_MTIME, CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.SOURCE_URI,
                        CATALOG_DOCUMENTS.BIB_YEAR, CATALOG_DOCUMENTS.BIB_AUTHORS, CATALOG_DOCUMENTS.BIB_VENUE, CATALOG_DOCUMENTS.BIB_CITATION_COUNT,
                        CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID, CATALOG_DOCUMENTS.BIB_OPENALEX_ID, CATALOG_DOCUMENTS.BIB_DOI, CATALOG_DOCUMENTS.BIB_ENRICHED_AT);
                for (var d : batch) {
                    String metaJson = jsonOrNull(d.get("metadata"));
                    // Same identity guard as doImportDocument (nexus-0ehwe arbiter class).
                    guardDocumentIdentity(ctx, tenant, s(d, "tumbler"), nne(s(d, "source_uri")));
                    insert = insert.values(tenant, s(d,"tumbler"), s(d,"title"), s(d,"author"), i(d,"year"),
                            nne(s(d,"content_type")), nne(s(d,"file_path")), nne(s(d,"corpus")),
                            nne(s(d,"physical_collection")), ni(i(d,"chunk_count"), 0),
                            nne(s(d,"head_hash")), nne(s(d,"indexed_at")),
                            jsonbVal(metaJson),
                            nd(dbl(d,"source_mtime")), nne(s(d,"alias_of")), nne(s(d,"source_uri")),
                            ni(i(d,"bib_year"), 0), nne(s(d,"bib_authors")),
                            nne(s(d,"bib_venue")), ni(i(d,"bib_citation_count"), 0),
                            nne(s(d,"bib_semantic_scholar_id")), nne(s(d,"bib_openalex_id")),
                            nne(s(d,"bib_doi")), nne(s(d,"bib_enriched_at")));
                }
                insert.onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
                      .doUpdate()
                      .set(CATALOG_DOCUMENTS.TITLE,  EX_DOC_TITLE)
                      .set(CATALOG_DOCUMENTS.AUTHOR, EX_DOC_AUTHOR)
                      .set(CATALOG_DOCUMENTS.YEAR,   EX_DOC_YEAR)
                      .set(CATALOG_DOCUMENTS.CONTENT_TYPE,  EX_DOC_CTYPE)
                      .set(CATALOG_DOCUMENTS.FILE_PATH,  EX_DOC_FPATH)
                      .set(CATALOG_DOCUMENTS.CORPUS, EX_DOC_CORPUS)
                      .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,  EX_DOC_PCOLL)
                      .set(CATALOG_DOCUMENTS.CHUNK_COUNT, EX_DOC_CHUNKS)
                      .set(CATALOG_DOCUMENTS.HEAD_HASH,   EX_DOC_HEAD)
                      .set(CATALOG_DOCUMENTS.INDEXED_AT,  EX_DOC_IDXAT)
                      .set(F_DOC_META,   EX_DOC_META)
                      // GREATEST: never downgrade source_mtime on re-import
                      .set(CATALOG_DOCUMENTS.SOURCE_MTIME, EX_DOC_SMTIME_GREATEST)
                      .set(CATALOG_DOCUMENTS.ALIAS_OF,  EX_DOC_ALIAS)
                      .set(CATALOG_DOCUMENTS.SOURCE_URI,    EX_DOC_URI)
                      .set(CATALOG_DOCUMENTS.BIB_YEAR,   EX_DOC_BIBY)
                      .set(CATALOG_DOCUMENTS.BIB_AUTHORS,   EX_DOC_BIAU)
                      .set(CATALOG_DOCUMENTS.BIB_VENUE,   EX_DOC_BIVE)
                      .set(CATALOG_DOCUMENTS.BIB_CITATION_COUNT,   EX_DOC_BICC)
                      .set(CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID,   EX_DOC_BIS2)
                      .set(CATALOG_DOCUMENTS.BIB_OPENALEX_ID,   EX_DOC_BIOA)
                      .set(CATALOG_DOCUMENTS.BIB_DOI,  EX_DOC_BIDOI)
                      .set(CATALOG_DOCUMENTS.BIB_ENRICHED_AT,   EX_DOC_BIAT)
                      .execute();
            }
            return rows.size();
        });
    }

    private void doImportDocument(DSLContext ctx, String tenant, Map<String, Object> d) {
        String metaJson = jsonOrNull(d.get("metadata"));
        {
            // nexus-0ehwe arbiter class: address-arbitrated, and the DO UPDATE arm sets
            // source_uri — so both arms are exposed to ux_catalog_documents_live_source_uri.
            guardDocumentIdentity(ctx, tenant, s(d, "tumbler"), nne(s(d, "source_uri")));
            ctx.insertInto(CATALOG_DOCUMENTS,
                    CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.YEAR,
                    CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.CORPUS, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT,
                    CATALOG_DOCUMENTS.HEAD_HASH, CATALOG_DOCUMENTS.INDEXED_AT, F_DOC_META, CATALOG_DOCUMENTS.SOURCE_MTIME, CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.SOURCE_URI,
                    CATALOG_DOCUMENTS.BIB_YEAR, CATALOG_DOCUMENTS.BIB_AUTHORS, CATALOG_DOCUMENTS.BIB_VENUE, CATALOG_DOCUMENTS.BIB_CITATION_COUNT,
                    CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID, CATALOG_DOCUMENTS.BIB_OPENALEX_ID, CATALOG_DOCUMENTS.BIB_DOI, CATALOG_DOCUMENTS.BIB_ENRICHED_AT)
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
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doUpdate()
               .set(CATALOG_DOCUMENTS.TITLE,  EX_DOC_TITLE)
               .set(CATALOG_DOCUMENTS.AUTHOR, EX_DOC_AUTHOR)
               .set(CATALOG_DOCUMENTS.YEAR,   EX_DOC_YEAR)
               .set(CATALOG_DOCUMENTS.CONTENT_TYPE,  EX_DOC_CTYPE)
               .set(CATALOG_DOCUMENTS.FILE_PATH,  EX_DOC_FPATH)
               .set(CATALOG_DOCUMENTS.CORPUS, EX_DOC_CORPUS)
               .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,  EX_DOC_PCOLL)
               .set(CATALOG_DOCUMENTS.CHUNK_COUNT, EX_DOC_CHUNKS)
               .set(CATALOG_DOCUMENTS.HEAD_HASH,   EX_DOC_HEAD)
               .set(CATALOG_DOCUMENTS.INDEXED_AT,  EX_DOC_IDXAT)
               .set(F_DOC_META,   EX_DOC_META)
               // GREATEST: never downgrade source_mtime on re-import
               .set(CATALOG_DOCUMENTS.SOURCE_MTIME, EX_DOC_SMTIME_GREATEST)
               .set(CATALOG_DOCUMENTS.ALIAS_OF,  EX_DOC_ALIAS)
               .set(CATALOG_DOCUMENTS.SOURCE_URI,    EX_DOC_URI)
               .set(CATALOG_DOCUMENTS.BIB_YEAR,   EX_DOC_BIBY)
               .set(CATALOG_DOCUMENTS.BIB_AUTHORS,   EX_DOC_BIAU)
               .set(CATALOG_DOCUMENTS.BIB_VENUE,   EX_DOC_BIVE)
               .set(CATALOG_DOCUMENTS.BIB_CITATION_COUNT,   EX_DOC_BICC)
               .set(CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID,   EX_DOC_BIS2)
               .set(CATALOG_DOCUMENTS.BIB_OPENALEX_ID,   EX_DOC_BIOA)
               .set(CATALOG_DOCUMENTS.BIB_DOI,  EX_DOC_BIDOI)
               .set(CATALOG_DOCUMENTS.BIB_ENRICHED_AT,   EX_DOC_BIAT)
               .execute();
        }
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
        tenantScope.withTenant(tenant, ctx -> {
            doImportLink(ctx, tenant, lnk);
            return null;
        });
    }

    /**
     * nexus-1usso: GUC-once bulk link import — ONE multi-row {@code INSERT
     * ... ON CONFLICT DO NOTHING} statement per chunk. No dedup needed:
     * intra-statement conflicts against {@code DO NOTHING} are a documented
     * no-op (unlike {@code DO UPDATE}, which cannot affect the same row
     * twice).
     */
    public int importLinksBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 9);
            for (int start = 0; start < rows.size(); start += chunkSize) {
                var batch = rows.subList(start, Math.min(start + chunkSize, rows.size()));
                var insert = ctx.insertInto(CATALOG_LINKS,
                        CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE,
                        CATALOG_LINKS.FROM_SPAN, CATALOG_LINKS.TO_SPAN, CATALOG_LINKS.CREATED_BY, CATALOG_LINKS.CREATED_AT, F_LNK_META);
                for (var lnk : batch) {
                    String metaJson = jsonOrNull(lnk.get("metadata"));
                    insert = insert.values(DSL.val(tenant),
                            DSL.val(s(lnk,"from_tumbler")), DSL.val(s(lnk,"to_tumbler")), DSL.val(s(lnk,"link_type")),
                            DSL.val(nne(s(lnk,"from_span"))), DSL.val(nne(s(lnk,"to_span"))),
                            DSL.val(nne(s(lnk,"created_by"))), DSL.val(nne(s(lnk,"created_at"))),
                            jsonbVal(metaJson));
                }
                insert.onConflict(CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE)
                      .doNothing()
                      .execute();
            }
            return rows.size();
        });
    }

    private void doImportLink(DSLContext ctx, String tenant, Map<String, Object> lnk) {
        String metaJson = jsonOrNull(lnk.get("metadata"));
        ctx.insertInto(CATALOG_LINKS,
                CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE,
                CATALOG_LINKS.FROM_SPAN, CATALOG_LINKS.TO_SPAN, CATALOG_LINKS.CREATED_BY, CATALOG_LINKS.CREATED_AT, F_LNK_META)
           .values(DSL.val(tenant),
                   DSL.val(s(lnk,"from_tumbler")), DSL.val(s(lnk,"to_tumbler")), DSL.val(s(lnk,"link_type")),
                   DSL.val(nne(s(lnk,"from_span"))), DSL.val(nne(s(lnk,"to_span"))),
                   DSL.val(nne(s(lnk,"created_by"))), DSL.val(nne(s(lnk,"created_at"))),
                   jsonbVal(metaJson))
           .onConflict(CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE)
           .doNothing()
           .execute();
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
            doImportChunk(ctx, tenant, docId, row);
            return null;
        });
    }

    /**
     * RDR-176 P3 (Gap 1): GUC-once bulk chunk import for ONE document — all
     * *rows* land under one withTenant (one GUC set), matching the doc-scoped
     * {@code {doc_id, rows}} import envelope.
     */
    public int importChunksBatch(String tenant, String docId, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            // Conflict key: (tenant_id, doc_id, position). doc_id is constant for
            // this call (the {doc_id, rows} import envelope is per-document).
            var unique = new java.util.LinkedHashMap<Integer, Map<String, Object>>(rows.size());
            for (var row : rows) unique.put(i(row, "position"), row);
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            // nexus-x6kdz: stamp the doc's physical_collection on every row —
            // the combined-query join key no writer previously populated.
            String coll = physicalCollectionOf(ctx, tenant, docId);
            // nexus-11gh6 rev 2 §2.2: gate BEFORE the insert loop below —
            // one acquisition per transaction is enough (pg_advisory_xact_lock_shared
            // is re-entrant within a transaction; this ETL leg has no
            // acquireIndexRunLock call to order against).
            if (coll != null) {
                acquireSweepGateShared(ctx, tenant, coll);
            }

            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 10);
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                // nexus-11gh6 §7d: routed through the single-homed insert
                // helper, one call per chunk — `deduped` above already
                // guarantees no two rows in `batch` share a conflict key, so
                // batching through ONE multi-row statement per chunk is safe
                // (preserves this method's pre-existing batching exactly).
                insertManifestChunkRows(ctx, tenant, docId, coll, batch, ManifestInsertMode.UPSERT_IMPORT);
            }
            return rows.size();
        });
    }

    private void doImportChunk(DSLContext ctx, String tenant, String docId, Map<String, Object> row) {
        String coll = physicalCollectionOf(ctx, tenant, docId);
        if (coll != null) {
            acquireSweepGateShared(ctx, tenant, coll);
        }
        insertManifestChunkRows(ctx, tenant, docId, coll, List.of(row), ManifestInsertMode.UPSERT_IMPORT);
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
            doImportCollection(ctx, tenant, coll);
            return null;
        });
    }

    /**
     * nexus-1usso: GUC-once bulk collection import — ONE multi-row
     * {@code INSERT ... ON CONFLICT DO UPDATE ... WHERE} statement per
     * chunk. nexus-xtmtf: jOOQ's chained {@code .values()} supports a
     * dynamic row count, and the nullable timestamptz columns bind as
     * OffsetDateTime (blank -> NULL) — zero raw SQL, one statement per
     * chunk preserved. Rows are deduped on {@code name} (the conflict
     * key) within a chunk, last occurrence wins.
     */
    public int importCollectionsBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(rows.size());
            for (var coll : rows) unique.put(s(coll, "name"), coll);
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            final int cols = 11;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                var insert = ctx.insertInto(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                        CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                        CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                        CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED,
                        CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_AT,
                        CATALOG_COLLECTIONS.CREATED_AT);
                for (Map<String, Object> coll : batch) {
                    insert = insert.values(tenant,
                            s(coll, "name"), nne(s(coll, "content_type")),
                            nne(s(coll, "owner_id")), nne(s(coll, "embedding_model")),
                            nne(s(coll, "model_version")), nne(s(coll, "display_name")),
                            ni(i(coll, "legacy_grandfathered"), 0),
                            nne(s(coll, "superseded_by")), tsOrNull(s(coll, "superseded_at")),
                            tsOrNull(s(coll, "created_at")));
                }
                insert.onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
                      .doUpdate()
                      .set(CATALOG_COLLECTIONS.CONTENT_TYPE,         DSL.excluded(CATALOG_COLLECTIONS.CONTENT_TYPE))
                      .set(CATALOG_COLLECTIONS.OWNER_ID,             DSL.excluded(CATALOG_COLLECTIONS.OWNER_ID))
                      .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL,      DSL.excluded(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
                      .set(CATALOG_COLLECTIONS.MODEL_VERSION,        DSL.excluded(CATALOG_COLLECTIONS.MODEL_VERSION))
                      .set(CATALOG_COLLECTIONS.DISPLAY_NAME,         DSL.excluded(CATALOG_COLLECTIONS.DISPLAY_NAME))
                      .set(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, DSL.excluded(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED))
                      .set(CATALOG_COLLECTIONS.SUPERSEDED_BY,        DSL.excluded(CATALOG_COLLECTIONS.SUPERSEDED_BY))
                      .set(CATALOG_COLLECTIONS.SUPERSEDED_AT,        DSL.excluded(CATALOG_COLLECTIONS.SUPERSEDED_AT))
                      .set(CATALOG_COLLECTIONS.CREATED_AT,           DSL.excluded(CATALOG_COLLECTIONS.CREATED_AT))
                      .where(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("")
                          .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq(""))
                          .and(CATALOG_COLLECTIONS.OWNER_ID.eq("")))
                      .execute();
            }
            return rows.size();
        });
    }

    private void doImportCollection(DSLContext ctx, String tenant, Map<String, Object> coll) {
        // DO UPDATE WHERE stub-guard: only upgrades rows where all three discriminator
        // columns are empty (auto-registered stubs from RDR-156 P0.2 ensure-registration).
        // nexus-xtmtf: single-row delegate of the importCollectionsBatch DSL shape.
        var insert = ctx.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                CATALOG_COLLECTIONS.DISPLAY_NAME, CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED,
                CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_AT,
                CATALOG_COLLECTIONS.CREATED_AT)
           .values(tenant,
                   s(coll, "name"), nne(s(coll, "content_type")),
                   nne(s(coll, "owner_id")), nne(s(coll, "embedding_model")),
                   nne(s(coll, "model_version")), nne(s(coll, "display_name")),
                   ni(i(coll, "legacy_grandfathered"), 0),
                   nne(s(coll, "superseded_by")), tsOrNull(s(coll, "superseded_at")),
                   tsOrNull(s(coll, "created_at")));
        insert.onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
              .doUpdate()
              .set(CATALOG_COLLECTIONS.CONTENT_TYPE,         DSL.excluded(CATALOG_COLLECTIONS.CONTENT_TYPE))
              .set(CATALOG_COLLECTIONS.OWNER_ID,             DSL.excluded(CATALOG_COLLECTIONS.OWNER_ID))
              .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL,      DSL.excluded(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
              .set(CATALOG_COLLECTIONS.MODEL_VERSION,        DSL.excluded(CATALOG_COLLECTIONS.MODEL_VERSION))
              .set(CATALOG_COLLECTIONS.DISPLAY_NAME,         DSL.excluded(CATALOG_COLLECTIONS.DISPLAY_NAME))
              .set(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED, DSL.excluded(CATALOG_COLLECTIONS.LEGACY_GRANDFATHERED))
              .set(CATALOG_COLLECTIONS.SUPERSEDED_BY,        DSL.excluded(CATALOG_COLLECTIONS.SUPERSEDED_BY))
              .set(CATALOG_COLLECTIONS.SUPERSEDED_AT,        DSL.excluded(CATALOG_COLLECTIONS.SUPERSEDED_AT))
              .set(CATALOG_COLLECTIONS.CREATED_AT,           DSL.excluded(CATALOG_COLLECTIONS.CREATED_AT))
              .where(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("")
                  .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq(""))
                  .and(CATALOG_COLLECTIONS.OWNER_ID.eq("")))
              .execute();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    @SuppressWarnings("unchecked")
    private static SelectField<?>[] documentFields() {
        return new SelectField<?>[]{
            CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.AUTHOR, CATALOG_DOCUMENTS.YEAR,
            CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.CORPUS, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT,
            CATALOG_DOCUMENTS.HEAD_HASH, CATALOG_DOCUMENTS.INDEXED_AT, F_DOC_META, CATALOG_DOCUMENTS.SOURCE_MTIME, CATALOG_DOCUMENTS.ALIAS_OF, CATALOG_DOCUMENTS.SOURCE_URI,
            CATALOG_DOCUMENTS.BIB_YEAR, CATALOG_DOCUMENTS.BIB_AUTHORS, CATALOG_DOCUMENTS.BIB_VENUE, CATALOG_DOCUMENTS.BIB_CITATION_COUNT,
            CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID, CATALOG_DOCUMENTS.BIB_OPENALEX_ID, CATALOG_DOCUMENTS.BIB_DOI, CATALOG_DOCUMENTS.BIB_ENRICHED_AT,
            // nexus-5xn3k.2 (RUNFENCE): the fence fields, so /show, /list, /search,
            // /resolve, /resolve_many, /traverse (every documentFields() reader) all
            // surface index_state/index_content_hash/index_run_id/index_started_at —
            // the wire contract the client half (nexus-5xn3k.3) reads its 4 new
            // CatalogEntry fields from. No separate "get fence state" route needed.
            CATALOG_DOCUMENTS.INDEX_STATE, CATALOG_DOCUMENTS.INDEX_CONTENT_HASH,
            CATALOG_DOCUMENTS.INDEX_RUN_ID, CATALOG_DOCUMENTS.INDEX_STARTED_AT
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
        // nexus-5xn3k.2 (RUNFENCE): index_state stays NULL-able (NULL = unknown,
        // catalog-020's deliberate no-backfill default) — do NOT nne() it, unlike
        // the other three (NOT NULL DEFAULT '' columns, same nne() treatment as
        // alias_of/source_uri above).
        m.put("index_state",        raw.getOrDefault("index_state", null));
        m.put("index_content_hash", nne((String) raw.getOrDefault("index_content_hash", null)));
        m.put("index_run_id",       nne((String) raw.getOrDefault("index_run_id", null)));
        m.put("index_started_at",   nne((String) raw.getOrDefault("index_started_at", null)));
        return m;
    }

    /**
     * Owner row PLUS {@code next_seq} (nexus-0ehwe item 3).
     *
     * <p>next_seq was exposed on NO read path: not {@code ownerByPrefix},
     * not {@code /owners/show}, not {@code /owners/list}. The only way to tell
     * a drifted owner from a healthy one was to attempt a real registration and
     * see whether it 409'd — a MUTATION used as a diagnostic. That is why the
     * original wedge (nexus-pbawi) took a long investigation to localize, and
     * why "how many other owners are already wedged?" was unanswerable.
     *
     * <p>An overload rather than a signature change: {@code ownerRow} has seven
     * callers and most have no next_seq in scope.
     */
    private static Map<String, Object> ownerRow(String prefix, String name, String type,
                                                  String repo, String desc, String root, String head,
                                                  Long nextSeq) {
        Map<String, Object> m = ownerRow(prefix, name, type, repo, desc, root, head);
        m.put("next_seq", nextSeq == null ? 0L : nextSeq);
        return m;
    }

    /**
     * nexus-cw262: {@code listOwners(tenant, includeDeactivated)} overload — carries
     * BOTH next_seq and deactivated_at. An overload rather than a signature change to
     * the 8-arg (nextSeq-only) form above for the same reason that one is itself an
     * overload of the 7-arg base: {@code ownerRow} has many callers and most have
     * neither next_seq nor deactivated_at in scope.
     */
    private static Map<String, Object> ownerRow(String prefix, String name, String type,
                                                  String repo, String desc, String root, String head,
                                                  Long nextSeq, OffsetDateTime deactivatedAt) {
        Map<String, Object> m = ownerRow(prefix, name, type, repo, desc, root, head, nextSeq);
        m.put("deactivated_at", deactivatedAt);
        return m;
    }

    /**
     * nexus-cw262: {@code ownersByType(tenant, type, includeDeactivated)} overload —
     * deactivated_at without next_seq (the {@code /owners/by_type} route never
     * selected next_seq; not changing that here).
     */
    private static Map<String, Object> ownerRow(String prefix, String name, String type,
                                                  String repo, String desc, String root, String head,
                                                  OffsetDateTime deactivatedAt) {
        Map<String, Object> m = ownerRow(prefix, name, type, repo, desc, root, head);
        m.put("deactivated_at", deactivatedAt);
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
        // nexus-cecqy: JSON booleans coerce to 0/1. Our integer columns that model flags
        // (catalog_collections.legacy_grandfathered) are sent by Python clients as real
        // JSON booleans, and this returned null for them — so the caller's value was
        // silently replaced by the ni(..., 0) default and the flag could NEVER be set
        // through /collections/upsert, by any caller, explicit or derived. That is why
        // the client-side derivation alone did not move the stored value.
        if (v instanceof Boolean b) return b ? 1 : 0;
        return null;
    }

    private static Double dbl(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v instanceof Number n) return n.doubleValue();
        return null;
    }

    /** Boolean field extraction tolerant of a real JSON boolean or its string form. */
    private static boolean bool(Map<String, Object> m, String k, boolean def) {
        Object v = m.get(k);
        if (v == null) return def;
        if (v instanceof Boolean b) return b;
        return "true".equalsIgnoreCase(String.valueOf(v));
    }

    /** Non-null empty: returns "" if null. */
    private static String nne(String v) { return v != null ? v : ""; }

    /**
     * nexus-4j80w: {@code upsertLink}'s created_at default. The client never
     * sends {@code created_at}; {@code nne()} alone defaulted it to {@code ""},
     * and {@code "" < any-date} is TRUE under lexical TEXT comparison, so every
     * service-written link matched EVERY {@code created_at_before} predicate —
     * {@code bulk_unlink --created-at-before} deleted the entire link graph,
     * and the MCP confirmation preview computed through the same predicate so
     * it read as correct.
     *
     * <p>Stamp a REAL ISO-8601 UTC timestamp on insert instead, in the same
     * fixed-width-micros + {@code "+00:00"} shape {@link #stampIndexedAt} uses
     * (via {@link #INDEXED_AT_FMT}) — byte-identical to the local arm's
     * {@code datetime.now(UTC).isoformat()}, so old (local-written) and new
     * (service-written) rows sort consistently as TEXT. Only applies to the
     * INSERT values list; the {@code doUpdate()} merge path never touches
     * {@code created_at}, so an existing row's timestamp is never overwritten.
     */
    private static String createdAtOrNow(String createdAt) {
        if (createdAt != null && !createdAt.isBlank()) return createdAt;
        return java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC).format(INDEXED_AT_FMT);
    }

    /**
     * Null-or-empty normalizer: returns null for null or blank/empty strings, else returns v.
     * Use for timestamptz columns (catalog_collections.created_at / superseded_at) where the
     * SQLite heritage used '' as the empty sentinel.  Binding '' into a timestamptz column
     * fails at the JDBC driver layer; NULL is the correct representation of "not set".
     * catalog-002-1-temporal-typing (RDR-156 P0.2) converts these columns to timestamptz NULL.
     */
    private static String nz(String v) { return (v != null && !v.isEmpty()) ? v : null; }

    /**
     * ISO-8601-or-blank to a typed timestamptz bind (nexus-xtmtf): blank/null
     * -> NULL (the nullable temporal columns' "unset" state after
     * catalog-002-1-temporal-typing). Accepts the same lenient shapes the
     * retired {@code ?::timestamptz} cast did — space-separated
     * ("2026-05-01 12:00:00") and offsetless forms from legacy-SQLite
     * fidelity imports parse as UTC. Genuinely unparseable input fails loud
     * (as the cast did) — these are fidelity-preserving import/supersede
     * timestamps, not event stamps, so never substitute now().
     */
    // Package-private for direct unit testing (CatalogTsOrNullTest).
    static java.time.OffsetDateTime tsOrNull(String iso) {
        if (iso == null || iso.isBlank()) return null;
        String normalized = iso.trim().replace(' ', 'T');
        try {
            return java.time.OffsetDateTime.parse(normalized);
        } catch (java.time.format.DateTimeParseException e) {
            // Offsetless (legacy SQLite catalog rows) — timestamptz text input
            // without a zone resolves in the session TZ; the service runs UTC.
            try {
                return java.time.LocalDateTime.parse(normalized)
                           .atOffset(java.time.ZoneOffset.UTC);
            } catch (java.time.format.DateTimeParseException e2) {
                // Date-only ("2026-05-01") — the retired ?::timestamptz cast
                // accepted bare dates as midnight; preserve that (review
                // finding: this branch was missing and threw uncaught).
                return java.time.LocalDate.parse(normalized)
                           .atStartOfDay().atOffset(java.time.ZoneOffset.UTC);
            }
        }
    }

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

    // ══════════════════════════════════════════════════════════════════════════
    // GC AUDIT  (nexus-jqvzk — engine-side record for destructive T3 ops)
    // ══════════════════════════════════════════════════════════════════════════

    /** Hard cap on chashes persisted per audit row; the count is always exact. */
    public static final int GC_AUDIT_MAX_CHASHES = 5000;

    /**
     * Record ONE destructive (or dry-run) T3 operation (nexus-jqvzk).
     *
     * <p>Append-only: an audit row is never updated or deleted by this service.
     * The row is written on the CALLER's say-so — the engine does not perform
     * the T3 deletion itself (the gc verb does), so this records what the
     * caller reports, and the caller writes it in the same breath as the
     * delete. That is the honest boundary: it is an audit trail, not a
     * two-phase commit.
     *
     * <p>{@code chashes} is TRUNCATED at {@value #GC_AUDIT_MAX_CHASHES} entries
     * so one enormous collection-wide sweep cannot write an unbounded row;
     * {@code chash_count} always carries the FULL count, and
     * {@code details.chashes_truncated} records that the list is partial —
     * never a silently short list.
     *
     * @param audit {@code {operation, collection, actor, dry_run, chashes[], details{}}}
     * @return the new audit row's id
     */
    public long recordGcAudit(String tenant, Map<String, Object> audit) {
        String operation = s(audit, "operation", "");
        if (operation.isBlank()) {
            throw new IllegalArgumentException("gc_audit: 'operation' is required");
        }
        List<String> chashes = new ArrayList<>();
        if (audit.get("chashes") instanceof List<?> raw) {
            for (Object o : raw) {
                if (o != null) chashes.add(o.toString());
            }
        }
        int fullCount = chashes.size();
        boolean truncated = fullCount > GC_AUDIT_MAX_CHASHES;
        List<String> stored = truncated ? chashes.subList(0, GC_AUDIT_MAX_CHASHES) : chashes;

        Map<String, Object> details = new LinkedHashMap<>();
        if (audit.get("details") instanceof Map<?, ?> d) {
            for (var e : d.entrySet()) details.put(String.valueOf(e.getKey()), e.getValue());
        }
        if (truncated) {
            details.put("chashes_truncated", true);
            details.put("chashes_stored", GC_AUDIT_MAX_CHASHES);
        }

        boolean dryRun = Boolean.TRUE.equals(audit.get("dry_run"))
            || "true".equalsIgnoreCase(String.valueOf(audit.get("dry_run")));

        return tenantScope.withTenant(tenant, ctx ->
            ctx.insertInto(GC_AUDIT)
               .set(GC_AUDIT.TENANT_ID,   tenant)
               .set(GC_AUDIT.OPERATION,   operation)
               .set(GC_AUDIT.COLLECTION,  s(audit, "collection", ""))
               .set(GC_AUDIT.ACTOR,       s(audit, "actor", ""))
               .set(GC_AUDIT.DRY_RUN,     dryRun)
               .set(GC_AUDIT.CHASH_COUNT, fullCount)
               .set(gcAuditChashes(),     jsonbVal(jsonOrNull(stored)))
               .set(gcAuditDetails(),     jsonbVal(details.isEmpty() ? null : jsonOrNull(details)))
               .returningResult(GC_AUDIT.ID)
               .fetchOne()
               .value1());
    }

    /**
     * Read the audit trail, newest first (nexus-jqvzk).
     *
     * @param collection optional exact {@code physical_collection} filter
     * @param operation  optional exact operation filter
     */
    public List<Map<String, Object>> listGcAudit(String tenant, String collection,
                                                  String operation, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = GC_AUDIT.TENANT_ID.eq(tenant);
            if (collection != null && !collection.isBlank()) cond = cond.and(GC_AUDIT.COLLECTION.eq(collection));
            if (operation != null && !operation.isBlank())   cond = cond.and(GC_AUDIT.OPERATION.eq(operation));
            return ctx.select(GC_AUDIT.ID, GC_AUDIT.OPERATION, GC_AUDIT.COLLECTION, GC_AUDIT.ACTOR,
                              GC_AUDIT.DRY_RUN, GC_AUDIT.CHASH_COUNT, gcAuditChashes(), gcAuditDetails(),
                              GC_AUDIT.CREATED_AT)
                      .from(GC_AUDIT)
                      .where(cond)
                      .orderBy(GC_AUDIT.ID.desc())
                      .limit(limit <= 0 ? 100 : limit)
                      .offset(Math.max(offset, 0))
                      .fetch()
                      .map(r -> {
                          Map<String, Object> m = new LinkedHashMap<>();
                          m.put("id",          r.value1());
                          m.put("operation",   r.value2());
                          m.put("collection",  r.value3());
                          m.put("actor",       r.value4());
                          m.put("dry_run",     r.value5());
                          m.put("chash_count", r.value6());
                          m.put("chashes",     parseJsonList(r.value7()));
                          m.put("details",     parseJsonMap(r.value8()));
                          m.put("created_at",  r.value9() != null ? r.value9().toString() : null);
                          return m;
                      });
        });
    }

    /** jsonb columns read as String so the same MAPPER round-trip as F_DOC_META applies. */
    private static Field<String> gcAuditChashes() {
        return DSL.field(DSL.name("gc_audit", "chashes"), String.class);
    }

    private static Field<String> gcAuditDetails() {
        return DSL.field(DSL.name("gc_audit", "details"), String.class);
    }

    private static List<Object> parseJsonList(String json) {
        if (json == null || json.isBlank()) return List.of();
        try {
            return MAPPER.readValue(json, new TypeReference<List<Object>>() {});
        } catch (Exception e) {
            log.warn("event=gc_audit_chashes_unparseable error={}", e.getMessage());
            return List.of();
        }
    }

    private static Map<String, Object> parseJsonMap(String json) {
        if (json == null || json.isBlank()) return Map.of();
        try {
            return MAPPER.readValue(json, MAP_TYPE);
        } catch (Exception e) {
            log.warn("event=gc_audit_details_unparseable error={}", e.getMessage());
            return Map.of();
        }
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
