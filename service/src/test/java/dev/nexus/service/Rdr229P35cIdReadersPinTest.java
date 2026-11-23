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
 * <p>"The readers select by id" is read off what the tree already pins, not asserted again: (1) every Java reader of
 * {@code p35c-java-reader-census.tsv} has a disposition that reads the id from the latch ({@code id}, {@code both} or {@code label-kept}:
 * the census test refuses a row without one); (2) every SQL search function P3.5b leaves on its name path ({@code name-kept} in
 * {@code p35b-reader-dispositions.tsv}) has its {@code <name>_by_id} twin called from the engine's Java, under the gate
 * ({@code Rdr229P35cGateStructureTest}). A reader that routes by name is a row that fails (1) or a function that fails (2). The check is
 * planted with each failure below, so a pin that cannot fail is caught.
 */
class Rdr229P35cIdReadersPinTest {

    private static final Path MAIN = Path.of("src", "main", "java");
    private static final Set<String> READS_THE_ID = Set.of("id", "both", "label-kept");

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

    /** The readers that still route by name after the latch, in what the two tables say; empty when every reader selects by id. */
    static List<String> routedByName(List<String> javaDispositions, List<String> sqlDispositions, String mainSource) {
        List<String> out = new ArrayList<>();
        for (String row : javaDispositions.subList(1, javaDispositions.size())) {   // [0] is the header
            String[] f = row.split("\t", -1);
            if (!READS_THE_ID.contains(f[3])) out.add("java reader " + f[1] + " (" + f[2] + ") is " + f[3]);
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

    private static boolean readersSelectById(List<String> java, List<String> sql, String src) {
        return routedByName(java, sql, src).isEmpty();
    }

    @Test
    void presentIsTrueIfAndOnlyIfTheReadersSelectById() throws IOException {
        List<String> java = resource("rdr229/p35c-java-reader-dispositions.tsv");
        List<String> sql = resource("rdr229/p35b-reader-dispositions.tsv");
        String src = mainSource();
        assertThat(java.size()).as("non-vacuity: the Java dispositions are read").isGreaterThan(200);
        assertThat(sql.stream().filter(r -> r.split("\t", -1)[3].equals("name-kept")).count())
            .as("non-vacuity: the name-kept SQL functions are read").isEqualTo(38);

        List<String> byName = routedByName(java, sql, src);
        if (IdReaders.PRESENT) {
            assertThat(byName).as("IdReaders.PRESENT is true while a reader still routes by name after the latch: the name indexes would be "
                + "dropped under it. Fix the reader, or set PRESENT false").isEmpty();
        } else {
            assertThat(byName).as("IdReaders.PRESENT is false while every reader selects by id: set it true, or the retire never runs").isNotEmpty();
        }
        assertThat(IdReaders.PRESENT).as("the invariant, both ways").isEqualTo(readersSelectById(java, sql, src));
    }

    @Test
    void thePinCanFailInBothDirections_plantedReaderDispositionAndPlantedMissingTwin() throws IOException {
        List<String> java = resource("rdr229/p35c-java-reader-dispositions.tsv");
        List<String> sql = resource("rdr229/p35b-reader-dispositions.tsv");
        String src = mainSource();
        assertThat(readersSelectById(java, sql, src)).as("the real tree: the readers select by id").isTrue();

        // A Java reader left on the name: PRESENT true would then be wrong, and the check says so.
        List<String> planted = new ArrayList<>(java);
        planted.add("java\tdb/PlantedReader.java planted_table where\tcollection\tname\tplanted");
        assertThat(routedByName(planted, sql, src)).as("a Java reader whose disposition reads no id").hasSize(1);
        assertThat(readersSelectById(planted, sql, src)).isFalse();

        // A name-kept function whose twin the engine never calls: same.
        List<String> plantedSql = new ArrayList<>(sql);
        plantedSql.add("function\tnexus.planted_search_384\tcollection\tname-kept\tplanted");
        assertThat(routedByName(java, plantedSql, src)).as("a name-kept function with no twin call").hasSize(1);

        // And the other direction: a tree whose engine calls no twin and keeps readers on the name reads as not selecting by id, so PRESENT
        // false would be right there; with the real tables and an engine source that calls no twin, every name-kept function is reported.
        assertThat(routedByName(java, sql, "")).as("no twin called: every one of the 38 name-kept functions is reported").hasSize(38);
    }
}
