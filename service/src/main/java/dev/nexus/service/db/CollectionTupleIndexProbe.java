// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import javax.sql.DataSource;
import java.time.Clock;
import java.util.function.Supplier;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28): does the partial UNIQUE index {@value #INDEX} on {@code nexus.catalog_collections}
 * exist? The source of {@code GET /v1/status}'s {@code collection_tuple_index_present}. The index is built by the
 * changeset {@code collections-001-2}, which waits (a CONTINUE precondition) for the re-own data move and for no
 * duplicate live tuple, so on an engine that booted before then the answer is {@code false} until a later boot.
 *
 * <p>The route is unauthenticated, so the answer is one boolean: no tenant, collection or tuple text. It is a catalog
 * lookup ({@code to_regclass}, which needs no privilege on the table), read at most once every
 * {@link #TTL_MILLIS} ms and shared by every poll. A probe that could not run answers {@code null} and is not cached, so
 * the route omits the key and the next poll tries again; a client reads an absent key as "cannot tell", never as false.
 */
public final class CollectionTupleIndexProbe {

    private static final Logger log = LoggerFactory.getLogger(CollectionTupleIndexProbe.class);

    /** The index this probe looks for (changeset collections-001-2). */
    public static final String INDEX = "catalog_collections_live_tuple_uq";
    static final long TTL_MILLIS = 5_000L;

    private CollectionTupleIndexProbe() { }

    /** {@code true} when the index exists, {@code false} when it does not, {@code null} when the probe could not run. */
    public static Boolean present(DataSource ds) {
        try {
            DSLContext ctx = DSL.using(ds, SQLDialect.POSTGRES);
            return ctx.fetchExists(ctx.selectOne().where(
                DSL.function("to_regclass", Object.class, DSL.val("nexus." + INDEX)).isNotNull()));
        } catch (RuntimeException e) {
            log.warn("event=collection_tuple_index_probe_unavailable error={}", e.getMessage());
            return null;
        }
    }

    /** The status supplier: {@link #present} behind a {@value #TTL_MILLIS} ms cache of its answers (never of a null). */
    public static Supplier<Boolean> statusSupplier(DataSource ds) {
        return statusSupplier(ds, Clock.systemUTC());
    }

    static Supplier<Boolean> statusSupplier(DataSource ds, Clock clock) {
        return new Supplier<>() {
            private long answeredAt = Long.MIN_VALUE;
            private Boolean answer;

            @Override
            public synchronized Boolean get() {
                long now = clock.millis();
                if (answer != null && now - answeredAt < TTL_MILLIS) {
                    return answer;
                }
                Boolean fresh = present(ds);
                if (fresh != null) {
                    answer = fresh;
                    answeredAt = now;
                }
                return fresh;
            }
        };
    }
}
