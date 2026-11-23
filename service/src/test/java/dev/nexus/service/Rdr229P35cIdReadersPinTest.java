// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.vectors.IdReaders;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25; linda's P3.4 review item): {@link IdReaders#PRESENT} is true if and only if this engine's readers select
 * by id once gate 3 has latched. Both directions fail:
 * <ul>
 *   <li>{@code PRESENT} true while a reader still routes by name after the latch: the name-predicate per-collection indexes would be dropped
 *       under it ({@code REPLACED_BY_ID});</li>
 *   <li>{@code PRESENT} false while every reader selects by id: the name family would be kept for good and the retire never run.</li>
 * </ul>
 *
 * <p>"The readers select by id" is read off what the tree already pins, not asserted again: (1) it iterates the CENSUS readers
 * ({@code p35c-java-reader-census.tsv}, one group per file, table, clause and column), so a reader with no disposition fails here (it cannot
 * hide behind a missing row), and a reader of a child table's collection column counts as reading the id only when its disposition is
 * {@code id} or {@code both}: {@code label-kept} is not an id read (it is for the registry's own label, {@code catalog_collections.name}, which no
 * name-predicate index serves, and the census test refuses it anywhere else); (2) every SQL search function P3.5b leaves on its name path ({@code name-kept} in
 * {@code p35b-reader-dispositions.tsv}) has its {@code <name>_by_id} twin called from the engine's Java, under the gate
 * ({@code Rdr229P35cGateStructureTest}). A reader that routes by name is a row that fails (1) or a function that fails (2). The check is
 * planted with each failure below, so a pin that cannot fail is caught.
 */
class Rdr229P35cIdReadersPinTest {

    private static final Path MAIN = Path.of("src", "main", "java");
    private static final Set<String> READS_THE_ID = Set.of("id", "both");
    private static final String JAVA = "service/src/main/java/dev/nexus/service/";

    private static List<String> resource(String name) throws IOException {
        try (InputStream in = Rdr229P35cIdReadersPinTest.class.getClassLoader().getResourceAsStream(name)) {
            assertThat(in).as("test resource " + name).isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8).lines()
                .filter(l -> !l.isBlank() && !l.startsWith("#")).toList();
        }
    }

    private static String mainSource() throws IOException {
        StringBuilder all = new StringBuilder();
        try (Stream<Path> walk = Files.walk(MAIN)) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).sorted().toList()) {
                all.append(EgressGuardCensus.stripComments(Files.readString(p))).append('\n');
            }
        }
        return all.toString();
    }

    /** The census readers: the rows (file, line, table, name_column, op, via, note) of the census file; the header and comments are not rows. */
    private static List<String> censusRows(List<String> census) {
        return census.stream().filter(r -> r.startsWith("service/")).toList();
    }

    /** disposition by {@code "<file> <table> <op>\t<name_column>"}, the key the census test uses for a reader group. */
    private static java.util.Map<String, String> dispositionsByGroup(List<String> dispositions) {
        java.util.Map<String, String> out = new java.util.HashMap<>();
        for (String row : dispositions.subList(1, dispositions.size())) {   // [0] is the header
            String[] f = row.split("\t", -1);
            out.put(f[1] + "\t" + f[2], f[3]);
        }
        return out;
    }

    /** The readers that still route by name after the latch, in what the tables say; empty when every reader selects by id. */
    static List<String> routedByName(List<String> census, List<String> javaDispositions, List<String> sqlDispositions, String mainSource) {
        List<String> out = new ArrayList<>();
        var disposition = dispositionsByGroup(javaDispositions);
        java.util.Set<String> seen = new java.util.TreeSet<>();
        for (String row : censusRows(census)) {
            String[] f = row.split("\t", -1);
            // A metadata-key row (name_column metadata.<key>, nexus-p2m1x.25) is a read of a jsonb tag, not of a name column a per-collection
            // name index serves, so the retire this pin guards does not depend on it. tests/test_rdr229_p37_pending_violations.py holds the
            // VIOLATING ones to P3.7; the reaper's are P4.1's, like the by-label reaper SQL above.
            if (f[3].startsWith("metadata.")) continue;
            String group = f[0].replace(JAVA, "") + " " + f[2] + " " + f[4];
            String d = disposition.get(group + "\t" + f[3]);
            if (!seen.add(group + "\t" + f[3])) continue;
            if (d == null) {
                out.add("java reader " + group + " (" + f[3] + ") has no disposition");
            } else if (!READS_THE_ID.contains(d) && !(d.equals("label-kept") && f[3].equals("name"))) {
                out.add("java reader " + group + " (" + f[3] + ") is " + d);
            }
        }
        for (String row : sqlDispositions.subList(1, sqlDispositions.size())) {
            String[] f = row.split("\t", -1);
            if (!f[3].equals("name-kept")) continue;
            String twin = f[1].replaceFirst("^nexus\\.", "").toUpperCase() + "_BY_ID";
            if (!java.util.regex.Pattern.compile("\\b" + twin + "\\b").matcher(mainSource).find()) {
                out.add("sql function " + f[1] + " is name-kept and its twin " + twin + " is not called from the engine");
            }
        }
        return out;
    }

    private static boolean readersSelectById(List<String> census, List<String> java, List<String> sql, String src) {
        return routedByName(census, java, sql, src).isEmpty();
    }

    @Test
    void presentIsTrueIfAndOnlyIfTheReadersSelectById() throws IOException {
        List<String> census = resource("rdr229/p35c-java-reader-census.tsv");
        List<String> java = resource("rdr229/p35c-java-reader-dispositions.tsv");
        List<String> sql = resource("rdr229/p35b-reader-dispositions.tsv");
        String src = mainSource();
        assertThat(censusRows(census).size()).as("non-vacuity: the census readers are read").isGreaterThan(500);
        assertThat(java.size()).as("non-vacuity: the Java dispositions are read").isGreaterThan(200);
        assertThat(sql.stream().filter(r -> r.split("\t", -1)[3].equals("name-kept")).count())
            .as("non-vacuity: the name-kept SQL functions are read").isEqualTo(38);

        List<String> byName = routedByName(census, java, sql, src);
        if (IdReaders.PRESENT) {
            assertThat(byName).as("IdReaders.PRESENT is true while a reader still routes by name after the latch: the name indexes would be "
                + "dropped under it. Fix the reader, or set PRESENT false").isEmpty();
        } else {
            assertThat(byName).as("IdReaders.PRESENT is false while every reader selects by id: set it true, or the retire never runs").isNotEmpty();
        }
        assertThat(IdReaders.PRESENT).as("the invariant, both ways").isEqualTo(readersSelectById(census, java, sql, src));
    }

    @Test
    void thePinCanFailInBothDirections_plantedReaderDispositionAndPlantedMissingTwin() throws IOException {
        List<String> census = resource("rdr229/p35c-java-reader-census.tsv");
        List<String> java = resource("rdr229/p35c-java-reader-dispositions.tsv");
        List<String> sql = resource("rdr229/p35b-reader-dispositions.tsv");
        String src = mainSource();
        assertThat(readersSelectById(census, java, sql, src)).as("the real tree: the readers select by id").isTrue();

        // A census reader with NO disposition (one row removed): the pin names it. The old pin iterated dispositions only and could not see it.
        String victim = java.stream().skip(1).filter(r -> r.split("\t", -1)[3].equals("id")).findFirst().orElseThrow();
        List<String> removed = new ArrayList<>(java);
        removed.remove(victim);
        assertThat(routedByName(census, removed, sql, src)).as("a reader with no disposition").hasSize(1);
        assertThat(readersSelectById(census, removed, sql, src)).isFalse();

        // An id reader of a child table's collection column that becomes label-kept: not an id read.
        String idRow = java.stream().skip(1).filter(r -> {
            String[] f = r.split("\t", -1);
            return f[3].equals("id") && !f[2].equals("name");
        }).findFirst().orElseThrow();
        List<String> relabelled = new ArrayList<>(java);
        relabelled.set(relabelled.indexOf(idRow), idRow.replaceFirst("\tid\t", "\tlabel-kept\t"));
        assertThat(routedByName(census, relabelled, sql, src)).as("an id row turned label-kept").hasSize(1);

        // A Java reader left on the name (a planted census row and its name disposition): PRESENT true would then be wrong.
        List<String> plantedCensus = new ArrayList<>(census);
        plantedCensus.add(JAVA + "db/PlantedReader.java\t1\tplanted_table\tcollection\twhere\tjooq\t-");
        List<String> planted = new ArrayList<>(java);
        planted.add("java\tdb/PlantedReader.java planted_table where\tcollection\tname\tplanted");
        assertThat(routedByName(plantedCensus, planted, sql, src)).as("a Java reader whose disposition reads no id").hasSize(1);
        assertThat(readersSelectById(plantedCensus, planted, sql, src)).isFalse();

        // A name-kept function whose twin the engine never calls: same.
        List<String> plantedSql = new ArrayList<>(sql);
        plantedSql.add("function\tnexus.planted_search_384\tcollection\tname-kept\tplanted");
        assertThat(routedByName(census, java, plantedSql, src)).as("a name-kept function with no twin call").hasSize(1);

        // With the real tables and an engine source that calls no twin, every name-kept function is reported.
        assertThat(routedByName(census, java, sql, "")).as("no twin called: every one of the 38 name-kept functions is reported").hasSize(38);
    }
}
