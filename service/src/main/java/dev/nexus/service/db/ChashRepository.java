package dev.nexus.service.db;

import dev.nexus.service.vectors.DimTables;
import org.jooq.DSLContext;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-187 (bead nexus-piwya.3) — the chash lookup surface, served from the
 * chunks tables.
 *
 * <p>Until RDR-187 this class fronted {@code nexus.chash_index}, the router
 * remnant of the split-store architecture: a dual-written copy of "which
 * collections hold this chash" that leaked orphans on every deletion path
 * (292,230 rows dangling in production). The chunks tables
 * ({@code chunks_384/768/1024}) ARE the chash-keyed store — the PK is
 * {@code (tenant_id, collection, chash)} — so every question the router
 * answered is answered here by probing them directly, using the
 * {@code idx_chunks_<dim>_tenant_chash} indexes (nexus-piwya.1).
 *
 * <p>Conformance contract (pinned by {@code ChashRerouteConformanceTest}):
 * answers are a SUPERSET of the router's — exact agreement on every router
 * row backed by a real chunk, plus resolution of RDR-169 reference-only
 * chunks the router never knew. Router rows without a backing chunk (the
 * orphan class) are correctly NOT resolved. {@code created_at} carries the
 * identical "when this chash entered this collection" semantics
 * (first-insert-per-key; both chunk upsert paths exclude it from their
 * ON CONFLICT set-lists — RDR-187 research finding 1).
 *
 * <p>WRITES ARE GONE: the router was the only thing written. The chunks
 * tables are written by the vector ingest paths ({@code PgVectorRepository});
 * {@code ChashHandler} accepts the old write shapes as deprecated no-ops for
 * one release (mixed-version window, RDR-187 finding 3). The exceptions:
 * {@link #renameCollection} stays REAL (rerouted to re-home
 * {@code chunks_<dim>.collection}; idempotent when the RDR-164 catalog
 * cascade already did the work), and {@link #resolveLegacyRef} reads the
 * PERMANENT {@code chash_alias} map (out of RDR-187's scope by design).
 *
 * <p>All methods route through {@link TenantScope#withTenant} so every row
 * access is stamped with the tenant GUC and enforced by RLS.
 */
public final class ChashRepository {

    /**
     * UTC second-precision ISO-8601 formatter matching Python's
     * {@code datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}.
     */
    public static final DateTimeFormatter UTC_SECOND =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                             .withZone(ZoneOffset.UTC);

    /**
     * The lookup probe: which collections hold this chash for the current
     * tenant, with the first-insert timestamp. Three PK-disjoint tables, so
     * UNION ALL (a collection lives in exactly one dim table; no duplicate
     * (collection, chash) pairs are possible across legs). The explicit
     * tenant_id predicate binds the leading column of
     * {@code idx_chunks_<dim>_tenant_chash}; RLS supplies the same qual for
     * the serving role, and the plan keeps the index either way (pinned at
     * 255k-row scale by {@code ChashProbePlanShapeTest}).
     *
     * <p>Public so plan-shape tests EXPLAIN exactly the shipped SQL.
     */
    public static final String PROBE_SQL =
        "SELECT collection, created_at FROM nexus.chunks_384 " +
        " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ? " +
        "UNION ALL " +
        "SELECT collection, created_at FROM nexus.chunks_768 " +
        " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ? " +
        "UNION ALL " +
        "SELECT collection, created_at FROM nexus.chunks_1024 " +
        " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ?";

    private static final int[] DIMS = {384, 768, 1024};

    private final TenantScope tenantScope;

    public ChashRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * RDR-156 P0.2: ensure catalog_collections has a stub row for the given
     * collection before a rename re-homes rows onto it (fk-002 RESTRICT).
     * Idempotent — ON CONFLICT DO NOTHING. In-transaction deliberately:
     * rename's registration must be atomic with the re-point.
     *
     * @throws IllegalArgumentException if collection is null or blank
     */
    private static void ensureCollectionRegistered(DSLContext ctx, String tenant, String collection) {
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("collection must not be blank");
        }
        if (CollectionRegistry.isKnown(tenant, collection)) {
            return;
        }
        ctx.insertInto(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .values(tenant, collection)
           .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .doNothing()
           .execute();
    }

    // ── legacy-reference resolution (RDR-180 Item3 read seam) ─────────────────

    /**
     * Resolve a LEGACY reference (pre-RDR-180 32-hex chunk id, or an ETL-era
     * external id) to its canonical chash via the permanent
     * ``nexus.chash_alias`` map (nexus-jxizy.6). Returns null when the map
     * holds no fact for *oldRef* — the caller treats that as chash-not-found
     * (empty rows), never an error: the alias map is the collision-free
     * resolver, and an unmapped legacy reference is simply dangling.
     *
     * <p>PERMANENT: chash_alias is explicitly out of RDR-187's scope; this
     * read seam outlives the router.
     */
    public Chash resolveLegacyRef(String tenant, String oldRef) {
        if (oldRef == null || oldRef.isBlank()) return null;
        return tenantScope.withTenant(tenant, ctx -> {
            var row = ctx.select(dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS.NEW_CHASH)
                         .from(dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS)
                         .where(dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS.OLD_REF.eq(oldRef))
                         .fetchOne();
            if (row == null || row.value1() == null) return null;
            return Chash.fromSha256Bytes(row.value1());
        });
    }

    // ── lookup ─────────────────────────────────────────────────────────────────

    /**
     * Return all {@code (collection, created_at)} rows for {@code chash} —
     * the collections whose chunk store actually holds this content.
     *
     * <p>Returns an empty list when {@code chash} is unknown. Response keys
     * ({@code collection}, {@code created_at}) and the second-precision UTC
     * timestamp format are unchanged from the router era — the HTTP shape is
     * part of the RDR-187 compatibility contract.
     */
    // SANCTIONED RAW (nexus-piwya.3): PROBE_SQL is the PUBLISHED probe
    // statement — ChashProbePlanShapeTest EXPLAINs the constant verbatim to
    // pin index usage at 255k-row scale, so the executed SQL and the tested
    // SQL must be the same string by construction. A jOOQ DSL rendering
    // would decouple them (the pin would test a hand-maintained copy).
    public List<Map<String, String>> lookup(String tenant, Chash chash) {
        byte[] bytes = chash.toBytes();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.resultQuery(PROBE_SQL, bytes, bytes, bytes).fetch();
            List<Map<String, String>> result = new ArrayList<>(rows.size());
            for (var r : rows) {
                OffsetDateTime ts = r.get("created_at", OffsetDateTime.class);
                String tsStr = ts != null ? UTC_SECOND.format(ts.atZoneSameInstant(ZoneOffset.UTC)) : "";
                result.add(Map.of("collection", r.get("collection", String.class), "created_at", tsStr));
            }
            return result;
        });
    }

    // ── distinct_collections ───────────────────────────────────────────────────

    /**
     * Return every distinct collection holding at least one chunk for this
     * tenant.
     *
     * <p>Used by {@code nx catalog chash-reconcile} to identify ghost
     * collections. Chunk-backed truth: registry stubs with zero chunks do
     * not appear (matching the router's behavior, which only knew
     * collections that had received rows).
     */
    public Set<String> distinctCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            Set<String> result = new HashSet<>();
            for (int dim : DIMS) {
                DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
                result.addAll(ctx.selectDistinct(ch.collection())
                                 .from(ch.table())
                                 .where(ch.tenantId().eq(tenant))
                                 .fetch(ch.collection()));
            }
            return result;
        });
    }

    // ── rename_collection ──────────────────────────────────────────────────────

    /**
     * Re-home every chunk row from {@code oldCollection} to
     * {@code newCollection} across the three dim tables, AND the manifest's
     * denormalized collection column. Returns the count of chunk rows
     * updated (manifest rows are not counted — shape parity with the router
     * era's return value).
     *
     * <p>KEPT REAL under RDR-187 (design Q3): the RDR-164 catalog cascade
     * ({@code CatalogRepository.renameCollection}) re-homes chunk rows, the
     * manifest, and the registry in one transaction, so this direct endpoint
     * usually matches 0 rows — idempotent belt-and-suspenders, real work
     * only for a cascade-less caller.
     *
     * <p>The manifest leg ({@code catalog_document_chunks.collection}) is
     * here because this rename now moves REAL chunk rows: renaming chunks
     * without the manifest would strand the combined-query join key on the
     * old name — the exact silently-empty-join class nexus-x6kdz closed for
     * the cascade path (.3 critique S2). The router-era version moved only
     * routing pointers, so it never had this exposure; matching the
     * cascade's guarantee is what keeps Q3's "no new window case" claim
     * true. Idempotent under either topology (0 rows when the cascade
     * already re-homed).
     *
     * <p>Collision policy is INTENTIONALLY REVERSED from the router era
     * (.3 review finding 1): the router deleted the colliding NEW-side row
     * and moved OLD's row (OLD won, OLD's created_at survived); here rows
     * already present in {@code newCollection} with the same chash win and
     * the colliding {@code oldCollection} copy is dropped. Deleting the
     * NEW-side CHUNK row to preserve a timestamp would discard real content
     * state for a tiebreak field (RDR-187 Q1: created_at ordering is a
     * tiebreak, not a correctness gate); the content is identical either
     * way by definition of the chash.
     */
    public int renameCollection(String tenant, String oldCollection, String newCollection) {
        if (oldCollection == null || oldCollection.isBlank()
                || newCollection == null || newCollection.isBlank()) {
            throw new IllegalArgumentException("old and new collection must not be empty");
        }
        int updated = tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, newCollection);
            int total = 0;
            for (int dim : DIMS) {
                DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
                // Drop OLD rows whose chash already exists under NEW (NEW-side
                // row wins — see the collision-policy paragraph above).
                ctx.deleteFrom(ch.table())
                   .where(ch.tenantId().eq(tenant)
                       .and(ch.collection().eq(oldCollection))
                       .and(ch.chash().in(
                           ctx.select(ch.chash())
                              .from(ch.table())
                              .where(ch.tenantId().eq(tenant)
                                  .and(ch.collection().eq(newCollection))))))
                   .execute();
                total += ctx.update(ch.table())
                            .set(ch.collection(), newCollection)
                            .where(ch.tenantId().eq(tenant)
                                .and(ch.collection().eq(oldCollection)))
                            .execute();
            }
            // Manifest re-home (collection is not part of its PK — no
            // collision handling needed).
            ctx.update(CATALOG_DOCUMENT_CHUNKS)
               .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, newCollection)
               .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                   .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(oldCollection)))
               .execute();
            return total;
        });
        // Post-commit (nexus-h8rf6.2): see CollectionRegistry class doc.
        CollectionRegistry.markKnown(tenant, newCollection);
        return updated;
    }

    // ── is_empty ───────────────────────────────────────────────────────────────

    /**
     * True when no chunk rows exist for this tenant — the "fresh install"
     * guard. Used by {@code nx doc cite} short-circuit.
     */
    public boolean isEmpty(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            for (int dim : DIMS) {
                DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
                if (ctx.fetchExists(ctx.selectOne()
                        .from(ch.table())
                        .where(ch.tenantId().eq(tenant)))) {
                    return false;
                }
            }
            return true;
        });
    }

    // ── count_for_collection ───────────────────────────────────────────────────

    /**
     * Return the chunk-row count for {@code collection} (summed across the
     * dim tables; a collection lives in exactly one, the others contribute
     * zero). Returns 0 for an unknown collection.
     */
    public int countForCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            int total = 0;
            for (int dim : DIMS) {
                DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
                Integer n = ctx.selectCount()
                               .from(ch.table())
                               .where(ch.tenantId().eq(tenant)
                                   .and(ch.collection().eq(collection)))
                               .fetchOne(0, Integer.class);
                if (n != null) total += n;
            }
            return total;
        });
    }

    // ── registered_chashes_for_collection ─────────────────────────────────────

    /**
     * Return every distinct chash present in {@code collection}, hex-encoded
     * (64-hex full digests — RDR-180 natural chunk IDs).
     *
     * <p>Used by the collection-audit coverage probe
     * ({@code collection_audit.py}): one set-difference against T3 chunk IDs
     * replaces the per-page IN-list query. Chunk-backed truth: the audit now
     * compares the chunk store against itself via the same table family,
     * which is exactly the RDR-187 end-state (no derived copy to drift).
     *
     * @param tenant     tenant principal (sets RLS GUC)
     * @param collection physical collection name to query
     * @return set of hex-encoded chashes; empty when collection is unknown
     */
    public Set<String> registeredChashesForCollection(String tenant, String collection) {
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("collection must not be empty");
        }
        return tenantScope.withTenant(tenant, ctx -> {
            Set<String> result = new HashSet<>();
            for (int dim : DIMS) {
                DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
                // ch.chash() is the ChashHex-converted field: fetches as
                // lowercase 64-hex directly.
                for (String hex : ctx.selectDistinct(ch.chash())
                                     .from(ch.table())
                                     .where(ch.tenantId().eq(tenant)
                                         .and(ch.collection().eq(collection)))
                                     .fetch(ch.chash())) {
                    if (hex != null && !hex.isEmpty()) {
                        result.add(hex);
                    }
                }
            }
            return result;
        });
    }
}
