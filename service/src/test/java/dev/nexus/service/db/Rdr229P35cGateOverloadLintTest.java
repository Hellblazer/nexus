// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the three {@link QuarantineOrigin} overloads that delegate with {@code Gate3.never()} stay only
 * for callers in another group's files and for tests (none left: the assembly threaded the gate through ReembedSourceReader and ChashRepository).
 * A production caller that is not on that list fails here, so a name-path read cannot be added unseen. The list can only shrink:
 * an entry whose call is gone fails too, so it is removed when the caller takes the gate.
 */
class Rdr229P35cGateOverloadLintTest {

    /** The delegating forms: method name and the argument count that selects the name-path overload. */
    private static final Pattern CALL = Pattern.compile("QuarantineOrigin\\.(countRowsOf|isLiveRegistered|retagRowsOf)\\(([^;]*?)\\)\\s*[;)\\s,?:&|]");

    /** Empty since the assembly threaded the gate through ReembedSourceReader and ChashRepository: no production caller uses a gateless form. */
    private static final Set<String> KNOWN = Set.of();

    private static int arguments(String list) {
        int depth = 0, count = 1;
        for (char c : list.toCharArray()) {
            if (c == '(') depth++;
            else if (c == ')') depth--;
            else if (c == ',' && depth == 0) count++;
        }
        return count;
    }

    private static int gateless(String method) {
        return method.equals("retagRowsOf") ? 4 : 3;
    }

    @Test
    void onlyTheKnownProductionCallersUseTheNeverDelegatingOverloads() throws IOException {
        Path root = Path.of("src", "main", "java", "dev", "nexus", "service");
        Set<String> found = new TreeSet<>();
        int scanned = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                scanned++;
                String text = Files.readString(p);
                Matcher m = CALL.matcher(text);
                while (m.find()) {
                    if (arguments(m.group(2)) == gateless(m.group(1))) {
                        found.add(root.relativize(p).toString().replace('\\', '/') + " " + m.group(1));
                    }
                }
            }
        }
        assertThat(scanned).as("non-vacuity: the walk saw the main tree").isGreaterThan(100);
        assertThat(found).as("production callers of a Gate3.never() delegating overload (thread the gate, or list the file with a reason)")
            .isEqualTo(new TreeSet<>(KNOWN));
    }

    @Test
    void theLintSeesAGatelessCallAndIgnoresAGatedOne() {
        List<String> seen = new ArrayList<>();
        for (String source : List.of("QuarantineOrigin.countRowsOf(ctx, t, o);", "QuarantineOrigin.countRowsOf(ctx, t, o, gate3);")) {
            Matcher m = CALL.matcher(source);
            if (m.find() && arguments(m.group(2)) == gateless(m.group(1))) seen.add(source);
        }
        assertThat(seen).containsExactly("QuarantineOrigin.countRowsOf(ctx, t, o);");
    }

    // ---- every method or constructor in main whose body hands Gate3.never() to the overload it delegates to (folio, linda 2026-11-23) --------

    private static final Set<String> NEVER_ALLOWED = Set.of("Gate3.java");

    private static java.util.Map<String, String> mainSources() throws IOException {
        Path root = Path.of("src", "main", "java", "dev", "nexus", "service");
        java.util.Map<String, String> sources = new java.util.LinkedHashMap<>();
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).sorted().toList()) {
                sources.put(root.relativize(p).toString().replace('\\', '/'), Files.readString(p));
            }
        }
        return sources;
    }

    /**
     * Production code reaches a name-path read by a one-argument shortfall only by mistake (resolveNeedEmbedIdx called the four-argument
     * chunkScope, so after the latch and a one-row rename every flush re-embedded). {@link GateNeverLint} finds the delegating overloads of main
     * and every call of one that names no gate, and every other use of {@code Gate3.never()}.
     */
    @Test
    void noProductionCallReachesAnOverloadThatDelegatesWithGate3Never() throws IOException {
        var result = GateNeverLint.scan(mainSources(), NEVER_ALLOWED);
        assertThat(result.overloads()).as("non-vacuity: the scan finds the delegating overloads of main").isGreaterThan(40);
        assertThat(result.neverUses()).as("non-vacuity: and the uses of Gate3.never()").isGreaterThan(50);
        assertThat(result.offenders()).as("production calls of an overload that delegates with Gate3.never(), or other uses of it: pass the "
            + "gate the caller holds").isEmpty();
    }

    // The planted new production callers, one per shape the lint must see. Each source set holds an owner with a gateless overload that
    // delegates and the gated form, and ONE consumer; the control consumer passes a gate and must pass.
    private static final String OWNER = """
        package p;
        final class PlantedRepo {
            PlantedRepo() {
            }
            static int read(int a) {
                return read(a, Gate3.never());
            }
            static int read(int a, Gate3 gate3) {
                return a;
            }
            static int read2(int a) {
                int r = read2(a, Gate3.never());
                return r;
            }
            static int read2(int a, Gate3 gate3) {
                return a;
            }
            int viaThis(int a) {
                return a;
            }
        }
        """;

    private static java.util.Map<String, String> planted(String consumer) {
        java.util.Map<String, String> m = new java.util.LinkedHashMap<>();
        m.put("p/PlantedRepo.java", OWNER);
        m.put("p/Consumer.java", consumer);
        return m;
    }

    private static Set<String> offendersOf(String consumer) {
        return GateNeverLint.scan(planted(consumer), Set.of()).offenders();
    }

    @Test
    void theLintFailsOnANewProductionCallerInEachShape() {
        assertThat(offendersOf("""
            package p;
            final class Consumer {
                void f() {
                    PlantedRepo.read(1);
                }
            }
            """)).as("a qualified static call").hasSize(1);
        assertThat(offendersOf("""
            package p;
            final class Consumer {
                void f(PlantedRepo r) {
                    r.read(1);
                }
            }
            """)).as("a call qualified by a variable of the owner's type").hasSize(1);
        assertThat(offendersOf("""
            package p;
            final class Consumer {
                void f() {
                    var r = new PlantedRepo();
                    r.read(1);
                }
            }
            """)).as("a call qualified by a var assigned the owner").hasSize(1);
        assertThat(offendersOf("""
            package p;
            import static p.PlantedRepo.read;
            final class Consumer {
                int f() {
                    return read(2);
                }
            }
            """)).as("a call through a static import").hasSize(1);
        assertThat(offendersOf("""
            package p;
            final class Consumer {
                void f(String gate) {
                    PlantedRepo.read(gate.length());
                }
            }
            """)).as("an argument that only contains the word gate").hasSize(1);
        assertThat(offendersOf("""
            package p;
            final class Consumer {
                void f(String gateName) {
                    PlantedRepo.read2(gateName.length());
                }
            }
            """)).as("a call of an overload whose delegation is not a return or this(").hasSize(1);
    }

    @Test
    void theLintAcceptsAGatedCallAChainAndADeclaration_andFailsOnAStrayNever() {
        assertThat(offendersOf("""
            package p;
            final class Consumer {
                void f(Gate3 gate3) {
                    PlantedRepo.read(1, gate3);
                    PlantedRepo.read(gate3);
                }
            }
            """)).as("a call that names a Gate3, and a gated overload").isEmpty();
        // a chain: read3 is a shorter gateless form that calls read(int) (a delegating overload) with no gate; its own caller is what counts
        java.util.Map<String, String> chain = new java.util.LinkedHashMap<>();
        chain.put("p/PlantedRepo.java", OWNER.replace("static int read2(int a, Gate3 gate3) {",
            "static int read(int a, int b) {\n        return read(a);\n    }\n    static int read2(int a, Gate3 gate3) {"));
        chain.put("p/Consumer.java", "package p;\nfinal class Consumer {\n}\n");
        assertThat(GateNeverLint.scan(chain, Set.of()).offenders())
            .as("a call inside a gateless chain member is a chain: it counts only through its callers").isEmpty();
        chain.put("p/Consumer.java", "package p;\nfinal class Consumer {\n    void f() {\n        PlantedRepo.read(1, 2);\n    }\n}\n");
        assertThat(GateNeverLint.scan(chain, Set.of()).offenders()).as("and the chain member's own caller is flagged").hasSize(1);
        assertThat(offendersOf("""
            package p;
            final class Consumer {
                private final Gate3 g = Gate3.never();
            }
            """)).as("Gate3.never() as a field").hasSize(1);
        assertThat(offendersOf("""
            package p;
            final class Consumer {
                void f() {
                    other(Gate3.never());
                }
            }
            """)).as("Gate3.never() handed to a call that is not a delegation").hasSize(1);
    }
}
