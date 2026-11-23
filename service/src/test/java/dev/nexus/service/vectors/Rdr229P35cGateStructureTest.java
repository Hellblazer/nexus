// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the gate is a structure, not a convention. The {@code <name>_by_id} twins read an unstamped row as
 * a row with no sensitive owner (they fail open), so they are called only once gate 3 has latched. This pins, over the main source
 * tree:
 * <ol>
 *   <li>every reference to a twin constant sits in a file this list names, at the count it names, and inside a member that tests the
 *       gate ({@code byId}: the {@link CollectionSelector#byId()} the caller built from the held Gate3, or {@code gate3.holds()});</li>
 *   <li>no main code hands a literal {@code () -> true} (or a captured boolean) to a {@code Gate3} parameter: production passes the
 *       one held gate on, so a reader can never be switched on by a lambda the latch does not own;</li>
 *   <li>no production caller reaches a {@code Gate3.never()}-default overload that a test keeps (PciIdBuilds.plan's eleven-argument form).</li>
 * </ol>
 * Guarded sites, the file's whole list: SearchFunctions (every twin call under {@code sel.byId()}), LexicalGateProbe (the probes, under
 * {@code sel.byId()}), PgVectorRepository ({@code chunk_live_owners_by_id} under the {@code byId} of liveChunksCondition and
 * {@code manifest_qualifying_hits_by_id} under {@code sel.byId()}), TopicRouteArm ({@code search_topic_set_by_id} under {@code byId}).
 * The canary statement is not a site: it stays on the name function until its id target lands, and that change builds a
 * {@link CollectionSelector} for {@code canaryStatement}, which adds no twin reference of its own.
 */
class Rdr229P35cGateStructureTest {

    private static final Path MAIN = Path.of("src", "main", "java");

    /** The twin routine constants vectors-077 adds, as jOOQ names them (a column such as SUPERSEDED_BY_ID is not one). */
    private static final Pattern TWIN = Pattern.compile(
        "\\b(?:PLAIN_SEARCH|MANIFEST_SEARCH|MANIFEST_QUALIFYING_HITS|TEXT_GATE_PROBE(?:_BY_COLLECTION)?|TEXT_GATED_SEARCH_[A-Z_]+?"
            + "|SEARCH_[A-Z_]+?|CHUNK_LIVE_OWNERS)(?:_\\d+)?_BY_ID\\b");

    /** File to the number of twin references in code (not comments, not imports). */
    private static final Map<String, Integer> GUARDED_SITES = Map.of(
        "SearchFunctions.java", 27,
        "LexicalGateProbe.java", 6,
        "TopicRouteArm.java", 3,
        "PgVectorRepository.java", 2);

    private static final Pattern GUARD_TOKEN = Pattern.compile("\\bbyId\\b|\\.byId\\(\\)|\\bgate3\\.holds\\(\\)");

    private static List<Path> mainFiles() throws IOException {
        try (Stream<Path> s = Files.walk(MAIN)) {
            return s.filter(p -> p.toString().endsWith(".java")).sorted().toList();
        }
    }

    private static boolean code(String line) {
        String t = line.strip();
        return !(t.startsWith("*") || t.startsWith("//") || t.startsWith("/*") || t.startsWith("import "));
    }

    @Test
    void everyTwinReferenceSitsInANamedFileAtItsCountAndInsideAMemberThatTestsTheGate() throws IOException {
        Map<String, Integer> found = new TreeMap<>();
        List<String> unguarded = new ArrayList<>();
        for (Path file : mainFiles()) {
            String body = Files.readString(file);
            int count = 0;
            for (String line : body.split("\n")) {
                if (code(line)) {
                    Matcher m = TWIN.matcher(line);
                    while (m.find()) {
                        count++;
                    }
                }
            }
            if (count == 0) {
                continue;
            }
            found.put(file.getFileName().toString(), count);
            int from = 0;
            while (true) {
                int end = body.indexOf("\n    }\n", from);
                String member = end < 0 ? body.substring(from) : body.substring(from, end);
                from = end + 1;
                boolean refs = member.lines().filter(Rdr229P35cGateStructureTest::code).anyMatch(l -> TWIN.matcher(l).find());
                if (refs && !GUARD_TOKEN.matcher(member).find()) {
                    unguarded.add(file.getFileName() + ": a member with a twin reference tests no gate: "
                        + member.strip().lines().findFirst().orElse("?"));
                }
                if (end < 0) {
                    break;
                }
            }
        }
        assertThat(found).as("files that reference an id twin, with their reference counts").isEqualTo(GUARDED_SITES);
        assertThat(unguarded).as("members that call a twin without testing the gate").isEmpty();
        // non-vacuity: the sweep saw the twins of every family at all three widths
        assertThat(found.get("SearchFunctions.java")).isGreaterThanOrEqualTo(24);
    }

    @Test
    void noMainCodeHandsALiteralTrueToAGate3() throws IOException {
        Pattern literal = Pattern.compile("(?:Gate3|CollectionSelector|collectionIs|collectionIn|ownerLateral|batchUpdateMetadata"
            + "|liveOwnedChashes)[^;]*\\(\\)\\s*->\\s*(?:true|byId)\\b"
            // a Gate3 variable bound to a lambda is the production signal only when it reads the LATCHED accessor (Main's one instance)
            + "|\\bGate3\\s+\\w+\\s*=\\s*\\(\\)\\s*->(?!.*\\bgate3Latched\\(\\))");
        List<String> offenders = new ArrayList<>();
        int scanned = 0;
        for (Path file : mainFiles()) {
            List<String> lines = Files.readAllLines(file);
            for (int i = 0; i < lines.size(); i++) {
                if (code(lines.get(i))) {
                    scanned++;
                    if (literal.matcher(lines.get(i)).find()) {
                        offenders.add(file.getFileName() + ":" + (i + 1) + " " + lines.get(i).strip());
                    }
                }
            }
        }
        assertThat(scanned).as("lines scanned").isGreaterThan(50_000);
        assertThat(offenders).as("a literal true (or a captured boolean) passed as a Gate3").isEmpty();
    }

    /** The never()-default overloads were removed except PciIdBuilds.plan's, which a test keeps; production must not call it. */
    @Test
    void noProductionCallerReachesTheElevenArgumentPlanThatReadsTheNamePath() throws IOException {
        List<String> callers = new ArrayList<>();
        for (Path file : mainFiles()) {
            String body = Files.readString(file);
            Matcher m = Pattern.compile("PciIdBuilds\\.plan\\(").matcher(body);
            while (m.find()) {
                if (arguments(body, m.end()) == 11) {
                    callers.add(file.getFileName().toString());
                }
            }
        }
        // PciReconciler's call carries the reconciler's Gate3 since the assembly's wiring commit: no caller is left.
        assertThat(callers).isEmpty();
    }

    /** The number of top-level arguments of the call whose argument list starts at {@code from}. */
    private static int arguments(String body, int from) {
        int depth = 1;
        int args = 1;
        for (int i = from; i < body.length() && depth > 0; i++) {
            char c = body.charAt(i);
            if (c == '(' || c == '[' || c == '{') {
                depth++;
            } else if (c == ')' || c == ']' || c == '}') {
                depth--;
            } else if (c == ',' && depth == 1) {
                args++;
            }
        }
        return args;
    }
}
