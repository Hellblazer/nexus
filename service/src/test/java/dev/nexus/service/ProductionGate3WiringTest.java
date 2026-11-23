// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

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
 * RDR-229 P3.5c assembly (nexus-p2m1x.25): no production site builds a gate-reading class on the never-latched signal. Every class
 * below has a convenience form that passes {@code Gate3.never()} (the tests use it); a production caller that took that form would
 * compile, stay green and read the name columns forever, so the wiring is pinned on comment-stripped source.
 *
 * <p>Two pins. (1) Each construction of a listed class (and each {@code PciReconciler.create} with a pool) outside the class's own
 * file passes an argument that names the gate: {@code gate3}, {@code repo.gate3()}, or a local the gate was read into. (2) The set of
 * production files that mention {@code Gate3.never()} at all is exactly the classes' own convenience overloads and the service's
 * test seam; a new file, or {@code Main} or a route, mentioning it fails here.
 *
 * <p>Not pinned, by decision: {@code NexusService}'s public eight-argument constructor chain ends in the package-private
 * thirteen-argument form that passes {@code Gate3.never()}; five tests in other packages build the service through it, so it stays
 * public, and {@code Main} calls the fifteen-argument form with the one signal (pinned in {@link MainGate3WiringTest}).
 */
class ProductionGate3WiringTest {

    private static final Path MAIN = Path.of("src/main/java");

    /** Classes whose constructors take the gate; a construction elsewhere must pass it. */
    private static final List<String> GATED = List.of(
        "PgVectorRepository", "ChunkConstraintAdmin", "CombinedWriteService", "ChunkKeySweep", "ReaperRepository",
        "TelemetryRepository", "PipelineRepository", "AspectQueueDrain", "TaxonomyCentroidRepository", "TopicRouteArm",
        "UriSegmentCensusReads", "PdfStubMetadataReads", "IndexRunCensusReads", "ChashConstraintValidator",
        "ChashConformanceHandler", "PciQueryRingReader", "TaxonomyRepository", "AspectRepository", "ChashRepository",
        "RemapRepository", "ReembedJobRepository", "ReembedSourceReader", "CatalogRepository", "ManifestLabelStage");

    /** The files whose own overloads (or the service's test seam) mention {@code Gate3.never()}, and how many times. */
    private static final Set<String> MENTIONS_NEVER = Set.of(
        "NexusService.java", "db/AspectQueueDrain.java", "db/AspectRepository.java", "db/CatalogRepository.java",
        "db/ChashConstraintValidator.java", "db/ChashRepository.java", "db/CombinedWriteService.java",
        "db/IndexRunCensusReads.java", "db/LegacyChunkSurvey.java", "db/PdfStubMetadataReads.java", "db/PipelineRepository.java",
        "db/QuarantineOrigin.java", "db/ReembedJobRepository.java", "db/ReembedSourceReader.java", "db/RemapRepository.java",
        "db/RenameGuard.java", "db/TaxonomyRepository.java", "db/TelemetryRepository.java", "db/UriSegmentCensusReads.java",
        "http/ChashConformanceHandler.java", "vectors/ChunkConstraintAdmin.java", "vectors/ChunkKeySweep.java",
        "vectors/ChunkOwnerLabel.java", "vectors/ChunkSensitivity.java", "vectors/ManifestLabelStage.java",
        "vectors/PciIdBuilds.java", "vectors/PciLeafCounting.java", "vectors/PciQueryRingReader.java",
        "vectors/PciReconciler.java", "vectors/PgVectorRepository.java", "vectors/ReaperRepository.java",
        "vectors/TaxonomyCentroidRepository.java", "vectors/TopicRouteArm.java");

    /** Comments removed, string and text-block literals kept whole: a {@code "/*"} inside a literal must not swallow the code after it. */
    private static String strip(String src) {
        return EgressGuardCensus.stripComments(src);
    }

    private static List<Path> sources() throws IOException {
        try (Stream<Path> s = Files.walk(MAIN.resolve("dev/nexus/service"))) {
            return s.filter(p -> p.toString().endsWith(".java")).toList();
        }
    }

    /** The text between the parenthesis that opens at {@code open} and its match. */
    private static String args(String code, int open) {
        int depth = 0;
        for (int i = open; i < code.length(); i++) {
            char c = code.charAt(i);
            if (c == '(') depth++;
            else if (c == ')' && --depth == 0) return code.substring(open + 1, i);
        }
        throw new IllegalStateException("unbalanced call at " + open);
    }

    private static boolean namesTheGate(String args) {
        return args.matches("(?s).*(gate3|Gate3|gate3\\(\\)|\\bgate\\b|readGate).*");
    }

    @Test
    void everyProductionConstructionOfAGatedClassPassesTheGate() throws Exception {
        List<String> bare = new ArrayList<>();
        int examined = 0;
        for (Path p : sources()) {
            String own = p.getFileName().toString().replace(".java", "");
            String code = strip(Files.readString(p));
            for (String cls : GATED) {
                if (cls.equals(own)) continue;   // the class's own convenience overloads delegate with Gate3.never(); pinned by (2)
                Matcher m = Pattern.compile("new\\s+(?:[a-z][\\w.]*\\.)?" + cls + "\\s*\\(").matcher(code);
                while (m.find()) {
                    examined++;
                    String a = args(code, m.end() - 1);
                    if (!namesTheGate(a)) bare.add(own + ": new " + cls + "(" + a.strip().replaceAll("\\s+", " ") + ")");
                }
            }
        }
        assertThat(examined).as("non-vacuity: the production constructions of gated classes found").isGreaterThanOrEqualTo(25);
        assertThat(bare).as("a production site that built a gated class without the gate").isEmpty();
    }

    @Test
    void thePciReconcilerIsCreatedWithTheGate_andMainNeverBuildsAGateOfItsOwn() throws Exception {
        String main = strip(Files.readString(MAIN.resolve("dev/nexus/service/Main.java")));
        Matcher m = Pattern.compile("PciReconciler\\.create\\s*\\(").matcher(main);
        assertThat(m.find()).as("Main creates the reconciler").isTrue();
        assertThat(namesTheGate(args(main, m.end() - 1))).as("with the one gate 3 signal").isTrue();
        assertThat(main).doesNotContain("Gate3.never()");
    }

    @Test
    void onlyTheDeclaredFilesMentionTheNeverLatchedSignal() throws Exception {
        Set<String> found = new TreeSet<>();
        for (Path p : sources()) {
            if (Pattern.compile("Gate3\\s*\\.\\s*never\\s*\\(\\s*\\)").matcher(strip(Files.readString(p))).find()) {
                found.add(MAIN.resolve("dev/nexus/service").relativize(p).toString());
            }
        }
        assertThat(found).as("production files that mention Gate3.never(): the classes' own test overloads and the service's test seam")
            .containsExactlyInAnyOrderElementsOf(MENTIONS_NEVER);
    }
}
