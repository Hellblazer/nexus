// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.LADDER_COMPLETIONS;

/**
 * RDR-186 bead nexus-146xx.12 (engine half) — jOOQ ladder_completions repository.
 *
 * <p>The PG home for upgrade-ladder rung completion bookkeeping
 * ({@code nexus.ladder_completions}, ladder-001-baseline.xml). The client's
 * {@code HttpLadderStore} — the {@code CompletionLedger} implementation
 * replacing the retired {@code ladder.db} — writes verified facts here and
 * reads them back for {@code verified_rungs()} / {@code completions()}.
 *
 * <p>Contract (RDR-186 D3 / RF-186-2):
 * <ul>
 *   <li>{@link #record} is an UPSERT on {@code (tenant_id, rung_name)} —
 *       overwrite-on-reverify, the SQLite {@code ON CONFLICT(rung_name) DO
 *       UPDATE} parity; audit metadata is observability-only and lossy-OK.</li>
 *   <li>{@code verified_at} is stamped HERE (server {@code now()}) — the
 *       flush may lag the client's own clock arbitrarily.</li>
 *   <li>NO position surface: completion facts only. Ladder position is
 *       DERIVED client-side via {@code derive_ladder_position} (the single
 *       Gap-4 mechanism-1 algorithm); this repository never orders rungs,
 *       never stores or computes a position.</li>
 * </ul>
 */
public final class LadderRepository {

    private static final Logger log = LoggerFactory.getLogger(LadderRepository.class);

    private final TenantScope tenantScope;

    public LadderRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /** Record one rung's verified completion (upsert; server-stamped verified_at). */
    public void record(String tenant, String rungName, String packageVersion, String detail) {
        requireNonBlank(rungName, "rung_name");
        requireNonBlank(packageVersion, "package_version");
        String safeDetail = detail == null ? "" : detail;
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(LADDER_COMPLETIONS,
                    LADDER_COMPLETIONS.TENANT_ID, LADDER_COMPLETIONS.RUNG_NAME,
                    LADDER_COMPLETIONS.VERIFIED_AT, LADDER_COMPLETIONS.PACKAGE_VERSION,
                    LADDER_COMPLETIONS.DETAIL)
               .values(tenant, rungName, now, packageVersion, safeDetail)
               .onConflict(LADDER_COMPLETIONS.TENANT_ID, LADDER_COMPLETIONS.RUNG_NAME)
               .doUpdate()
               .set(LADDER_COMPLETIONS.VERIFIED_AT, DSL.field("EXCLUDED.verified_at", OffsetDateTime.class))
               .set(LADDER_COMPLETIONS.PACKAGE_VERSION, DSL.field("EXCLUDED.package_version", String.class))
               .set(LADDER_COMPLETIONS.DETAIL, DSL.field("EXCLUDED.detail", String.class))
               .execute();
            return null;
        });
        log.info("event=ladder_completion_recorded tenant={} rung={} version={}",
                tenant, rungName, packageVersion);
    }

    /** Every completion fact for the tenant, ordered by rung name. */
    public List<Map<String, String>> completions(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(LADDER_COMPLETIONS.RUNG_NAME, LADDER_COMPLETIONS.VERIFIED_AT,
                       LADDER_COMPLETIONS.PACKAGE_VERSION, LADDER_COMPLETIONS.DETAIL)
               .from(LADDER_COMPLETIONS)
               .where(LADDER_COMPLETIONS.TENANT_ID.eq(tenant))
               .orderBy(LADDER_COMPLETIONS.RUNG_NAME)
               .fetch(r -> Map.of(
                       "rung_name", r.value1(),
                       "verified_at", r.value2().toString(),
                       "package_version", r.value3(),
                       "detail", r.value4())));
    }

    private static void requireNonBlank(String value, String field) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("'" + field + "' is required");
        }
    }
}
