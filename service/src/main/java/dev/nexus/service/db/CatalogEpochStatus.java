// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Clock;
import java.util.function.Supplier;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): can this engine's role read what {@link CatalogEpoch} hashes? The source of
 * {@code GET /v1/status}'s {@code catalog_epoch_available}. {@code false} means every route that would carry a
 * {@code catalog_epoch} sends none, so a client keys its id cache by label.
 *
 * <p>Built at boot: the supplier asks once as it is built. After that it answers from memory and asks again at most
 * every {@value #TTL_MILLIS} ms, so a grant or revoke shows without a restart. The answer is cached whatever it is,
 * a probe that could not run ({@code null}) included: the route is unauthenticated, and a database that is down must
 * not be asked again on every poll. {@code null} omits the key, and a client reads that as "cannot tell".
 *
 * <p>One log line per change of state, never one per call: {@code event=catalog_epoch_unavailable} (WARN) on entering
 * "no", with {@code cause=pg_control_not_readable} (a missing privilege) or {@code cause=pg_control_member_null}, and
 * {@code event=catalog_epoch_probe_unavailable} (WARN) on entering "cannot tell". One boolean on the wire: the route is
 * unauthenticated.
 */
public final class CatalogEpochStatus {

    private static final Logger log = LoggerFactory.getLogger(CatalogEpochStatus.class);
    static final long TTL_MILLIS = 5_000L;

    private CatalogEpochStatus() { }

    /** The status supplier over the service role's pool. Asks once now. */
    public static Supplier<Boolean> statusSupplier(DataSource ds) {
        return statusSupplier(ds, Clock.systemUTC());
    }

    static Supplier<Boolean> statusSupplier(DataSource ds, Clock clock) {
        Supplier<Boolean> supplier = new Supplier<>() {
            private long answeredAt = Long.MIN_VALUE;
            private Boolean answer;
            private boolean asked;
            private String state;   // "available", "unavailable:<cause>", "unknown"

            @Override
            public synchronized Boolean get() {
                long now = clock.millis();
                if (asked && now - answeredAt < TTL_MILLIS) {
                    return answer;
                }
                Probe probed = probe(ds);
                CatalogEpoch.Availability fresh = probed.availability();
                answer = fresh == null ? null : fresh == CatalogEpoch.Availability.AVAILABLE;
                answeredAt = now;
                asked = true;
                transition(probed.error(), fresh == null ? "unknown" : fresh == CatalogEpoch.Availability.AVAILABLE ? "available"
                    : "unavailable:" + (fresh == CatalogEpoch.Availability.NOT_READABLE
                        ? "pg_control_not_readable" : "pg_control_member_null"));
                return answer;
            }

            private void transition(String error, String next) {
                if (next.equals(state)) return;
                state = next;
                if (next.equals("unknown")) {
                    log.warn("event=catalog_epoch_probe_unavailable effect=catalog_epoch_available_absent error={}", error);
                } else if (next.startsWith("unavailable:")) {
                    log.warn("event=catalog_epoch_unavailable cause={} "
                            + "functions=pg_control_system(),pg_control_checkpoint(),pg_postmaster_start_time() "
                            + "effect=catalog_epoch_absent client_keys_by=label",
                        next.substring("unavailable:".length()));
                }
            }
        };
        supplier.get();
        return supplier;
    }

    /** The availability, or {@code null} with the error when the probe could not run. */
    record Probe(CatalogEpoch.Availability availability, String error) {}

    /** One connection per probe. */
    static Probe probe(DataSource ds) {
        try (Connection c = ds.getConnection()) {
            return new Probe(CatalogEpoch.availability(DSL.using(c, SQLDialect.POSTGRES)), null);
        } catch (SQLException | RuntimeException e) {
            return new Probe(null, e.getMessage());
        }
    }
}
