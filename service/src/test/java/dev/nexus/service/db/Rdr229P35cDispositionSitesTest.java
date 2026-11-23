// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): a disposition row covers every census line of its (file, table, op) group, so one switched site
 * could bless an unswitched one beside it (review of d036a60d49, F2: a by-title select left on the name column under a
 * {@code catalog_documents select} row). Here every census line of this group's files whose group is named in
 * {@code p35c-id-groups-gate-checked.tsv} (the id groups that were disposed with no reason; the set is named, so a reason added to a row does not
 * drop it from the check) and is still disposed {@code id} must read through the gate: its source line, or one within three lines of it, names {@code gate3}, {@code byId},
 * {@code CollectionIdReads} or {@code Gate3}. A group that is not wholly switched is {@code both} with a reason that says why.
 */
class Rdr229P35cDispositionSitesTest {

    private static final Set<String> FILES = Set.of("CatalogRepository", "CollectionTombstones", "CollectionMint", "QuarantineOrigin",
        "DepartedWriterSpans", "ManifestReplace");
    private static final Path RES = Path.of("src", "test", "resources", "rdr229");
    private static final Path SRC = Path.of("src", "main", "java", "dev", "nexus", "service");

    private static boolean readsThroughTheGate(List<String> lines, int line) {
        for (int i = Math.max(1, line - 3); i <= Math.min(lines.size(), line + 3); i++) {
            String t = lines.get(i - 1);
            if (t.contains("gate3") || t.contains("byId") || t.contains("CollectionIdReads") || t.contains("Gate3")) return true;
        }
        return false;
    }

    @Test
    void everyCensusLineOfAnIdGroupWithNoReasonReadsThroughTheGate() throws IOException {
        Set<String> named = new java.util.HashSet<>();
        for (String l : Files.readAllLines(RES.resolve("p35c-id-groups-gate-checked.tsv"))) {
            if (l.startsWith("#") || l.startsWith("object\t") || l.isBlank()) continue;
            named.add(l);
        }
        Map<String, String[]> disposition = new HashMap<>();
        for (String l : Files.readAllLines(RES.resolve("p35c-java-reader-dispositions.tsv"))) {
            if (l.startsWith("java\t")) {
                String[] p = (l + "\t\t\t\t\t").split("\t", -1);
                disposition.put(p[1] + "\t" + p[2], p);
            }
        }
        Map<String, List<String>> sources = new HashMap<>();
        List<String> unswitched = new ArrayList<>();
        int checked = 0;
        for (String l : Files.readAllLines(RES.resolve("p35c-java-reader-census.tsv"))) {
            if (!l.startsWith("service/")) continue;
            String[] c = l.split("\t");
            String file = c[0].substring(c[0].lastIndexOf('/') + 1, c[0].length() - 5);
            if (!FILES.contains(file)) continue;
            String[] d = disposition.get("db/" + file + ".java " + c[2] + " " + c[4] + "\t" + c[3]);
            assertThat(d).as("a disposition for " + l).isNotNull();
            if (!d[3].equals("id") || !named.contains("db/" + file + ".java " + c[2] + " " + c[4] + "\t" + c[3])) continue;
            List<String> lines = sources.computeIfAbsent(file, f -> {
                try {
                    return Files.readAllLines(SRC.resolve("db").resolve(f + ".java"));
                } catch (IOException e) {
                    throw new IllegalStateException(e);
                }
            });
            checked++;
            if (!readsThroughTheGate(lines, Integer.parseInt(c[1]))) unswitched.add(file + ":" + c[1] + " " + c[2] + " " + c[3] + " " + c[4]);
        }
        // 28 lines at this tip: the named set held 31 while every row was id; two of its groups became both (a reason beside the id arm)
        assertThat(checked).as("non-vacuity: the id groups were examined").isGreaterThan(25);
        assertThat(unswitched).as("census lines of an id group that do not read through the gate").isEmpty();
    }
}
