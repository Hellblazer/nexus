// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * A failed build of the owner-migrated template (nexus-0ty8z) must not leave a half-built
 * {@code nexus_template_owned} behind. Without the drop, the next owner acquire in the fork runs
 * {@code CREATE DATABASE nexus_template_owned} against the leftover and every owner class after the first
 * failure dies on "already exists", which hides the original error.
 */
class SharedClusterOwnedTemplateRetryTest {

    private static boolean templateExists(PostgreSQLContainer<?> c) throws SQLException {
        try (Connection admin = SharedCluster.controlConnection(c)) {
            return DSL.using(admin, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("pg_catalog", "pg_database")),
                DSL.field(DSL.name("datname"), String.class).eq("nexus_template_owned")) == 1;
        }
    }

    @Test
    void aFailedTemplateBuildIsDroppedAndTheNextAcquireRebuildsIt() throws Exception {
        PostgreSQLContainer<?> cluster = SharedCluster.container();
        SharedCluster.resetOwnedTemplate();
        assertThat(templateExists(cluster)).as("non-vacuity: the reset left no template").isFalse();
        try {
            SharedCluster.ownedTemplateBuildFault = () -> {
                throw new IllegalStateException("injected: the build fails after the owner walk");
            };
            assertThatThrownBy(SharedCluster::acquireOwnedDatabase)
                .isInstanceOf(SQLException.class).hasMessageContaining("owner-migrated template build failed")
                .hasRootCauseMessage("injected: the build fails after the owner walk");
            assertThat(templateExists(cluster)).as("the half-built template was dropped").isFalse();
        } finally {
            SharedCluster.ownedTemplateBuildFault = null;
        }
        PostgreSQLContainer<?> retry = SharedCluster.acquireOwnedDatabase();
        try (Connection su = retry.createConnection("")) {
            String owner = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.function("pg_get_userbyid", String.class, DSL.field(DSL.name("c", "relowner"))))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                    .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq("chunks"))
                .fetchOne(0, String.class);
            assertThat(owner).as("the rebuilt template carries the product schema, owned by the template owner")
                .isEqualTo(PgContainerHelper.TEMPLATE_OWNER);
        } finally {
            retry.stop();
        }
        assertThat(templateExists(cluster)).as("the rebuilt template stays for later classes").isTrue();
    }
}
