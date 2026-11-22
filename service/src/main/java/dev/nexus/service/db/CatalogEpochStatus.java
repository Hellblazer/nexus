// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.time.Clock;
import java.util.function.Supplier;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): can this engine's role read what {@link CatalogEpoch} hashes? The source of
 * {@code GET /v1/status}'s {@code catalog_epoch_available}. {@code false} means every route that would carry a
 * {@code catalog_epoch} sends none, so a client keys its id cache by label.
 *
 * <p>Built at boot: the supplier asks once as it is built, and logs one WARN when the answer is no. After that it
 * answers from memory and asks again at most every {@value #TTL_MILLIS} ms, as
 * {@link CollectionTupleIndexProbe#statusSupplier} does, so a grant or revoke shows without a restart. A probe that
 * could not run answers {@code null} and is not cached; the route omits the key and a client reads that as "cannot
 * tell". One boolean: the route is unauthenticated.
 */
public final class CatalogEpochStatus {

    private static final Logger log = LoggerFactory.getLogger(CatalogEpochStatus.class);
    static final long TTL_MILLIS = 5_000L;

    private CatalogEpochStatus() { }

    /** The status supplier over the service role's pool. Asks once now, and warns once if the answer is no. */
    public static Supplier<Boolean> statusSupplier(DataSource ds) {
        return statusSupplier(ds, Clock.systemUTC());
    }

    static Supplier<Boolean> statusSupplier(DataSource ds, Clock clock) {
        Supplier<Boolean> supplier = new Supplier<>() {
            private long answeredAt = Long.MIN_VALUE;
            private Boolean answer;

            @Override
            public synchronized Boolean get() {
                long now = clock.millis();
                if (answer != null && now - answeredAt < TTL_MILLIS) {
                    return answer;
                }
                Boolean fresh = probe(ds);
                if (fresh != null) {
                    answer = fresh;
                    answeredAt = now;
                }
                return fresh;
            }
        };
        if (Boolean.FALSE.equals(supplier.get())) {
            log.warn("event=catalog_epoch_unavailable cause=pg_control_not_readable "
                + "functions=pg_control_system(),pg_control_checkpoint(),pg_postmaster_start_time() "
                + "effect=catalog_epoch_absent client_keys_by=label");
        }
        return supplier;
    }

    /** {@code true}, {@code false}, or {@code null} when the probe could not run. */
    static Boolean probe(DataSource ds) {
        try {
            DSLContext ctx = DSL.using(ds, SQLDialect.POSTGRES);
            return CatalogEpoch.available(ctx);
        } catch (RuntimeException e) {
            log.warn("event=catalog_epoch_probe_unavailable error={}", e.getMessage());
            return null;
        }
    }
}
