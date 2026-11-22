// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.ExportTable;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.1 item 7 (nexus-p2m1x.17): {@link TenantExport#TABLES} declares every id the expand adds as computed by the
 * import, without a database. A restore never reads {@code collection_id}, a twin, or {@code superseded_by_id} from a file
 * (it fills them from the restored registry), so each is declared maintained by the load and either as an id-shaped column
 * that holds no other row's key or, once P3.3a or P3.3c keys it, as its id foreign key's reference; none is a reassigned id,
 * and the registry's key stays {@code (tenant_id, name)}.
 */
class TenantExportIdTwinModelTest {

    private static final Path LIST = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");

    /** Every id column of the expand's list (the twins, superseded_by_id, the history labels) by table, as "table.column". */
    private static Set<String> listedIds() throws IOException {
        Set<String> ids = new LinkedHashSet<>();
        for (String line : Files.readAllLines(LIST)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
            String[] f = line.split("\t");
            ids.add(f[0] + "." + f[2]);
        }
        return ids;
    }

    @Test
    void everyIdTheExpandAddsIsDeclaredComputedByTheImport() throws IOException {
        int examined = 0;
        for (String id : listedIds()) {
            String[] tc = id.split("\\.");
            ExportTable t = TenantExport.entry(tc[0]).orElse(null);
            if (t == null) continue;   // aspect_extraction_queue is absent on most installs and not registered
            examined++;
            assertThat(t.triggerMaintained()).as("%s is rewritten by the import", id).contains(tc[1]);
            // no other row's key, or (from P3.3a and P3.3c) the reference of its id foreign key: exactly one of the two
            boolean keyed = t.references().stream().anyMatch(r -> r.foreignKey() && r.columns().contains(tc[1]));
            assertThat(keyed != t.notReferences().contains(tc[1]))
                .as("%s is classified once: as holding no other row's key, or as its id foreign key's reference", id).isTrue();
            assertThat(t.reassignedId()).as("%s is not a reassigned id", id).isNotEqualTo(tc[1]);
        }
        assertThat(examined).as("non-vacuity: the expand's id columns that the registry holds").isGreaterThanOrEqualTo(21);
    }

    @Test
    void everyImportComputedReferenceOfAnExportedTableIsOneTheRestoreFills() {
        // The reverse of the test above: a keyed twin (Entry.importComputedRef) is exempt from the file only because
        // RestoreIdFill writes it before the insert, so every maintained reference column must be one it computes.
        int examined = 0;
        for (ExportTable t : TenantExport.TABLES) {
            if (!t.exported()) continue;   // an excluded table is never restored (canary_query_ring)
            for (var r : t.references()) {
                for (String c : r.columns()) {
                    if (!t.triggerMaintained().contains(c)) continue;
                    examined++;
                    assertThat(RestoreIdFill.computedColumns(t.name())).as("%s.%s is filled by the restore", t.name(), c)
                        .contains(c);
                }
            }
        }
        assertThat(examined).as("non-vacuity: the keyed twins of exported tables (4 at P3.3c alone, 5 at P3.3a alone)")
            .isGreaterThanOrEqualTo(4);
    }

    @Test
    void theRegistrysOwnIdIsDeclaredAndTheKeyIsStillTheName() {
        ExportTable registry = TenantExport.entry("catalog_collections").orElseThrow();
        assertThat(registry.triggerMaintained()).contains("collection_id", "superseded_by_id");
        assertThat(registry.notReferences()).contains("collection_id", "superseded_by_id");
        assertThat(registry.reassignedId()).isNull();
        assertThat(registry.primaryKey().stream().map(TenantExport.Column::name).toList())
            .isEqualTo(List.of("tenant_id", "name"));
    }
}
