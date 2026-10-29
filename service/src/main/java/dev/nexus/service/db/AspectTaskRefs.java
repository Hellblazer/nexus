// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;

/**
 * RDR-235 (bead nexus-5r2sc.2.6): the engine's collection-keyed verbs, for the unconsumed tasks of the
 * {@code aspects} tuple queue.
 *
 * <p>A task names its collection in its body (a JSON object with {@code collection}, {@code source_path},
 * {@code doc_id} and {@code content_hash}), not in a column or in the queue name. So a collection rename, a
 * re-embed move, a re-home or a purge has to rewrite or delete the tasks itself, in the same transaction as
 * the table rows it handles, or the tasks keep naming a collection that no longer exists. The tasks cannot be
 * a {@code CatalogRepository.COLLECTION_SCOPED_TABLES} entry ({@code nexus.tuples} has no collection column),
 * so this helper stands beside the registry with its own disposition, {@link CatalogRepository#ASPECT_TASKS_DISPOSITION},
 * and its own parity test ({@code AspectTaskRefsParityTest}) which fails when a collection-keyed site has no call.
 *
 * <p><b>Which tasks.</b> The tenant's {@code aspects} rows with {@code consumed_at IS NULL} and
 * {@code expires_at > now()}: available, claimed and dead alike. A claimed task keeps its claim, its lease and
 * its attempt count; only the body's {@code collection} changes. The task id is a hash of the keys and the
 * nonce (the {@code doc_id}), not of the body, so a rewrite never changes it.
 *
 * <p><b>Every statement runs in the caller's {@link DSLContext}</b>, so in the caller's transaction, and this
 * class opens none. The tenant is stated in every predicate beside the row-level security the caller's
 * context already carries.
 *
 * <p><b>Matching.</b> The body is TEXT. The collection is read through {@code CASE WHEN pg_input_is_valid(body,
 * 'jsonb') THEN (body::jsonb)->>'collection' END}, so a body that is not valid jsonb (not JSON, or JSON that
 * jsonb refuses such as the NUL escape, backslash-u-0000) matches nothing and is never cast (a bare
 * {@code ::jsonb} in a WHERE would abort the statement, and PostgreSQL does not promise to evaluate one
 * predicate before another). Such rows are counted in a {@code aspect_task_refs_non_json_skipped} log event.
 * No index serves the match: each statement scans the tenant's unconsumed {@code aspects} rows, which the
 * template's {@code max_live_rows} bounds.
 *
 * <p><b>Lock order (this bead and E5): the tuple row lock FIRST, then {@code document_aspects}, in every
 * path.</b> A claim-fenced aspect write locks its task's row and then writes {@code document_aspects}. A
 * rename, move, re-home or purge therefore calls {@link #rewriteCollection}, {@link #rewriteCollectionForDoc}
 * or {@link #deleteNaming} BEFORE its own statements touch {@code document_aspects}. These methods do not rely
 * on the UPDATE's own row locks: they first take every matching row with {@code SELECT ... ORDER BY id FOR NO
 * KEY UPDATE} (ordered by id, so two renames sharing tasks take them in one order and cannot deadlock each
 * other), then write by id. A row a worker is writing under blocks the select until that transaction ends,
 * and the select then re-checks it, so a task acked meanwhile is not rewritten. The count methods take no
 * locks.
 *
 * <p><b>What it does not cover.</b> A task queued after the select, in the same READ COMMITTED window every
 * registry statement has (see {@code collectionIsEmpty}); and a rewrite that makes a body longer than the
 * 4096-byte cap, which the table's CHECK constraint refuses.
 */
public final class AspectTaskRefs {

    private static final Logger log = LoggerFactory.getLogger(AspectTaskRefs.class);

    /** The tuple subspace, which is also the template name. */
    public static final String SUBSPACE = "aspects";

    /** Ids per write statement, well under the JDBC bind-parameter ceiling. */
    private static final int ID_BATCH = 5000;

    private static final Field<JSONB> BODY_AS_JSONB = TUPLES.BODY.cast(JSONB.class);

    private AspectTaskRefs() { }

    /** A JSON attribute of the body as text; NULL when the body is not JSON or lacks the attribute. */
    private static Field<String> bodyAttribute(String attribute) {
        return DSL.when(bodyIsJsonb(), DSL.jsonbGetAttributeAsText(BODY_AS_JSONB, attribute));
    }

    /**
     * True when the body casts to {@code jsonb} without error: {@code pg_input_is_valid(body, 'jsonb')}
     * (PostgreSQL 16+; the engine bundles 17). Not {@code body IS JSON}: that accepts JSON text jsonb then
     * refuses (the NUL escape, backslash-u-0000, for one), and the cast would abort the statement. NULL for a NULL body.
     */
    private static Condition bodyIsJsonb() {
        return DSL.condition(DSL.function("pg_input_is_valid", SQLDataType.BOOLEAN, TUPLES.BODY, DSL.inline("jsonb")));
    }

    /** A body that is present and will not cast to {@code jsonb}: skipped by every match and counted in the log. */
    private static Condition bodyIsNotJsonb() {
        return TUPLES.BODY.isNotNull().and(bodyIsJsonb().not());
    }

    private static Condition unconsumed(String tenant) {
        return TUPLES.TENANT_ID.eq(tenant)
            .and(TUPLES.SUBSPACE.eq(SUBSPACE))
            .and(TUPLES.CONSUMED_AT.isNull())
            .and(TUPLES.EXPIRES_AT.gt(DSL.currentOffsetDateTime()));
    }

    /** How many unconsumed tasks name {@code collection} (available, claimed and dead). Takes no lock. */
    public static int countNaming(DSLContext ctx, String tenant, String collection) {
        return count(ctx, tenant, bodyAttribute("collection").eq(collection));
    }

    /**
     * {@link #countNaming} restricted by a condition on the task's {@code doc_id}: the re-embed plan's
     * "naming the source, and not yet moved" count passes the same document predicate it applies to the other
     * satellites.
     */
    public static int countNamingWhereDoc(DSLContext ctx, String tenant, String collection,
                                          Function<Field<String>, Condition> docIdCondition) {
        return count(ctx, tenant, bodyAttribute("collection").eq(collection).and(docIdCondition.apply(bodyAttribute("doc_id"))));
    }

    private static int count(DSLContext ctx, String tenant, Condition match) {
        var row = ctx.select(
                DSL.count().filterWhere(match),
                DSL.count().filterWhere(bodyIsNotJsonb()))
            .from(TUPLES)
            .where(unconsumed(tenant))
            .fetchOne();
        logNonJson(tenant, "count", row.value2());
        return row.value1();
    }

    /**
     * Rewrite {@code collection} in the body of every unconsumed task naming {@code from} so that it names
     * {@code to}. Returns the number of tasks rewritten. Locks them first (see the class comment).
     */
    public static int rewriteCollection(DSLContext ctx, String tenant, String from, String to) {
        return rewrite(ctx, tenant, from, to, DSL.trueCondition());
    }

    /** {@link #rewriteCollection} for the tasks of one document ({@code doc_id}); the re-embed move's per-document site. */
    public static int rewriteCollectionForDoc(DSLContext ctx, String tenant, String from, String to, String docId) {
        return rewrite(ctx, tenant, from, to, bodyAttribute("doc_id").eq(docId));
    }

    private static int rewrite(DSLContext ctx, String tenant, String from, String to, Condition extra) {
        if (from.equals(to)) {
            return 0;
        }
        List<byte[]> ids = lockNaming(ctx, tenant, from, extra);
        Field<String[]> path = DSL.cast(DSL.val(new String[] {"collection"}), SQLDataType.CLOB.array());
        Field<JSONB> newValue = DSL.function("to_jsonb", SQLDataType.JSONB, DSL.cast(DSL.val(to), SQLDataType.CLOB));
        Field<String> newBody = DSL.function("jsonb_set", SQLDataType.JSONB, BODY_AS_JSONB, path, newValue)
            .cast(String.class);
        int rewritten = 0;
        for (int i = 0; i < ids.size(); i += ID_BATCH) {
            rewritten += ctx.update(TUPLES)
                .set(TUPLES.BODY, newBody)
                .where(TUPLES.TENANT_ID.eq(tenant))
                .and(TUPLES.ID.in(ids.subList(i, Math.min(ids.size(), i + ID_BATCH))))
                .execute();
        }
        return rewritten;
    }

    /**
     * Delete every unconsumed task naming {@code collection}. Returns the number deleted. The purge calls
     * this inside its own transaction; a task's claim-log rows keep their audit trail (their tuple reference
     * is set to NULL by the foreign key).
     */
    public static int deleteNaming(DSLContext ctx, String tenant, String collection) {
        List<byte[]> ids = lockNaming(ctx, tenant, collection, DSL.trueCondition());
        int deleted = 0;
        for (int i = 0; i < ids.size(); i += ID_BATCH) {
            deleted += ctx.deleteFrom(TUPLES)
                .where(TUPLES.TENANT_ID.eq(tenant))
                .and(TUPLES.ID.in(ids.subList(i, Math.min(ids.size(), i + ID_BATCH))))
                .execute();
        }
        return deleted;
    }

    /** The matching tasks' ids, locked FOR NO KEY UPDATE in id order. */
    private static List<byte[]> lockNaming(DSLContext ctx, String tenant, String collection, Condition extra) {
        int skipped = ctx.fetchCount(TUPLES, unconsumed(tenant).and(bodyIsNotJsonb()));
        logNonJson(tenant, "lock", skipped);
        return ctx.select(TUPLES.ID)
            .from(TUPLES)
            .where(unconsumed(tenant))
            .and(bodyAttribute("collection").eq(collection))
            .and(extra)
            .orderBy(TUPLES.ID)
            .forNoKeyUpdate()
            .fetch(TUPLES.ID);
    }

    private static void logNonJson(String tenant, String operation, int skipped) {
        if (skipped > 0) {
            log.warn("event=aspect_task_refs_non_json_skipped tenant={} operation={} skipped={}", tenant, operation, skipped);
        }
    }
}
