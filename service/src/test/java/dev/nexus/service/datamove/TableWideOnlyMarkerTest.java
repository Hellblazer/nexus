// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.vectors.Rdr229SwapMove;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * nexus-p2m1x.21: the {@link DataMove#TABLE_WIDE_ONLY_MARKER} contract. A move whose verification carries it has the finisher run
 * no per-tenant statement, so a marked text must read only the catalogs: on a row-security-scoped table the table-wide run sees
 * nothing without a tenant and only the tenant's own run is a check, and a marker on such a text would turn the re-check into one
 * that cannot find a late row. Nothing at run time looks at what the text reads, so these two tests hold the set of marked texts and
 * what they read.
 */
class TableWideOnlyMarkerTest {

    private static final Path MAIN = Path.of("src/main");

    /** The swaps: the only registered moves that carry the marker. */
    private static final List<String> SWAP_TABLES = List.of("chunks", "taxonomy_centroids");

    @Test
    void theMarkedVerificationsInMain_areExactlyTheTwoSwapsLeafKeyVerification() throws IOException {
        // every use of the marker in the main tree outside a comment, by file: where a verification text could be marked
        var uses = new TreeMap<String, Integer>();
        int examined = 0;
        try (Stream<Path> files = Files.walk(MAIN)) {
            for (Path f : files.filter(Files::isRegularFile).toList()) {
                String name = f.getFileName().toString();
                if (!(name.endsWith(".java") || name.endsWith(".xml") || name.endsWith(".sql") || name.endsWith(".yaml")
                    || name.endsWith(".yml") || name.endsWith(".json") || name.endsWith(".properties"))) {
                    continue;
                }
                examined++;
                int n = 0;
                for (String line : Files.readAllLines(f)) {
                    String t = line.stripLeading();
                    boolean comment = t.startsWith("*") || t.startsWith("//") || t.startsWith("/*");
                    if (!comment && (line.contains("nexus:table-wide-only") || line.contains("TABLE_WIDE_ONLY_MARKER"))) {
                        n++;
                    }
                }
                if (n > 0) {
                    uses.put(MAIN.relativize(f).toString(), n);
                }
            }
        }
        assertThat(examined).as("files read (a walk that found none would pass vacuously)").isGreaterThan(500);
        assertThat(uses).as("the marker's definition and its one use in the swaps' leafKeyVerification, nowhere else").containsOnlyKeys(
            "java/dev/nexus/service/datamove/DataMove.java", "java/dev/nexus/service/vectors/Rdr229SwapMove.java");
        assertThat(uses.get("java/dev/nexus/service/vectors/Rdr229SwapMove.java")).as("one use: leafKeyVerification").isEqualTo(1);
        for (String table : SWAP_TABLES) {
            assertThat(Rdr229SwapMove.spec(table).verification().stripLeading()).as("the %s swap's verification", table)
                .startsWith(DataMove.TABLE_WIDE_ONLY_MARKER);
        }
    }

    private static final Pattern RELATION = Pattern.compile("(?i)\\b(?:from|join)\\s+([\\w.\"]+)");

    /** The relations and functions a text reads from, named in a FROM or JOIN; every one must be a pg_catalog name. */
    private static List<String> readFrom(String text) {
        var out = new ArrayList<String>();
        Matcher m = RELATION.matcher(text);
        while (m.find()) {
            out.add(m.group(1));
        }
        return out;
    }

    private static boolean readsOnlyTheCatalog(String text) {
        List<String> from = readFrom(text);
        return !from.isEmpty() && from.stream().allMatch(r -> r.toLowerCase(java.util.Locale.ROOT).startsWith("pg_catalog."));
    }

    @Test
    void everyMarkedText_readsOnlyPgCatalogRelations() {
        int examined = 0;
        for (String table : SWAP_TABLES) {
            String text = Rdr229SwapMove.spec(table).verification();
            assertThat(readFrom(text)).as("the relations the %s swap's verification reads", table)
                .containsExactlyInAnyOrder("pg_catalog.pg_partition_tree", "pg_catalog.pg_index", "pg_catalog.pg_attribute");
            assertThat(readsOnlyTheCatalog(text)).as("the %s swap's verification reads only pg_catalog", table).isTrue();
            examined++;
        }
        assertThat(examined).isEqualTo(SWAP_TABLES.size());
    }

    @Test
    void theCatalogOnlyCheck_refusesATextThatReadsAScopedTable() {
        // negative controls: the check itself must fail a text a scoped table slipped into
        assertThat(readsOnlyTheCatalog("SELECT 1 FROM nexus.chunks t WHERE $1 IS NULL")).isFalse();
        assertThat(readsOnlyTheCatalog("SELECT 1 FROM pg_catalog.pg_index i JOIN nexus.chunks c ON true WHERE $1 IS NULL")).isFalse();
        assertThat(readsOnlyTheCatalog("SELECT 1 WHERE $1 IS NULL")).as("a text that names no relation is not shown to read none").isFalse();
        assertThat(readsOnlyTheCatalog("SELECT 1 FROM pg_catalog.pg_index i WHERE $1 IS NULL")).isTrue();
    }
}
