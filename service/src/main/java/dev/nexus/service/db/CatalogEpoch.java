// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_META;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.Optional;

/**
 * The catalog epoch (RDR-229 item 10 / P3.5a, bead nexus-p2m1x.23; ruling by the lead, linda, 2026-11-20, refined
 * 2026-11-22): a short token a client cache keyed by {@code (catalog_epoch, collection_id)} compares to know that
 * its ids may no longer mean what they meant. The cache must drop on a tenant restore and on a point-in-time
 * rewind of the database.
 *
 * <p>A value held only in a table rewinds with the data, so it cannot see a PITR rewind: the rewound table
 * holds the old value and the cache would keep ids the rewind has re-minted. The cluster part carries the rewind.
 * The epoch is the first 16 hex characters of
 * {@code sha256(system_identifier ":" timeline_id ":" marker)}:
 * <ul>
 *   <li>{@code system_identifier} from {@code pg_control_system()}: a restore into a fresh cluster changes it.</li>
 *   <li>{@code timeline_id} from {@code pg_control_checkpoint()}: a PITR recovery ends on a new timeline.</li>
 *   <li>{@code marker}: the value of the tenant's {@code nexus.catalog_meta} row with key
 *       {@link TenantImportLoad#MARKER_KEY}, or the empty string when there is none. A tenant restore rewrites
 *       that row, so it changes that tenant's epoch and no other tenant's.</li>
 * </ul>
 * A failover promotion also changes the timeline. That is accepted: it costs one re-resolve by label.
 *
 * <p><b>A missing privilege means no epoch.</b> The pg_control functions are not always executable by the
 * engine's role. When either is not, {@link #of} returns empty, and the caller reads that as unstable: it sends no
 * epoch and the client does not trust a cache. The privilege is checked with {@code has_function_privilege} in
 * its own SELECT before either function is called, because a 42501 raised inside the caller's transaction would
 * abort it. This class never throws for a missing privilege.
 *
 * <p>Stateless and static: no cache, no singleton. Every call reads the cluster and the marker.
 */
final class CatalogEpoch {

    /** The two cluster facts the epoch hashes. */
    record ClusterIdentity(String systemIdentifier, String timelineId) {}

    private static final int HEX_CHARS = 16;

    private CatalogEpoch() {}

    /** The epoch of {@code tenant} as read from the cluster {@code ctx} is connected to, or empty when the role cannot read it. */
    static Optional<String> of(DSLContext ctx, String tenant) {
        return probe(ctx).map(cluster -> of(ctx, tenant, cluster));
    }

    /** The test seam: the epoch for a given cluster identity. The marker is still read from {@code ctx}. */
    static String of(DSLContext ctx, String tenant, ClusterIdentity cluster) {
        String marker = ctx.select(CATALOG_META.VALUE)
            .from(CATALOG_META)
            .where(CATALOG_META.TENANT_ID.eq(tenant))
            .and(CATALOG_META.KEY.eq(TenantImportLoad.MARKER_KEY))
            .fetchOptional(CATALOG_META.VALUE)
            .orElse("");
        return sha256Prefix(cluster.systemIdentifier() + ":" + cluster.timelineId() + ":" + marker);
    }

    private static Optional<ClusterIdentity> probe(DSLContext ctx) {
        Boolean canRead = ctx.select(DSL.field(canExecute(DSL.inline("pg_catalog.pg_control_system()"))
            .and(canExecute(DSL.inline("pg_catalog.pg_control_checkpoint()"))))).fetchOne(0, Boolean.class);
        if (!Boolean.TRUE.equals(canRead)) return Optional.empty();
        Field<String> system = member(DSL.function(DSL.name("pg_catalog", "pg_control_system"), SQLDataType.JSONB),
            "system_identifier");
        Field<String> timeline = member(DSL.function(DSL.name("pg_catalog", "pg_control_checkpoint"), SQLDataType.JSONB),
            "timeline_id");
        var row = ctx.select(system, timeline).fetchOne();
        if (row == null || row.value1() == null || row.value2() == null) return Optional.empty();
        return Optional.of(new ClusterIdentity(row.value1(), row.value2()));
    }

    /** {@code has_function_privilege(signature, 'EXECUTE')}, for the current role. */
    private static Condition canExecute(Field<String> signature) {
        return DSL.function("has_function_privilege", Boolean.class, signature, DSL.inline("EXECUTE"))
            .isTrue();
    }

    /** One column of a function's composite result: {@code jsonb_extract_path_text(to_jsonb(fn()), column)}. */
    private static Field<String> member(Field<?> composite, String column) {
        return DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR,
            DSL.function("to_jsonb", SQLDataType.JSONB, composite), DSL.val(column));
    }

    private static String sha256Prefix(String text) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest).substring(0, HEX_CHARS);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is required of every JVM", e);
        }
    }
}
