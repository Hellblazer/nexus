// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Reference;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.1 item 7 (nexus-p2m1x.17): the {@code remapped} declaration and the id twins' references in
 * {@link TenantExport#TABLES}, without a database. {@code .remapped} is a no-op in P3.1 (nothing reads it; P3.11's
 * restore does); this class pins the declaration so a later change that drops it fails here.
 */
class TenantExportRemappedModelTest {

    private static final Path LIST = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");

    /** (table, id column) for every id twin the expand lists, including superseded_by_id and the history tables. */
    private static List<String[]> twins() throws IOException {
        List<String[]> twins = new ArrayList<>();
        for (String line : Files.readAllLines(LIST)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
            String[] f = line.split("\t");
            String key = f[0] + "." + f[2];
            if (twins.stream().noneMatch(t -> (t[0] + "." + t[1]).equals(key))) twins.add(new String[] {f[0], f[2]});
        }
        return twins;
    }

    @Test
    void theRegistryRemapsTheRegistrysOwnIdAndNothingElse() {
        ExportTable registry = TenantExport.entry("catalog_collections").orElseThrow();
        assertThat(registry.remapped()).containsExactly("collection_id");
        assertThat(registry.reassignedId())
            .as("reassignedId keeps its primary-key-only meaning; the registry's key is (tenant_id, name)").isNull();
        assertThat(TenantExport.TABLES.stream().filter(t -> !t.remapped().isEmpty()).map(ExportTable::name))
            .containsExactly("catalog_collections");
        assertThat(registry.namedColumns()).contains("collection_id");
    }

    @Test
    void anEntryWithNoRemappedColumnHasAnEmptyList() {
        ExportTable plain = new ExportTable("nexus", "x", ExportClass.DATA, null, List.of(new Column("id", "bigint")),
            "id", List.of(), Map.of(), Set.of(), Set.of(), Set.of());
        assertThat(plain.remapped()).isEmpty();
        ExportTable withIt = new ExportTable("nexus", "x", ExportClass.DATA, null, List.of(new Column("id", "bigint")),
            null, List.of("c"), List.of(), Map.of(), Set.of(), Set.of(), Set.of(), List.of());
        assertThat(withIt.remapped()).containsExactly("c");
        assertThat(withIt.namedColumns()).contains("c");
    }

    @Test
    void everyIdTwinInTheRegistryReferencesTheRegistrysCollectionId() throws IOException {
        int examined = 0;
        for (String[] twin : twins()) {
            ExportTable t = TenantExport.entry(twin[0]).orElse(null);
            if (t == null) continue; // aspect_extraction_queue is absent on most installs and not registered
            examined++;
            assertThat(t.references())
                .as("%s.%s is a reference to catalog_collections(collection_id)", twin[0], twin[1])
                .anySatisfy(r -> {
                    assertThat(r.columns()).containsExactly(twin[1]);
                    assertThat(r.parentName()).isEqualTo("catalog_collections");
                    assertThat(r.parentColumns()).containsExactly("collection_id");
                });
        }
        assertThat(examined).as("non-vacuity: the expand's twins that the registry holds").isGreaterThanOrEqualTo(20);
    }

    @Test
    void theTwinReferencesAreSoftSoAnExistingBackupStillLoadsAndNoForeignKeyIsClaimed() throws IOException {
        for (String[] twin : twins()) {
            ExportTable t = TenantExport.entry(twin[0]).orElse(null);
            if (t == null) continue;
            for (Reference r : t.references()) {
                if (r.columns().equals(List.of(twin[1]))) {
                    assertThat(r.foreignKey()).as("%s.%s: no FK until P3.3a", twin[0], twin[1]).isFalse();
                }
            }
        }
    }
}
