// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import javax.sql.DataSource;
import java.time.Clock;
import java.util.function.Supplier;

/**
 * RDR-229 P3.12 (nexus-p2m1x.32): does the partial UNIQUE index {@value #INDEX} on {@code nexus.catalog_collections}
 * exist and is it VALID? The source of {@code GET /v1/status}'s {@code live_label_index_present}. The index is built by
 * the changeset {@code catalog-053-1}, which is {@code failOnError="false"} behind a CONTINUE precondition: a build that
 * cannot get its lock in time, or that the migration role may not run, is rolled back and logged and the walk goes on, so
 * on such an install the index is absent and nothing else says so. {@code catalog-053-1} is a plain {@code CREATE UNIQUE
 * INDEX} (not {@code CONCURRENTLY}) inside the changeset's transaction, so a failed build leaves no index, never an invalid
 * one. The {@code indisvalid} check is defensive: an index of that name left INVALID (say, one built by hand with
 * {@code CONCURRENTLY} and interrupted) enforces nothing for new rows, so it counts as absent too.
 *
 * <p>Same shape as {@link CollectionTupleIndexProbe}: the route is unauthenticated, so the answer is one boolean with no
 * tenant, collection or label text; it is a catalog lookup ({@code to_regclass} joined to {@code pg_index}, both
 * readable without a privilege on the table), read at most once every {@link #TTL_MILLIS} ms and shared by every poll.
 * A probe that could not run answers {@code null} and is not cached, so the route omits the key and the next poll
 * tries again; a client reads an absent key as "cannot tell", never as false.
 */
public final class LiveLabelIndexProbe {

    private static final Logger log = LoggerFactory.getLogger(LiveLabelIndexProbe.class);

    /** The index this probe looks for (changeset catalog-053-1). */
    public static final String INDEX = "idx_catalog_collections_live_label";
    static final long TTL_MILLIS = 5_000L;

    private LiveLabelIndexProbe() { }

    /** {@code true} when the index exists and is valid, {@code false} when absent or invalid, {@code null} when the probe could not run. */
    public static Boolean present(DataSource ds) {
        try {
            DSLContext ctx = DSL.using(ds, SQLDialect.POSTGRES);
            Field<Object> indexRelId = DSL.field(DSL.name("indexrelid"), Object.class);
            return ctx.fetchExists(ctx.selectOne()
                .from(DSL.table(DSL.name("pg_catalog", "pg_index")))
                .where(indexRelId.eq(DSL.function("to_regclass", Object.class, DSL.val("nexus." + INDEX))))
                .and(DSL.field(DSL.name("indisvalid"), Boolean.class)));
        } catch (RuntimeException e) {
            log.warn("event=live_label_index_probe_unavailable error={}", e.getMessage());
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
