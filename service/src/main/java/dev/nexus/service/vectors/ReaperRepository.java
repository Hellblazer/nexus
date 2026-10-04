// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;

import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.impl.DSL;

import java.time.Duration;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Routines.reaperOwnsQuarantinedRow;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_CLIENT_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_QUARANTINE_CHUNKS;

/**
 * RDR-192 Step 9 (bead nexus-2x9xa): the database half of the periodic reaper ({@code dev.nexus.service.ChunkReaper}).
 *
 * <p>Deliberately thin. Reapability is decided inside {@code nexus.reaper_quarantine_chunks} (vectors-024) by
 * {@code nexus.chunk_is_reapable} and by nothing else, in the move statement's own WHERE; this class never reads a
 * chunk's age or compares anything itself, so there is no second definition for the predicate to drift from. The
 * three questions it does answer are which collections hold chunks (from {@code nexus.chunks}, so a collection with
 * chunks and no catalog row is seen and refused rather than never listed), what lifecycle state each registered
 * collection is in, and the dry-run and move calls.
 */
public class ReaperRepository {   // not final: ChunkReaperIntegrationTest raises a statement timeout from a subclass

    /**
     * What one call to {@code reaper_quarantine_chunks} reports: {@code moved} chunks moved, the collection's whole
     * {@code reapable} count and {@code total} chunk count as that call saw them, {@code refused} when the fraction
     * floor stopped the move, and the {@code remaining} reapable count after it.
     */
    public record Pass(long moved, long reapable, long total, boolean refused, long remaining) {}

    /**
     * What one call to {@code reaper_expire_quarantine} reports: {@code expired} chunks deleted (counted from the
     * DELETE's own RETURNING) and {@code protectedCount} chunks past the cutoff a manifest row of the origin still
     * names (benign: re-referenced after the move, never deleted, nothing to act on). There is no floor and so no
     * refused count (Sam, 2026-10-01).
     */
    public record Expiry(long expired, long protectedCount) {}

    private final TenantScope tenantScope;

    public ReaperRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * Every collection this tenant holds at least one chunk in, quarantine siblings included (the caller skips
     * them by name). Bounded by {@code statementTimeout}: the enumeration runs before any collection is chosen, so
     * an unbounded scan here would stall the whole pass, and the single scheduler thread behind it.
     */
    public List<String> collectionsWithChunks(String tenant, Duration statementTimeout) {
        return tenantScope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, (int) statementTimeout.toMillis(), 2_000);
            return ctx.selectDistinct(CHUNKS.COLLECTION).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant))
                .orderBy(CHUNKS.COLLECTION)
                .fetch(CHUNKS.COLLECTION);
        });
    }

    /**
     * True when the tenant holds no chunk row in any collection, quarantine siblings included (nexus-wbfpw.73;
     * {@code Rdr192BackfillGate}). Read under the tenant's own RLS context, bounded by {@code statementTimeout}.
     * Throws on a database failure, never answers true for "could not read".
     *
     * <p>One read is enough, and the catalog's manifest is deliberately not consulted. Every manifest row references
     * its chunk through {@code fk_catalog_chunks_chunk} (catalog-029, validated), so a manifest row cannot outlive
     * the chunk it names: a tenant with no chunk has no manifest row either, and a second existence read could
     * never answer differently. It was written once and no test could reach it, because the same FK refuses the
     * seed. The client rung's own empty-listing branch also cross-checks the catalog, but that guards a listing
     * that failed silently; this read is the table itself and fails loudly.
     */
    public boolean holdsNothing(String tenant, Duration statementTimeout) {
        return tenantScope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, (int) statementTimeout.toMillis(), 2_000);
            return !ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(tenant));
        });
    }

    /**
     * {@code name -> lifecycle_state} for every registered collection of the tenant. A name that is absent is not
     * registered; a registered collection with no state maps to the empty string, which is not {@code live}.
     */
    public Map<String, String> lifecycleStates(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, String> out = new LinkedHashMap<>();
            ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
               .from(CATALOG_COLLECTIONS)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
               .forEach(r -> out.put(r.value1(), r.value2() == null ? "" : r.value2()));
            return out;
        });
    }

    /** A chunk named by its chash, with the two fields an operator recognises it by. Either may be null. */
    public record ChunkLabel(String chash, String title, String sourcePath) {}

    /**
     * The {@code title} and {@code source_path} the chunk's own metadata carries, for the chunks of {@code collection}
     * named by {@code chashHex}. Display only: a refusal's audit row names a handful of what it refused to move, so
     * an operator can tell a stale index from a mass orphaning without a database shell. A chash that is no longer
     * stored is simply absent.
     */
    public List<ChunkLabel> describe(String tenant, String collection, List<String> chashHex) {
        if (chashHex.isEmpty()) return List.of();
        List<byte[]> keys = chashHex.stream().map(h -> dev.nexus.service.db.Chash.fromHex(h).toBytes()).toList();
        Field<String> title = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "title");
        Field<String> sourcePath = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "source_path");
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(CHUNKS.CHASH, title, sourcePath).from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.in(keys)))
               .orderBy(CHUNKS.CHASH)
               .fetch(r -> new ChunkLabel(HexFormat.of().formatHex(r.value1()),
                   r.value2() == null || r.value2().isEmpty() ? null : r.value2(),
                   r.value3() == null || r.value3().isEmpty() ? null : r.value3())));
    }

    /**
     * Counts only: how many of the collection's chunks are reapable under {@code grace} (null: the predicate's own
     * 30 days). Takes no gate and moves nothing.
     */
    public Pass probe(String tenant, String collection, Duration grace, int statementTimeoutMs) {
        return call(tenant, collection, "", "", 1, grace, 0.0, 0, true, statementTimeoutMs, 2_000);
    }

    /**
     * Moves at most {@code rowLimit} reapable chunks of {@code collection} into {@code quarantineCollection}, unless
     * they are more than {@code floorFraction} of a collection of at least {@code floorMinChunks} chunks, in which
     * case nothing moves and {@link Pass#refused()} is true. Throws on a database error; a sweep gate that could not
     * be taken in {@code lockTimeoutMs}'s neighbourhood surfaces as SQLSTATE 55P03.
     */
    public Pass move(String tenant, String collection, String quarantineCollection, String quarantinedAt,
                     int rowLimit, Duration grace, double floorFraction, int floorMinChunks,
                     int statementTimeoutMs, int lockTimeoutMs) {
        return call(tenant, collection, quarantineCollection, quarantinedAt, rowLimit, grace, floorFraction,
                    floorMinChunks, false, statementTimeoutMs, lockTimeoutMs);
    }

    /**
     * The origins of the chunks the reaper itself moved into {@code quarantineCollection}: the distinct
     * {@code origin_collection} tags of its engine-owned rows ({@code nexus.reaper_owns_quarantined_row}, the one
     * predicate the expiry functions call too). The expiry reads the origin from the chunk, never by parsing the
     * sibling's name, so a sibling whose name is not {@code quarantine-<origin>} (the Python client builds it from the origin's catalog
     * row, which agrees with the name only for a conformant one) is still expired against the right manifest.
     */
    public List<String> taggedOrigins(String tenant, String quarantineCollection, int statementTimeoutMs) {
        Field<String> origin = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection");
        // The ownership test is the database's one definition (vectors-024-2), never the three keys written here.
        Condition engineOwned = DSL.condition(reaperOwnsQuarantinedRow(CHUNKS.METADATA));
        return tenantScope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, statementTimeoutMs, 2_000);
            // No ORDER BY: SELECT DISTINCT cannot order by an expression it rebinds. Sorted below instead.
            return ctx.selectDistinct(origin).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(quarantineCollection))
                    .and(engineOwned).and(origin.isNotNull()))
                .fetch(origin).stream().sorted().toList();
        });
    }

    /**
     * The engine's expiry, for the chunks the reaper itself moved and no others: deletes at most {@code rowLimit}
     * tagged chunks of {@code quarantineCollection} (origin {@code originCollection}) stamped at or before
     * {@code cutoff} ({@code YYYY-MM-DDTHH:MM:SSZ}), never one a manifest row of the origin names. There is no
     * fraction floor. Quarantine a client filled carries no tag and is not touched. Throws on a database error; a
     * lock wait that times out surfaces as SQLSTATE 55P03 and a statement that hits its bound as 57014.
     */
    public Expiry expire(String tenant, String quarantineCollection, String originCollection, String cutoff,
                         int rowLimit, int statementTimeoutMs, int lockTimeoutMs) {
        var rec = tenantScope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, statementTimeoutMs, lockTimeoutMs);
            return ctx.selectFrom(REAPER_EXPIRE_QUARANTINE.call(
                    tenant, quarantineCollection, originCollection, cutoff, rowLimit))
               .fetchOne();
        });
        return new Expiry(rec.get(REAPER_EXPIRE_QUARANTINE.EXPIRED), rec.get(REAPER_EXPIRE_QUARANTINE.PROTECTED_COUNT));
    }

    /**
     * The origins of the chunks a CLIENT moved into {@code quarantineCollection} (nexus-wbfpw.75): the distinct
     * {@code origin_collection} tags of its rows that the reaper does not own
     * ({@code nexus.reaper_owns_quarantined_row} is not true, the complement of {@link #taggedOrigins}), limited to
     * the scope {@code nexus.reaper_expire_client_quarantine} itself enforces, all of it registry rows and never a
     * parse of a name: the quarantine collection is registered with catalog {@code content_type}
     * {@code contentType} and lifecycle state {@code quarantine}, and the origin is registered with that content
     * type and lifecycle state {@code live} (restore, {@code vectors-025}, requires a live origin too). A row with
     * no origin tag names no origin and is not listed: the engine never derives an origin from the sibling's name.
     */
    public List<String> clientMovedOrigins(String tenant, String quarantineCollection, String contentType,
                                           int statementTimeoutMs) {
        Field<String> origin = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection");
        // The function is total (true or false, never NULL: vectors-024-2), so NOT agrees with IS NOT TRUE.
        Condition clientMoved = DSL.not(DSL.condition(reaperOwnsQuarantinedRow(CHUNKS.METADATA)));
        return tenantScope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, statementTimeoutMs, 2_000);
            return ctx.selectDistinct(origin).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(quarantineCollection))
                    .and(clientMoved).and(origin.isNotNull())
                    .and(DSL.exists(DSL.selectOne().from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                            .and(CATALOG_COLLECTIONS.NAME.eq(quarantineCollection))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq(contentType))
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("quarantine")))))
                    .and(DSL.exists(DSL.selectOne().from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(CHUNKS.TENANT_ID)
                            .and(CATALOG_COLLECTIONS.NAME.eq(origin))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq(contentType))
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live"))))))
                .fetch(origin).stream().sorted().toList();
        });
    }

    /**
     * The engine's expiry of what a client moved ({@code nexus.reaper_expire_client_quarantine}, vectors-028): at
     * most {@code rowLimit} rows of {@code quarantineCollection} that the reaper does not own, tagged for
     * {@code originCollection}, stamped at or before {@code cutoff}, never one a manifest row of the origin names.
     * The function itself refuses to touch anything but a registered quarantine knowledge collection and a registered
     * live knowledge origin, and refuses a cutoff that is not {@code yyyy-MM-ddTHH:mm:ssZ}. No fraction floor. One {@code reaper_expire_client_quarantine} audit row per call
     * that deletes. Throws on a database error; a lock wait that times out surfaces as SQLSTATE 55P03 and a
     * statement that hits its bound as 57014.
     */
    public Expiry expireClientMoved(String tenant, String quarantineCollection, String originCollection,
                                    String cutoff, int rowLimit, int statementTimeoutMs, int lockTimeoutMs) {
        var rec = tenantScope.withTenant(tenant, ctx -> {
            PgSession.setStatementAndLockBounds(ctx, statementTimeoutMs, lockTimeoutMs);
            return ctx.selectFrom(REAPER_EXPIRE_CLIENT_QUARANTINE.call(
                    tenant, quarantineCollection, originCollection, cutoff, rowLimit))
               .fetchOne();
        });
        return new Expiry(rec.get(REAPER_EXPIRE_CLIENT_QUARANTINE.EXPIRED),
                          rec.get(REAPER_EXPIRE_CLIENT_QUARANTINE.PROTECTED_COUNT));
    }

    private Pass call(String tenant, String collection, String quarantineCollection, String quarantinedAt,
                      int rowLimit, Duration grace, double floorFraction, int floorMinChunks, boolean dryRun,
                      int statementTimeoutMs, int lockTimeoutMs) {
        org.jooq.types.YearToSecond interval = grace == null ? null : new org.jooq.types.YearToSecond(
            new org.jooq.types.YearToMonth(0, 0), org.jooq.types.DayToSecond.valueOf(grace));
        var rec = tenantScope.withTenant(tenant, ctx -> {
            // The statement bound is its OWN statement before the call: the function body's own set_config of
            // statement_timeout cannot bound the statement already running it.
            PgSession.setStatementAndLockBounds(ctx, statementTimeoutMs, lockTimeoutMs);
            return ctx.selectFrom(REAPER_QUARANTINE_CHUNKS.call(
                    tenant, collection, quarantineCollection, quarantinedAt, rowLimit, interval,
                    floorFraction, floorMinChunks, dryRun))
               .fetchOne();
        });
        return new Pass(rec.get(REAPER_QUARANTINE_CHUNKS.MOVED), rec.get(REAPER_QUARANTINE_CHUNKS.REAPABLE_COUNT),
                        rec.get(REAPER_QUARANTINE_CHUNKS.TOTAL_COUNT), rec.get(REAPER_QUARANTINE_CHUNKS.REFUSED),
                        rec.get(REAPER_QUARANTINE_CHUNKS.REMAINING));
    }
}
