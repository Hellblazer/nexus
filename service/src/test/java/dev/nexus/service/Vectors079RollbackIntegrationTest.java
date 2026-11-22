// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), vectors-079-1 on a migrated schema: it creates the three triggers and three functions, its rollback
 * removes every one of them, and replaying the changeset brings them back. Its own database (a template clone).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Vectors079RollbackIntegrationTest {

    private static final String FILE = "db/changelog/vectors-079-child-row-name-from-chunk.xml";
    private static final String ID = "vectors-079-1";
    private static final List<String> TRIGGERS = List.of(
        "trg_catalog_document_chunks_name_from_chunk", "trg_chunk_orphaned_at_name_from_chunk", "trg_topic_assignments_name_from_chunk");
    private static final List<String> FUNCTIONS = List.of(
        "catalog_document_chunks_name_from_chunk", "chunk_orphaned_at_name_from_chunk", "topic_assignments_name_from_chunk");

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    private static List<String> present(Connection su, String sql, List<String> names) {
        var have = DSL.using(su, SQLDialect.POSTGRES).fetch(sql).getValues(0, String.class);
        return names.stream().filter(have::contains).toList();
    }

    private static List<String> triggers(Connection su) {
        return present(su, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal", TRIGGERS);
    }

    private static List<String> functions(Connection su) {
        return present(su, "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'nexus'", FUNCTIONS);
    }

    @Test
    void theChangesetCreatesThreeTriggersAndThreeFunctions_itsRollbackRemovesAll_andAReplayRestoresThem() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThat(triggers(su)).as("the migration created every trigger").containsExactlyInAnyOrderElementsOf(TRIGGERS);
            assertThat(functions(su)).as("and every function").containsExactlyInAnyOrderElementsOf(FUNCTIONS);

            PgContainerHelper.replayChangesetRollback(su, FILE, ID);
            assertThat(triggers(su)).as("the rollback removed every trigger").isEmpty();
            assertThat(functions(su)).as("and every function").isEmpty();

            PgContainerHelper.replayChangeset(su, FILE, ID);
            assertThat(triggers(su)).as("replaying the changeset restores every trigger").containsExactlyInAnyOrderElementsOf(TRIGGERS);
            assertThat(functions(su)).containsExactlyInAnyOrderElementsOf(FUNCTIONS);
            var acl = DSL.using(su, SQLDialect.POSTGRES).fetch(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'nexus' "
                    + "AND p.proname = ANY(?) AND has_function_privilege('public', p.oid, 'EXECUTE')", (Object) FUNCTIONS.toArray(String[]::new))
                .getValues(0, String.class);
            assertThat(acl).as("EXECUTE is revoked from PUBLIC on every function").isEmpty();
        }
    }
}
