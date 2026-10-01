// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

/**
 * Test support: has a backend started waiting on a lock inside a statement the test is driving on
 * another thread? Read through {@code pg_stat_activity} with typed jOOQ, from a superuser connection
 * so it sees every role's backends. One helper for every race test, so none of them carries its own
 * poller (and none adds a raw-SQL site to the ratchet in {@code RawSqlGateTest}).
 */
public final class PgActivityProbe {

    private PgActivityProbe() { }

    /**
     * Polls until some active backend waits on a lock of a statement whose text matches
     * {@code queryPattern} (a SQL {@code ILIKE} pattern against the rendered statement), for up to
     * about 15 seconds. False when none ever did, which the caller reports as "the race never
     * happened" rather than as a pass.
     */
    public static boolean waitsOnALock(PostgreSQLContainer<?> pg, String queryPattern) throws Exception {
        var activity = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));
        var waitType = DSL.field(DSL.name("wait_event_type"), String.class);
        var state = DSL.field(DSL.name("state"), String.class);
        var query = DSL.field(DSL.name("query"), String.class);
        for (int i = 0; i < 150; i++) {
            try (Connection c = pg.createConnection("")) {
                if (DSL.using(c, SQLDialect.POSTGRES).fetchExists(
                        DSL.selectOne().from(activity)
                           .where(waitType.eq("Lock")).and(state.eq("active")).and(query.likeIgnoreCase(queryPattern)))) {
                    return true;
                }
            }
            Thread.sleep(100);
        }
        return false;
    }
}
