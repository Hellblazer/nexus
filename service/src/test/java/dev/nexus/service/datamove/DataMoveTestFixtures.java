// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.Map;

/**
 * nexus-f3yqd.2.13: the states DataMoveWorkerIntegrationTest starts from. Each is a test changelog run through
 * {@link PgContainerHelper#runSuperuserTestChangelog} (nexus-zrcj7: no SQL in Java, tests included); the move templates
 * are resources, and a move is registered with a typed INSERT.
 */
final class DataMoveTestFixtures {

    private DataMoveTestFixtures() { /* static */ }

    private static void run(Connection su, String name) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-datamove-" + name + ".xml",
            "databasechangelog_test_datamove_" + name.replace('-', '_'), Map.of());
    }

    /** dm_test rebuilt from nothing: plain_rows, tenant_rows and leafed, all owned by the migration role. */
    static void rebuild(Connection su) throws Exception {
        run(su, "fixtures");
    }

    static void vacuumPlainRows(Connection su) throws Exception {
        run(su, "vacuum");
    }

    /** Drops the leaf of tenant globex under model m2. */
    static void dropGlobexLeaf(Connection su) throws Exception {
        run(su, "drop-leaf");
    }

    /** The library schemas and the three library roles gone, as before a bootstrap. */
    static void dropLibraryRoles(Connection su) throws Exception {
        run(su, "drop-library-roles");
    }

    static void holdWorkerSlots(Connection su) throws Exception {
        run(su, "hold-slots");
    }

    static void releaseWorkerSlots(Connection su) throws Exception {
        run(su, "release-slots");
    }

    /** A test resource under {@code datamove/}. */
    static String resource(String name) {
        try (InputStream in = DataMoveTestFixtures.class.getResourceAsStream("/datamove/" + name)) {
            if (in == null) {
                throw new IllegalStateException("no test resource datamove/" + name);
            }
            return new String(in.readAllBytes(), StandardCharsets.UTF_8).strip();
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Registers a move over {@code dm_test.<table>} with the stamping template, as a changeset's one INSERT does. */
    static void register(DSLContext ctx, String label, String table, int chunkSize, int sleepMs) {
        ctx.insertInto(DSL.table(DSL.name("nexus", "data_moves")))
            .columns(DSL.field(DSL.name("label"), String.class), DSL.field(DSL.name("template"), String.class),
                DSL.field(DSL.name("driving_schema"), String.class), DSL.field(DSL.name("driving_table"), String.class),
                DSL.field(DSL.name("chunk_size"), Integer.class), DSL.field(DSL.name("sleep_ms"), Integer.class),
                DSL.field(DSL.name("statement_timeout_ms"), Integer.class),
                DSL.field(DSL.name("verification"), String.class), DSL.field(DSL.name("changeset_id"), String.class))
            .values(label, resource("stamp-template.sql"), "dm_test", table, chunkSize, sleepMs, 60_000,
                resource("verification.sql"), "test-" + label)
            .execute();
    }
}
