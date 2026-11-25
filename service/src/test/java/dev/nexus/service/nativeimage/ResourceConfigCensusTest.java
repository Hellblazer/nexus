// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.nativeimage;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.File;
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
import org.junit.jupiter.api.Test;

/**
 * nexus-p2m1x: every file under {@code src/main/resources} must reach the GraalVM native binary, or be a
 * named file the engine never reads at run time.
 *
 * <p>JVM tests load resources from the classpath, so a resource missing from {@code resource-config.json}
 * is invisible to them. The native binary then lacks it: {@code db/rdr229/p33a-fk-twins.tsv} was missing
 * from engine-service v0.1.200 to v0.1.202, {@code FkTwins} threw "is not on the classpath" from its static
 * holder, and RDR-229's boot-time completion work never started on the native engine.
 *
 * <p>This test walks the resources tree and requires each file to match a {@code pattern} of
 * {@code resource-config.json} (GraalVM matches the pattern against the whole resource path), or an
 * {@code -H:IncludeResources} buildArg of {@code pom.xml}, or the explicit not-read-at-run-time list below.
 * It is not vacuous: it counts the files it examined and asserts the count equals what the tree holds.
 */
class ResourceConfigCensusTest {

    private static final Path RESOURCES = Path.of("src/main/resources");
    private static final Path NATIVE_DIR = RESOURCES.resolve("META-INF/native-image");
    private static final Path POM = Path.of("pom.xml");

    /**
     * Files in the resources tree that no engine code path reads, each with the reason. A file listed here
     * that is gone, or that a pattern now covers anyway, fails the test, so the list cannot rot.
     */
    private static final List<String> NOT_READ_AT_RUN_TIME = List.of(
            // Vendored-library pin records: read by the VendoredLibraryPinTest, DmlUtilsInstallerTest and
            // scripts/vendor_dml_utils.py, never by the engine.
            "db/vendor/pins.properties",
            "db/vendor/ddl_utils/PIN.manifest",
            "db/vendor/dml_utils/PIN.manifest",
            // Documentation and empty-directory markers inside the vendored trees.
            "db/vendor/ddl_utils/changes/procedures/README.md",
            "db/vendor/ddl_utils/changes/functions/README.md",
            "db/vendor/dml_utils/changes/functions/README.md",
            "db/vendor/ddl_utils/changes/procedures/ddl_utils/.gitkeep",
            "db/vendor/ddl_utils/changes/procedures/ddl_utils_lib/.gitkeep");

    @Test
    void everyMainResourceIsEmbeddedInTheNativeBinary() throws IOException {
        ObjectMapper mapper = new ObjectMapper();
        List<Pattern> configPatterns = new ArrayList<>();
        for (JsonNode include : mapper.readTree(NATIVE_DIR.resolve("resource-config.json").toFile())
                .path("resources").path("includes")) {
            configPatterns.add(Pattern.compile(include.path("pattern").asText()));
        }
        assertThat(configPatterns).as("resource-config.json include patterns").isNotEmpty();

        List<Pattern> pomPatterns = pomIncludeResourcesPatterns();
        assertThat(pomPatterns).as("-H:IncludeResources buildArgs of the native profile").isNotEmpty();

        List<String> files;
        try (Stream<Path> walk = Files.walk(RESOURCES)) {
            files = walk.filter(Files::isRegularFile)
                    .filter(p -> !p.startsWith(NATIVE_DIR))
                    .map(p -> RESOURCES.relativize(p).toString().replace(File.separatorChar, '/'))
                    .sorted()
                    .toList();
        }

        long examined = 0;
        List<String> uncovered = new ArrayList<>();
        Set<String> exemptUsed = new TreeSet<>();
        for (String f : files) {
            examined++;
            boolean covered = configPatterns.stream().anyMatch(p -> p.matcher(f).matches())
                    || pomPatterns.stream().anyMatch(p -> p.matcher(f).matches());
            if (NOT_READ_AT_RUN_TIME.contains(f)) {
                assertThat(covered)
                        .as(f + " is on the not-read-at-run-time list but a pattern covers it; drop it from the list")
                        .isFalse();
                exemptUsed.add(f);
            } else if (!covered) {
                uncovered.add(f);
            }
        }

        long treeCount;
        try (Stream<Path> walk = Files.walk(RESOURCES)) {
            treeCount = walk.filter(Files::isRegularFile).filter(p -> !p.startsWith(NATIVE_DIR)).count();
        }
        System.out.println("ResourceConfigCensusTest: examined " + examined + " files (tree holds " + treeCount
                + "), " + exemptUsed.size() + " on the not-read-at-run-time list, " + uncovered.size()
                + " uncovered, against " + configPatterns.size() + " resource-config patterns and "
                + pomPatterns.size() + " pom IncludeResources patterns");

        assertThat(examined).as("files examined").isEqualTo(treeCount).isGreaterThan(400);
        assertThat(exemptUsed).as("every not-read-at-run-time entry names a file that exists")
                .containsExactlyInAnyOrderElementsOf(NOT_READ_AT_RUN_TIME);
        assertThat(uncovered)
                .as("resources the native binary would not contain (add a pattern to resource-config.json)")
                .isEmpty();
    }

    /** The {@code -H:IncludeResources=} patterns of pom.xml, with each {@code ${property}} widened to one path segment. */
    private static List<Pattern> pomIncludeResourcesPatterns() throws IOException {
        String pom = Files.readString(POM);
        Matcher m = Pattern.compile("<buildArg>-H:IncludeResources=([^<]+)</buildArg>").matcher(pom);
        List<Pattern> out = new ArrayList<>();
        while (m.find()) {
            out.add(Pattern.compile(m.group(1).replaceAll("\\$\\{[^}]+}", "[^/]+")));
        }
        return out;
    }
}
