// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * Reads {@code nexus.data_moves}, on the admin connection only: {@code nexus_svc} holds no grant on it, so nothing on
 * the serving pool may call this. The table is addressed by name (the generated schema does not carry what a later
 * changeset adds, and the move worker must not depend on which release generated it).
 */
final class DataMoveRegistry {

    private DataMoveRegistry() { /* static */ }

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));

    private static Field<String> text(String alias, String column) {
        return DSL.field(DSL.name(alias, column), String.class);
    }

    private static Field<Integer> integer(String alias, String column) {
        return DSL.field(DSL.name(alias, column), Integer.class);
    }

    private static Field<OffsetDateTime> stamp(String alias, String column) {
        return DSL.field(DSL.name(alias, column), SQLDataType.TIMESTAMPWITHTIMEZONE);
    }

    /**
     * The open moves, oldest first: {@code completed_at} and {@code failed_at} NULL and no row's {@code supersedes}
     * naming it, ordered by {@code registered_at}, then {@code label}. A paused move is open and is returned; the
     * caller decides not to work it.
     */
    static List<DataMove> openMoves(DSLContext admin) {
        var m = MOVES.as("m");
        var s = MOVES.as("s");
        return admin.select(
                text("m", "label"), text("m", "template"), text("m", "driving_schema"), text("m", "driving_table"),
                integer("m", "chunk_size"), integer("m", "sleep_ms"), integer("m", "statement_timeout_ms"),
                text("m", "verification"), text("m", "on_complete"), stamp("m", "registered_at"),
                stamp("m", "paused_at"), text("m", "paused_by"), integer("m", "transient_retries"),
                stamp("m", "last_transient_at"), text("m", "last_transient"))
            .from(m)
            .where(stamp("m", "completed_at").isNull())
            .and(stamp("m", "failed_at").isNull())
            .and(DSL.notExists(DSL.selectOne().from(s).where(text("s", "supersedes").eq(text("m", "label")))))
            .orderBy(stamp("m", "registered_at"), text("m", "label"))
            .fetch(r -> new DataMove(r.value1(), r.value2(), r.value3(), r.value4(), r.value5(), r.value6(),
                r.value7(), r.value8(), r.value9(), r.value10(), r.value11(), r.value12(), r.value13(), r.value14(),
                r.value15()));
    }
}
