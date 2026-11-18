// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.Collection;
import java.util.TreeSet;

import static org.jooq.impl.DSL.function;
import static org.jooq.impl.DSL.val;

/**
 * The one advisory lock of a board topic family (RDR-252), keyed on (tenant, base topic name): the base name is the
 * topic before {@code --}, so {@code nexus-develop} covers {@code nexus-develop--done}, {@code --push} and
 * {@code --owner}. Every writer that must be alone on a topic takes it through here: the group writes that can make
 * a group serve a topic (form, re-form, {@code set --serves}, {@code set --ends}) and, in Step 3, the owner stamper
 * and its deadline timer. A second key would let two of them hold "the" lock of one topic at once.
 *
 * <p>It is a transaction-scoped lock ({@code pg_advisory_xact_lock}), released at commit or rollback. The two-key
 * form keeps it apart from the single-key locks other repositories take on {@code hashtext(...)}, which are a
 * separate lock space in PostgreSQL. <b>Lock order</b>: the group record row first, then the topic locks in sorted
 * order ({@link #lockAll}), then any identity row. A caller that takes them in another order can deadlock with one
 * that does not.
 */
public final class BoardTopicLock {

    /** The first key: names this lock space (the bytes of "BTPL"). */
    static final int NAMESPACE = 0x4254504C;

    private BoardTopicLock() {
    }

    /** Takes the lock of one topic family; waits for its holder. */
    public static void lock(DSLContext ctx, String tenant, String baseTopic) {
        // Typed-DSL function composition (house rule: no raw string-SQL, RawSqlGateTest):
        // SELECT pg_advisory_xact_lock(NAMESPACE, hashtext(:key)).
        ctx.select(function("pg_advisory_xact_lock", Object.class, val(NAMESPACE),
                function("hashtext", Integer.class, val(tenant + "/" + baseTopic))))
            .fetch();
    }

    /** Takes the locks of these topic families in sorted order, each once. */
    public static void lockAll(DSLContext ctx, String tenant, Collection<String> baseTopics) {
        for (String topic : new TreeSet<>(baseTopics)) {
            lock(ctx, tenant, topic);
        }
    }
}
