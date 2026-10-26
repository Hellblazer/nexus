// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tko2w: {@code catalog-046-manifest-section-labels.xml} adds three NULLABLE per-position labels
 * ({@code section_title}, {@code section_type}, {@code page_number}) and the {@code labels_written} marker
 * (BOOLEAN NOT NULL DEFAULT false) to {@code catalog_document_chunks}.
 *
 * <p>On an empty database migrated to the changeset before it, the four columns are absent; after it they
 * exist, typed, the labels NULLABLE and the marker NOT NULL defaulting to false (an existing row is read as
 * legacy, not rewritten or backfilled: the only source would be the shared chunk row, the wrong value for a
 * shared chunk); its own rollback drops exactly the four columns; the full changelog applies again over the
 * rolled-back schema.
 */
class Catalog046ManifestSectionLabelsSchemaLiquibaseTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String PRIOR_CHANGESET_ID = "vectors-037-2";
    private static final String TARGET_CHANGESET_ID = "catalog-046-1";

    @Test
    void addsThreeNullableLabelsAndTheWrittenMarker_rollsBackAndReapplies() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, PRIOR_CHANGESET_ID);
            }
            try (Connection su = dedicated.createConnection("")) {
                assertThat(columns(su)).as("before the changeset")
                    .doesNotContainKeys("section_title", "section_type", "page_number", "labels_written");
            }
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, TARGET_CHANGESET_ID);
            }
            try (Connection su = dedicated.createConnection("")) {
                Map<String, String> cols = columns(su);
                assertThat(cols).containsEntry("section_title", "text")
                    .containsEntry("section_type", "text")
                    .containsEntry("page_number", "int4")
                    .containsEntry("labels_written", "bool");
                assertThat(nullable(su)).as("the labels are NULLABLE, the marker is not").containsOnly(
                    Map.entry("section_title", "YES"), Map.entry("section_type", "YES"),
                    Map.entry("page_number", "YES"), Map.entry("labels_written", "NO"));
                // The DEFAULT is what makes every row written before catalog-046 read as legacy (labels
                // NULL, not written), with no rewrite of the table.
                try (ResultSet rs = su.getMetaData().getColumns(null, "nexus", "catalog_document_chunks", "labels_written")) {
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getString("COLUMN_DEF")).isEqualTo("false");
                }
            }
            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                try (Liquibase liquibase = new Liquibase(
                        MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                    liquibase.rollback(1, new Contexts(), new LabelExpression());
                }
            }
            try (Connection su = dedicated.createConnection("")) {
                assertThat(columns(su)).as("rollback drops exactly the four columns")
                    .doesNotContainKeys("section_title", "section_type", "page_number", "labels_written")
                    .containsKeys("chash", "line_start", "char_end", "collection", "embedding_model");
            }
            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
                liquibase.update(new Contexts(), new LabelExpression());
                su.setAutoCommit(true);
            }
            try (Connection su = dedicated.createConnection("")) {
                assertThat(columns(su)).containsKeys("section_title", "section_type", "page_number", "labels_written");
            }
        } finally {
            dedicated.stop();
        }
    }

    /** Column name to its type name, through JDBC metadata (no SQL text). */
    private static Map<String, String> columns(Connection su) throws Exception {
        Map<String, String> out = new HashMap<>();
        try (ResultSet rs = su.getMetaData().getColumns(null, "nexus", "catalog_document_chunks", null)) {
            while (rs.next()) out.put(rs.getString("COLUMN_NAME"), rs.getString("TYPE_NAME"));
        }
        return out;
    }

    /** The label columns' and marker's {@code IS_NULLABLE}, through JDBC metadata. */
    private static Map<String, String> nullable(Connection su) throws Exception {
        Map<String, String> out = new HashMap<>();
        try (ResultSet rs = su.getMetaData().getColumns(null, "nexus", "catalog_document_chunks", null)) {
            while (rs.next()) {
                String name = rs.getString("COLUMN_NAME");
                if (name.equals("section_title") || name.equals("section_type") || name.equals("page_number")
                        || name.equals("labels_written")) {
                    out.put(name, rs.getString("IS_NULLABLE"));
                }
            }
        }
        return out;
    }

    private static void migrateUpTo(Connection conn, String targetChangesetId) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(conn));
        Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
        int idx = -1;
        for (int i = 0; i < unrun.size(); i++) {
            if (targetChangesetId.equals(unrun.get(i).getId())) {
                idx = i;
                break;
            }
        }
        assertThat(idx).as(targetChangesetId + " must be present in the master changelog").isGreaterThanOrEqualTo(0);
        liquibase.update(idx + 1, new Contexts(), new LabelExpression());
        conn.setAutoCommit(true);
    }
}
