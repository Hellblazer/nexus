// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_META;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.temporal.ChronoUnit;
import java.util.HexFormat;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * The catalog epoch (RDR-229 item 10 / P3.5a, bead nexus-p2m1x.23; ruling by the lead, linda, 2026-11-20, refined
 * 2026-11-22): a short token a client cache keyed by {@code (catalog_epoch, collection_id)} compares to know that
 * its ids may no longer mean what they meant. The cache must drop on a tenant restore, on a point-in-time rewind
 * of the database and on a snapshot restore.
 *
 * <p>A value held only in a table rewinds with the data, so it cannot see a PITR rewind or a snapshot restore: the
 * rewound table holds the old value and the cache would keep ids the rewind has re-minted. The cluster part carries
 * the rewind. The epoch is the first 16 hex characters of
 * {@code sha256(system_identifier ":" timeline_id ":" postmaster_start_time ":" marker)}:
 * <ul>
 *   <li>{@code system_identifier} from {@code pg_control_system()}: a restore into a fresh cluster changes it.</li>
 *   <li>{@code timeline_id} from {@code pg_control_checkpoint()}: a PITR recovery ends on a new timeline. A
 *       promotion changes it too, once the first checkpoint after the promotion has run.</li>
 *   <li>{@code postmaster_start_time} from {@code pg_postmaster_start_time()}, as epoch microseconds: a filesystem or
 *       volume snapshot restored on the same timeline always needs a PostgreSQL restart, which moves it. It is the
 *       same for every engine replica on one cluster, which is why it and not an engine boot token is used: replicas
 *       would disagree on a token and flap each other's client caches.</li>
 *   <li>{@code marker}: the value of the tenant's {@code nexus.catalog_meta} row with key
 *       {@link TenantImportLoad#MARKER_KEY}, or the empty string when there is none. A tenant restore rewrites
 *       that row, so it changes that tenant's epoch and no other tenant's.</li>
 * </ul>
 * Only those four things move the epoch: a tenant restore, a PITR or promotion, a fresh cluster, a restart. The
 * accepted effect is that any PostgreSQL restart, like a failover, drops every client id cache once, and each
 * collection then re-resolves by label.
 *
 * <p><b>No epoch reads as unstable.</b> {@link #of} returns empty, and the caller sends no epoch, so a client does
 * not trust a cache, when
 * <ul>
 *   <li>the role cannot execute {@code pg_control_system()}, {@code pg_control_checkpoint()} or
 *       {@code pg_postmaster_start_time()}. The privilege is checked with {@code has_function_privilege} in its
 *       own SELECT before any of them is called, because a 42501 raised inside the caller's transaction would abort
 *       it. This class never throws for a missing privilege;</li>
 *   <li>a function answers NULL (logged once, {@code event=catalog_epoch_cluster_null}, apart from the privilege
 *       case); or</li>
 *   <li>{@code ctx} is not scoped to {@code tenant} ({@code current_setting('nexus.tenant', true)}). Row-level
 *       security would hide the tenant's marker, and an epoch computed without it would not change on a restore.</li>
 * </ul>
 *
 * <p>Stateless and static: no cache, no singleton. Every call reads the cluster and the marker; the only memory is
 * the once-only log guard.
 */
final class CatalogEpoch {

    private static final Logger log = LoggerFactory.getLogger(CatalogEpoch.class);
    private static final AtomicBoolean NULL_LOGGED = new AtomicBoolean();
    private static final int HEX_CHARS = 16;

    /** The cluster facts the epoch hashes; {@code postmasterStart} is epoch microseconds in decimal. */
    record ClusterIdentity(String systemIdentifier, String timelineId, String postmasterStart) {}

    private CatalogEpoch() {}

    /**
     * The epoch of {@code tenant} as read from the cluster {@code ctx} is connected to, or empty when there is none to
     * give: see the class note.
     */
    static Optional<String> of(DSLContext ctx, String tenant) {
        var guards = ctx.select(DSL.field(scoped(tenant)), DSL.field(canReadCluster())).fetchOne();
        if (guards == null || !Boolean.TRUE.equals(guards.value1())) return Optional.empty();
        if (!Boolean.TRUE.equals(guards.value2())) return Optional.empty();
        return readCluster(ctx).map(cluster -> ofCluster(ctx, tenant, cluster));
    }

    /**
     * Whether {@link #of} can read the cluster as the role {@code ctx} runs as (the source of {@code /v1/status}'s
     * {@code catalog_epoch_available}). False when a function is not executable or answers NULL; never throws for a
     * missing privilege.
     */
    static boolean available(DSLContext ctx) {
        Boolean can = ctx.select(DSL.field(canReadCluster())).fetchOne(0, Boolean.class);
        return Boolean.TRUE.equals(can) && readCluster(ctx).isPresent();
    }

    /** The test seam: the epoch for a given cluster identity. The marker is still read from {@code ctx}, which must be scoped to {@code tenant}. */
    static String ofCluster(DSLContext ctx, String tenant, ClusterIdentity cluster) {
        String marker = ctx.select(CATALOG_META.VALUE)
            .from(CATALOG_META)
            .where(CATALOG_META.TENANT_ID.eq(tenant))
            .and(CATALOG_META.KEY.eq(TenantImportLoad.MARKER_KEY))
            .fetchOptional(CATALOG_META.VALUE)
            .orElse("");
        return sha256Prefix(cluster.systemIdentifier() + ":" + cluster.timelineId() + ":" + cluster.postmasterStart()
            + ":" + marker);
    }

    /** The cluster facts, read in one statement. Call only after {@link #canReadCluster} answered true. */
    static Optional<ClusterIdentity> readCluster(DSLContext ctx) {
        Field<String> system = member(DSL.function(DSL.name("pg_catalog", "pg_control_system"), SQLDataType.JSONB),
            "system_identifier");
        Field<String> timeline = member(DSL.function(DSL.name("pg_catalog", "pg_control_checkpoint"), SQLDataType.JSONB),
            "timeline_id");
        Field<OffsetDateTime> started = DSL.function(DSL.name("pg_catalog", "pg_postmaster_start_time"),
            SQLDataType.TIMESTAMPWITHTIMEZONE);
        var row = ctx.select(system, timeline, started).fetchOne();
        if (row == null || row.value1() == null || row.value2() == null || row.value3() == null) {
            if (NULL_LOGGED.compareAndSet(false, true)) {
                log.warn("event=catalog_epoch_cluster_null system_identifier_null={} timeline_id_null={} "
                        + "postmaster_start_null={} effect=catalog_epoch_absent",
                    row == null || row.value1() == null, row == null || row.value2() == null,
                    row == null || row.value3() == null);
            }
            return Optional.empty();
        }
        long micros = ChronoUnit.MICROS.between(Instant.EPOCH, row.value3().toInstant());
        return Optional.of(new ClusterIdentity(row.value1(), row.value2(), Long.toString(micros)));
    }

    /** {@code ctx}'s transaction is scoped to {@code tenant}: {@code current_setting('nexus.tenant', true) = tenant}. */
    private static Condition scoped(String tenant) {
        return DSL.function("current_setting", String.class, DSL.val(TenantConstants.GUC_NAME), DSL.inline(true))
            .eq(DSL.val(tenant));
    }

    /** The role can execute all three functions: {@code has_function_privilege(signature, 'EXECUTE')} for the current role. */
    private static Condition canReadCluster() {
        return canExecute(DSL.inline("pg_catalog.pg_control_system()"))
            .and(canExecute(DSL.inline("pg_catalog.pg_control_checkpoint()")))
            .and(canExecute(DSL.inline("pg_catalog.pg_postmaster_start_time()")));
    }

    private static Condition canExecute(Field<String> signature) {
        return DSL.function("has_function_privilege", Boolean.class, signature, DSL.inline("EXECUTE")).isTrue();
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
