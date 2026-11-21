// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.CollectionIdSites;
import dev.nexus.service.CollectionIdSites.Site;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Reference;
import dev.nexus.service.db.TenantExportRegistryParityTest.LiveColumn;
import dev.nexus.service.db.TenantExportRegistryParityTest.LiveTable;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The tenant export registry describes RDR-229's {@code collection_id} columns (bead nexus-p2m1x.31, P3.11; RDR-229
 * Technical Design item 10). {@link TenantExportRegistryParityTest} already goes red on an undeclared id-shaped column,
 * which says "classify this"; this class says what the classification must be:
 *
 * <ul>
 *   <li>{@code catalog_collections.collection_id} is the registry's identity. Until P4.1 it is not the primary key, so it
 *       is NOT the table's {@code reassignedId} ("the primary-key column a restore gives new values from the table's own
 *       sequence"); it is a column other tables refer to, and a restore maps the source's values to the target's.</li>
 *   <li>Every table that gains a {@code collection_id} (or {@code superseded_by_id}) declares it as a reference to
 *       {@code catalog_collections(collection_id)}, so the loader can map it.</li>
 * </ul>
 *
 * <p>The columns are read from the live Liquibase-migrated schema and the registry by name, so this compiles before P3.1
 * (nexus-p2m1x.17) adds them. It fails today because they are absent, and each failure line names the table. When P3.1's
 * cut lands, the registry change belongs in the same cut: a column in the schema and not in the registry fails here.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantExportCollectionIdParityTest {

    private static final String REGISTRY = "catalog_collections";
    private static final String IDENTITY = "collection_id";

    PostgreSQLContainer<?> pg;
    Map<String, LiveTable> live;

    @BeforeAll
    void migrateAndRead() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            live = TenantExportRegistryParityTest.readLiveRlsTables(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    private static ExportTable entry(String table) {
        return TenantExport.entry(table).orElseThrow(() -> new AssertionError("TenantExport has no entry for " + table));
    }

    private static boolean refersToRegistryIdentity(ExportTable e, String column) {
        for (Reference r : e.references()) {
            int i = r.columns().indexOf(column);
            if (i >= 0 && r.parentTable().equals(REGISTRY) && r.parentColumns().get(i).equals(IDENTITY)) return true;
        }
        return false;
    }

    /** What is wrong with how the registry declares {@code column} of {@code table}, or null when it is declared as a reference to the registry id. */
    private String undeclaredReference(String table, String column) {
        LiveTable t = live.get(table);
        if (t == null) return table + ": not a live row-level-security table";
        if (!t.columns().containsKey(column)) return table + "." + column + ": not in the live schema (P3.1 has not added it)";
        ExportTable e = entry(table);
        if (!e.namedColumns().contains(column)) return table + "." + column + ": in the schema, not named by the registry entry";
        if (!refersToRegistryIdentity(e, column)) {
            return table + "." + column + ": named, but not as a reference to " + REGISTRY + "(" + IDENTITY + ")";
        }
        return null;
    }

    // ── the registry's own identity ─────────────────────────────────────────────────────────────

    @Test
    void theRegistryIdIsDeclaredAndIsNotAReassignedId() {
        ExportTable e = entry(REGISTRY);
        LiveTable t = live.get(REGISTRY);
        assertThat(t.columns().keySet()).as("P3.1 adds catalog_collections.collection_id; its live columns").contains(IDENTITY);
        LiveColumn id = t.columns().get(IDENTITY);
        assertThat(id.type()).as("the id is a bigint (RDR-229 item 6)").isEqualTo("bigint");
        assertThat(id.serial()).as("the id is an identity column of the registry (one installation-wide sequence)").isTrue();

        assertThat(e.namedColumns()).as("the registry entry classifies the id").contains(IDENTITY);
        assertThat(e.reassignedId())
            .as("not the primary key until P4.1: a restore maps the id from old values to the target's, it does not draw it "
                + "from the table's own sequence as a key").isNotEqualTo(IDENTITY);
        assertThat(t.primaryKey()).as("the name primary key stays until P4.1, so the entry keeps its key").doesNotContain(IDENTITY);
        assertThat(e.primaryKey().stream().map(TenantExport.Column::name).toList()).as("the entry's key tracks the live key")
            .isEqualTo(t.primaryKey());
    }

    @Test
    void theTombstoneIdIsDeclaredAsAReferenceToTheRegistryId() {
        // superseded_by -> superseded_by_id: NULL on a live row, the successor's id on a tombstone (item 6)
        assertThat(undeclaredReference(REGISTRY, "superseded_by_id")).as("catalog_collections.superseded_by_id").isNull();
    }

    // ── every table that refers to the registry ─────────────────────────────────────────────────

    private List<String> undeclaredAmong(boolean exported) {
        List<String> found = new ArrayList<>();
        for (Site s : CollectionIdSites.SITES) {
            if (s.exported() != exported) continue;
            LiveTable t = live.get(s.table());
            String column = t == null ? null : s.idCandidates().stream().filter(t.columns()::containsKey).findFirst().orElse(null);
            if (column == null) {
                found.add(s.table() + "." + s.nameColumn() + ": no id column among " + s.idCandidates() + " in the live schema");
                continue;
            }
            String why = undeclaredReference(s.table(), column);
            if (why != null) found.add(why);
        }
        return found;
    }

    @Test
    void everyExportedTableThatGainsACollectionIdDeclaresItAsAReference() {
        long sites = CollectionIdSites.SITES.stream().filter(Site::exported).count();
        assertThat(sites).as("the check examined the exported tables of RDR-229 item 6").isGreaterThanOrEqualTo(10);

        assertThat(undeclaredAmong(true)).as("exported tables whose collection id the registry does not declare (examined " + sites + " sites)").isEmpty();
    }

    @Test
    void everyExcludedTableThatGainsACollectionIdDeclaresItToo() {
        // The census covers every row-level-security table, exported or not: an excluded table's id is still an id-shaped
        // column the parity test must see classified (pdf_pipeline, chash_remap, reembed_jobs, canary_query_ring, the logs).
        long sites = CollectionIdSites.SITES.stream().filter(s -> !s.exported()).count();
        assertThat(sites).as("the check examined the excluded tables of RDR-229 item 6").isGreaterThanOrEqualTo(6);

        assertThat(undeclaredAmong(false)).as("excluded tables whose collection id the registry does not declare (examined " + sites + " sites)").isEmpty();
    }
}
