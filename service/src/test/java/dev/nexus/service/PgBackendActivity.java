// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;

/** The other backends of a test database, for a failure message; held apart from {@link PgContainerHelper} to keep it under its size ceiling. */
public final class PgBackendActivity {
    private PgBackendActivity() { }

    /** The other backends of this database as a table (pid, state, wait event, the start of the query), for a failure message. */
    public static String backendActivity(DSLContext ctx) {
        PgLockWait.clearSnapshot(ctx);
        return ctx.select(DSL.field(DSL.name("pid"), Integer.class), DSL.field(DSL.name("state"), String.class),
                DSL.field(DSL.name("wait_event_type"), String.class), DSL.field(DSL.name("wait_event"), String.class),
                DSL.left(DSL.field(DSL.name("query"), String.class), 90).as("query"))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("pid"), Integer.class).ne(DSL.function("pg_backend_pid", Integer.class))
                .and(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", String.class))))
            .fetch().format();
    }
}
