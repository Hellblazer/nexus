// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a, review F12 (nexus-p2m1x.23): production never builds a repository with {@code RenameGuard.open()}, the guard that
 * never refuses. The one-argument constructors that pass it exist for tests; if production used one, a rename would run from gate 3
 * on the stale name columns with no refusal, and nothing would fail. So the text of {@code src/main} is scanned: every constructor
 * that takes a {@code RenameGuard} is found, {@code RenameGuard.open()} appears only in those classes' own one-argument
 * constructors, and every construction of them in main passes the guard. The scan fails when it finds nothing to examine.
 */
class RenameGuardProductionWiringTest {

    private static final Path MAIN = Path.of("src/main/java");
    private static final Pattern GUARDED_CTOR = Pattern.compile("public\\s+(\\w+)\\s*\\([^)]*\\bRenameGuard\\s+\\w+\\s*\\)");

    private static String code(Path file) throws IOException {
        return Files.readString(file).replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
    }

    private static List<Path> mainFiles() throws IOException {
        try (Stream<Path> s = Files.walk(MAIN)) {
            return s.filter(p -> p.toString().endsWith(".java")).toList();
        }
    }

    @Test
    void productionNeverBuildsAGuardedRepositoryWithTheOpenGuard() throws Exception {
        List<String> guarded = new ArrayList<>();
        List<String> openUses = new ArrayList<>();
        List<String> unguardedConstructions = new ArrayList<>();
        var files = mainFiles();
        for (Path f : files) {
            var m = GUARDED_CTOR.matcher(code(f));
            while (m.find()) {
                if (!m.group(1).equals("RenameGuard") && !guarded.contains(m.group(1))) {
                    guarded.add(m.group(1));
                }
            }
        }
        assertThat(guarded).as("non-vacuity: the classes that take a RenameGuard (catalog, chash, taxonomy, telemetry, aspects)")
            .contains("CatalogRepository", "ChashRepository", "TaxonomyRepository", "TelemetryRepository", "AspectRepository");

        int constructions = 0;
        for (Path f : files) {
            String src = code(f);
            String own = f.getFileName().toString().replace(".java", "");
            int at = -1;
            while ((at = src.indexOf("RenameGuard.open()", at + 1)) >= 0) {
                if (!f.getFileName().toString().equals("RenameGuard.java")) {
                    boolean inOwnOneArgConstructor = guarded.contains(own)
                        && src.lastIndexOf("public " + own + "(TenantScope tenantScope) {", at) >= 0
                        && src.indexOf("}", src.lastIndexOf("public " + own + "(TenantScope tenantScope) {", at)) > at;
                    if (!inOwnOneArgConstructor) {
                        openUses.add(f + "@" + at);
                    }
                }
            }
            for (String cls : guarded) {
                var m = Pattern.compile("new\\s+(?:[\\w.]*\\.)?" + cls + "\\s*\\(([^;]*?)\\)\\s*[;)]").matcher(src);
                while (m.find()) {
                    constructions++;
                    String args = m.group(1);
                    if (!args.contains("renameGuard") && !args.contains("RenameGuard")) {
                        unguardedConstructions.add(f.getFileName() + ": new " + cls + "(" + args + ")");
                    }
                }
            }
        }
        assertThat(constructions).as("non-vacuity: main constructs the guarded repositories (NexusService, five of them)").isGreaterThanOrEqualTo(5);
        assertThat(openUses).as("RenameGuard.open() outside a guarded class's own one-argument constructor").isEmpty();
        assertThat(unguardedConstructions).as("a guarded repository built in main without the guard").isEmpty();
    }
}
