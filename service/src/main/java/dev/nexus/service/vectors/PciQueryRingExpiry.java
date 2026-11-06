// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.Collection;
import java.util.Objects;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;

/**
 * The age bound for query rings nobody writes (RDR-230 Technical Design 6, bead nexus-4lnn1.1.3): deletes every ring
 * row older than {@value #MAX_AGE_DAYS} days, one transaction per tenant, under that tenant's scope as nexus_svc.
 * A write's own trim ({@code nexus.canary_query_ring_upsert}) ages the rings it writes; this covers the others.
 * {@link PciReconciler} runs it at the start of each pass on the builder-lock holder, so no row outlives 30 days by
 * more than one pass while a holder runs.
 *
 * <p><b>Connection and statement.</b> The application pool as nexus_svc, a typed jOOQ {@code DELETE} on the generated
 * table: no admin session, no SQL text, nothing added to the admin statement set. The table is FORCE row-level
 * security, so each transaction sees and deletes only its own tenant's rows.
 *
 * <p><b>Tenant source</b> is the caller's supplier. In production it is the reaper's tenants (the default tenant and
 * every tenant with a {@code service_tokens} row) together with {@code request_ledger_tenants}. Residual gap: a tenant
 * that holds ring rows, has no token row (an idle JIT tenant loses its last one 7 days after it expires) and has no
 * ledger row (only the keyed sweep routes write the ledger; a search does not) is not visited. Its rings stay
 * bounded at 32 rows per collection and are trimmed by their next write.
 *
 * <p>A tenant's failure is logged and the next tenant still runs. Counts only are logged.
 */
public final class PciQueryRingExpiry {

    private static final Logger log = LoggerFactory.getLogger(PciQueryRingExpiry.class);

    /** The ring's age bound in days (the function's own trim uses the same). */
    public static final int MAX_AGE_DAYS = 30;
    /** Bound of one tenant's delete; a held row lock must not stall the builder's pass. */
    static final Duration STATEMENT_TIMEOUT = Duration.ofSeconds(10);

    private final TenantScope scope;
    private final Supplier<? extends Collection<String>> tenants;
    private final Clock clock;

    public PciQueryRingExpiry(TenantScope scope, Supplier<? extends Collection<String>> tenants) {
        this(scope, tenants, Clock.systemUTC());
    }

    PciQueryRingExpiry(TenantScope scope, Supplier<? extends Collection<String>> tenants, Clock clock) {
        this.scope = Objects.requireNonNull(scope, "scope");
        this.tenants = Objects.requireNonNull(tenants, "tenants");
        this.clock = Objects.requireNonNull(clock, "clock");
    }

    /** One pass over the tenant source. Returns the rows deleted; never throws. */
    public int expireAll() {
        Collection<String> visit;
        try {
            visit = tenants.get();
        } catch (RuntimeException e) {
            log.warn("event=pci_ring_expire_failed reason=tenant_list error_class={}", e.getClass().getSimpleName());
            return 0;
        }
        OffsetDateTime cutoff = OffsetDateTime.now(clock).minusDays(MAX_AGE_DAYS);
        int deleted = 0;
        int failed = 0;
        for (String tenant : visit) {
            try {
                deleted += scope.withTenant(tenant, ctx -> {
                    PgSession.setLocal(ctx, "statement_timeout", Long.toString(STATEMENT_TIMEOUT.toMillis()));
                    return ctx.deleteFrom(CANARY_QUERY_RING)
                        .where(CANARY_QUERY_RING.TENANT_ID.eq(tenant))
                        .and(CANARY_QUERY_RING.ADDED_AT.lt(cutoff))
                        .execute();
                });
            } catch (RuntimeException e) {
                failed++;
                log.warn("event=pci_ring_expire_failed tenant={} error=\"{}\"", tenant, PciQueryRing.sanitize(e));
            }
        }
        if (deleted > 0 || failed > 0) {
            log.info("event=pci_ring_expire tenants={} deleted={} failed={}", visit.size(), deleted, failed);
        }
        return deleted;
    }
}
