// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.OptionalInt;

/**
 * nexus-f3yqd.2.15: what the failure classes write to {@code nexus.data_moves}, on the admin connection only
 * ({@code nexus_svc} holds no grant). Each write is guarded to a move that is still open, so a move completed or failed by
 * another path meanwhile is not touched.
 */
final class MoveFailureRecords {

    private MoveFailureRecords() { /* static */ }

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> LABEL = DSL.field(DSL.name("label"), String.class);
    private static final Field<OffsetDateTime> FAILED_AT =
        DSL.field(DSL.name("failed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<String> FAILURE = DSL.field(DSL.name("failure"), String.class);
    private static final Field<OffsetDateTime> COMPLETED_AT =
        DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<Integer> RETRIES = DSL.field(DSL.name("transient_retries"), Integer.class);
    private static final Field<OffsetDateTime> LAST_AT =
        DSL.field(DSL.name("last_transient_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<String> LAST = DSL.field(DSL.name("last_transient"), String.class);

    private static Condition open(String label) {
        return LABEL.eq(label).and(FAILED_AT.isNull()).and(COMPLETED_AT.isNull());
    }

    private static OffsetDateTime at(Instant instant) {
        return instant.atOffset(ZoneOffset.UTC);
    }

    /**
     * A permanent failure: {@code failed_at} and {@code failure}, which a later pass, or boot, reads as "do not retry".
     *
     * @return whether the move was open and is now marked
     */
    static boolean markFailed(DSLContext admin, String label, Instant now, String reason) {
        return admin.update(MOVES).set(FAILED_AT, at(now)).set(FAILURE, reason).where(open(label)).execute() == 1;
    }

    /**
     * A transient failure: one more consecutive retry, stamped and with its error.
     *
     * @return the new count, or empty when the move is no longer open
     */
    static OptionalInt recordTransient(DSLContext admin, String label, Instant now, String error) {
        var row = admin.update(MOVES).set(RETRIES, RETRIES.plus(1)).set(LAST_AT, at(now)).set(LAST, error)
            .where(open(label)).returning(RETRIES).fetchOne();
        return row == null ? OptionalInt.empty() : OptionalInt.of(row.get(RETRIES));
    }

    /**
     * A pass that ended without a transient error: the count, the time and the error back to 0 and NULL. Not guarded to an
     * open move: a pass whose finisher completed the move clears it too.
     */
    static void clearTransient(DSLContext admin, String label) {
        admin.update(MOVES).set(RETRIES, 0).set(LAST_AT, (OffsetDateTime) null).set(LAST, (String) null)
            .where(LABEL.eq(label)).and(RETRIES.gt(0)).execute();
    }
}
