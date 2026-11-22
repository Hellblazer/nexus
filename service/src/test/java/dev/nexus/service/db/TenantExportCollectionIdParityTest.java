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
 * The tenant export registry describes RDR-229's {@code collection_id} columns (bead nexus-p2m1x.31, P3.11; RDR-229 Technical
 * Design item 10), in the form P3.1's restore uses: the import computes every id, it never reads one from the file. The load
 * draws the registry's {@code collection_id} from the column's own sequence and refills every twin, {@code superseded_by_id}
 * included, by name from the restored registry ({@code RestoreIdFill}). So each id column is declared <em>import-computed</em>
 * ({@code Entry.importComputed}: maintained, so the load's verification skips it, and an id-shaped column that holds no other
 * row's key), and none is a {@link TenantExport.Reference} or the table's {@code reassignedId}.
 *
 * <p>{@link TenantExportRegistryParityTest} already goes red on an undeclared id-shaped column, which says "classify this";
 * this class says what the classification must be, for every site of RDR-229 item 6 ({@link CollectionIdSites#SITES}), read from
 * the live Liquibase-migrated schema.
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

    /** What is wrong with how the registry declares {@code column} of {@code table}, or null when it is declared import-computed. */
    private String misdeclared(String table, String column) {
        LiveTable t = live.get(table);
        if (t == null) return table + ": not a live row-level-security table";
        if (!t.columns().containsKey(column)) return table + "." + column + ": not in the live schema";
        ExportTable e = entry(table);
        List<String> why = new ArrayList<>();
        if (!e.namedColumns().contains(column)) why.add("not named by the registry entry");
        if (!e.triggerMaintained().contains(column)) why.add("not maintained (import-computed ids are skipped by the load's verification)");
        // Before its id foreign key the twin holds no other row's key; under one (P3.3a, P3.3c) it is that key's reference
        // instead. Either way the load must not remap it: a reference is safe only to a parent with no reassigned id, by the
        // parent's collection_id, so the load inserts the value the fill wrote.
        List<Reference> keys = e.references().stream().filter(r -> r.columns().contains(column)).toList();
        if (keys.isEmpty() && !e.notReferences().contains(column)) {
            why.add("neither classed as an id that holds no other row's key nor under an id foreign key");
        }
        if (!keys.isEmpty() && e.notReferences().contains(column)) {
            why.add("under an id foreign key and still classed as holding no other row's key");
        }
        if (column.equals(e.reassignedId())) why.add("declared as the reassignedId, which is the primary key a restore draws from its own sequence");
        for (Reference r : keys) {
            ExportTable parent = TenantExport.entry(r.parentTable()).orElse(null);
            boolean byId = r.foreignKey() && r.parentColumns().get(r.columns().indexOf(column)).equals("collection_id");
            if (!byId || parent == null || parent.reassignedId() != null) {
                why.add("declared as a reference to " + r.parentName() + " that is not an id foreign key to a parent with no "
                    + "reassigned id, which the load would then remap");
            }
        }
        return why.isEmpty() ? null : table + "." + column + ": " + String.join("; ", why);
    }

    // ── the registry's own identity ─────────────────────────────────────────────────────────────

    @Test
    void theRegistryIdIsLiveAnIdentityAndDeclaredImportComputed() {
        ExportTable e = entry(REGISTRY);
        LiveTable t = live.get(REGISTRY);
        assertThat(t.columns().keySet()).as("catalog_collections' live columns").contains(IDENTITY);
        LiveColumn id = t.columns().get(IDENTITY);
        assertThat(id.type()).as("the id is a bigint (RDR-229 item 6)").isEqualTo("bigint");
        assertThat(id.serial()).as("the id is an identity column of the registry (one installation-wide sequence)").isTrue();
        assertThat(t.primaryKey()).as("the name primary key stays until P4.1").doesNotContain(IDENTITY);
        assertThat(e.primaryKey().stream().map(TenantExport.Column::name).toList()).as("the entry's key tracks the live key")
            .isEqualTo(t.primaryKey());

        assertThat(misdeclared(REGISTRY, IDENTITY)).as("catalog_collections.collection_id").isNull();
    }

    @Test
    void theTombstoneIdIsDeclaredImportComputed() {
        // superseded_by -> superseded_by_id: NULL on a live row, the successor's id on a tombstone (item 6). The restore re-points it.
        assertThat(misdeclared(REGISTRY, "superseded_by_id")).as("catalog_collections.superseded_by_id").isNull();
    }

    // ── every table that names a collection ─────────────────────────────────────────────────────

    private List<String> misdeclaredAmong(boolean exported) {
        List<String> found = new ArrayList<>();
        for (Site s : CollectionIdSites.SITES) {
            if (s.exported() != exported) continue;
            LiveTable t = live.get(s.table());
            String column = t == null ? null : s.idCandidates().stream().filter(t.columns()::containsKey).findFirst().orElse(null);
            if (column == null) {
                found.add(s.table() + "." + s.nameColumn() + ": no id column among " + s.idCandidates() + " in the live schema");
                continue;
            }
            String why = misdeclared(s.table(), column);
            if (why != null) found.add(why);
        }
        return found;
    }

    @Test
    void everyExportedTableThatNamesACollectionDeclaresItsIdImportComputed() {
        long sites = CollectionIdSites.SITES.stream().filter(Site::exported).count();
        assertThat(sites).as("the check examined the exported tables of RDR-229 item 6").isGreaterThanOrEqualTo(10);

        assertThat(misdeclaredAmong(true)).as("exported tables whose collection id is not declared import-computed (examined " + sites + " sites)")
            .isEmpty();
    }

    @Test
    void everyExcludedTableThatNamesACollectionDeclaresItsIdImportComputedToo() {
        // The census covers every row-level-security table, exported or not: an excluded table's id is still an id-shaped
        // column the parity test must see classified (pdf_pipeline, chash_remap, reembed_jobs, canary_query_ring, the logs).
        long sites = CollectionIdSites.SITES.stream().filter(s -> !s.exported()).count();
        assertThat(sites).as("the check examined the excluded tables of RDR-229 item 6").isGreaterThanOrEqualTo(6);

        assertThat(misdeclaredAmong(false)).as("excluded tables whose collection id is not declared import-computed (examined " + sites + " sites)")
            .isEmpty();
    }
}
