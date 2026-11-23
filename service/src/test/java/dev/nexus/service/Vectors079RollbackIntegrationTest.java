// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
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

    private static final Table<?> PG_TRIGGER = DSL.table(DSL.name("pg_catalog", "pg_trigger"));
    private static final Table<?> PG_PROC = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
    private static final Table<?> PG_NAMESPACE = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
    private static final Field<String> TGNAME = DSL.field(DSL.name("tgname"), String.class);
    private static final Field<Boolean> TGISINTERNAL = DSL.field(DSL.name("tgisinternal"), Boolean.class);
    private static final Field<String> PRONAME = DSL.field(DSL.name("p", "proname"), String.class);
    private static final Field<Object> PROOID = DSL.field(DSL.name("p", "oid"), Object.class);
    private static final Field<Object> PRONAMESPACE = DSL.field(DSL.name("p", "pronamespace"), Object.class);
    private static final Field<Object> NSPOID = DSL.field(DSL.name("n", "oid"), Object.class);
    private static final Field<String> NSPNAME = DSL.field(DSL.name("n", "nspname"), String.class);

    private static List<String> triggers(Connection su) {
        return DSL.using(su, SQLDialect.POSTGRES).select(TGNAME).from(PG_TRIGGER).where(TGISINTERNAL.eq(false)).and(TGNAME.in(TRIGGERS))
            .fetch(TGNAME);
    }

    private static List<String> functions(Connection su) {
        return DSL.using(su, SQLDialect.POSTGRES).select(PRONAME).from(PG_PROC).join(PG_NAMESPACE).on(PRONAMESPACE.eq(NSPOID))
            .where(NSPNAME.eq("nexus")).and(PRONAME.in(FUNCTIONS)).fetch(PRONAME);
    }

    /** The functions callable by PUBLIC: none after the changeset's REVOKE. */
    private static List<String> publicExecutable(Connection su) {
        Field<Boolean> executable = DSL.function("has_function_privilege", Boolean.class, DSL.inline("public"), PROOID, DSL.inline("EXECUTE"));
        return DSL.using(su, SQLDialect.POSTGRES).select(PRONAME).from(PG_PROC).join(PG_NAMESPACE).on(PRONAMESPACE.eq(NSPOID))
            .where(NSPNAME.eq("nexus")).and(PRONAME.in(FUNCTIONS)).and(executable).fetch(PRONAME);
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
            var acl = publicExecutable(su);
            assertThat(acl).as("EXECUTE is revoked from PUBLIC on every function").isEmpty();
        }
    }
}
