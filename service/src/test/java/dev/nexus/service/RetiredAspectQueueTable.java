// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.sql.Connection;
import java.time.OffsetDateTime;

/**
 * Test fixture (nexus-hbnml): the retired {@code nexus.aspect_extraction_queue} table as an install that KEPT it
 * has it (aspects-005-2 leaves a table that holds rows). A test class that runs on a fully migrated database, where
 * the table is already dropped, calls {@link #restore} to bring back the table exactly as an install that kept it
 * has it, seeds rows with {@link #seed}, runs the operation under test, and calls {@link #drop}.
 *
 * <p>{@code restore} does what the changelog did on such an install, from the changelog's own text so it cannot
 * drift: it replays the rollback of {@code aspects-005-2} (which creates the table), puts the collection foreign key
 * back to the {@code ON DELETE RESTRICT} that fk-003-2 made (typed jOOQ DDL), then replays the forward SQL of
 * {@code aspects-005-1}, which swaps that foreign key to CASCADE. A test that passes on this table therefore passes
 * because of aspects-005-1's own foreign key, and a changeset that leaves the foreign key RESTRICT fails it. The
 * statements run on the connection behind the given context (a superuser, which also bypasses the table's
 * row-level security for the seeds).
 */
public final class RetiredAspectQueueTable {

    private static final String CHANGELOG = "db/changelog/aspects-005-retire-aspect-queue-table.xml";
    private static final String FK = "aspect_extraction_queue_collection_fk";
    private static final Table<?> QUEUE = DSL.table(DSL.name("nexus", "aspect_extraction_queue"));

    private RetiredAspectQueueTable() { }

    /** Creates the table as a store that kept it has it: the pre-aspects-005-1 shape, then aspects-005-1's own SQL. */
    public static void restore(DSLContext ctx) throws Exception {
        Connection conn = ctx.configuration().connectionProvider().acquire();
        try {
            PgContainerHelper.replayChangesetRollback(conn, CHANGELOG, "aspects-005-2");
            ctx.alterTable(QUEUE).dropConstraint(DSL.name(FK)).execute();
            ctx.alterTable(QUEUE).add(DSL.constraint(DSL.name(FK))
                .foreignKey(DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("collection")))
                .references(DSL.table(DSL.name("nexus", "catalog_collections")),
                    DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("name")))
                .onDeleteRestrict()).execute();
            String before = foreignKeyDefinition(ctx);
            if (!before.contains("ON DELETE RESTRICT")) {
                throw new IllegalStateException("non-vacuity: the pre-aspects-005-1 foreign key is not RESTRICT: " + before);
            }
            PgContainerHelper.replayChangeset(conn, CHANGELOG, "aspects-005-1");
        } finally {
            ctx.configuration().connectionProvider().release(conn);
        }
        String after = foreignKeyDefinition(ctx);
        if (!after.contains("ON DELETE CASCADE") || !after.contains("ON UPDATE CASCADE")) {
            throw new IllegalStateException("aspects-005-1 did not leave the collection foreign key CASCADE: " + after);
        }
    }

    /** One row; the collection and document must already exist (the foreign keys). */
    public static void seed(DSLContext ctx, String tenant, String collection, String docId, String sourcePath,
                            String status) {
        ctx.insertInto(QUEUE,
                DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("collection")),
                DSL.field(DSL.name("source_path")), DSL.field(DSL.name("doc_id")),
                DSL.field(DSL.name("status")), DSL.field(DSL.name("enqueued_at")))
            .values(tenant, collection, sourcePath, docId, status, OffsetDateTime.now())
            .execute();
    }

    /** Rows naming {@code collection} (read as a superuser). */
    public static int rows(DSLContext ctx, String tenant, String collection) {
        return ctx.fetchCount(QUEUE, DSL.field(DSL.name("tenant_id")).eq(tenant)
            .and(DSL.field(DSL.name("collection")).eq(collection)));
    }

    /** Drops the table (the counting helper the rollback replay brought back stays; it is inert without the table). */
    public static void drop(DSLContext ctx) {
        ctx.dropTableIfExists(QUEUE).execute();
    }

    private static String foreignKeyDefinition(DSLContext ctx) {
        return PgCatalogProbes.constraintDefinitionsIn(ctx, java.util.List.of("nexus")).stream()
            .filter(c -> c.conname().equals(FK))
            .map(PgCatalogProbes.ConstraintDefinition::definition)
            .findFirst().orElseThrow(() -> new IllegalStateException(FK + " is missing"));
    }
}
