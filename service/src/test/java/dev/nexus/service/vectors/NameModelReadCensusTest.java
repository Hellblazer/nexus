// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * The callers of {@code CollectionRegistration.modelOfConformantName}, pinned (nexus-p2m1x.28). The engine's collection-name
 * parse census ({@link CollectionParseGateTest}) counts string literals per file, not call sites, and this read reuses
 * the version regex, so it adds no literal and that census cannot see it. It is a migration-source name-read site: a
 * re-embed source row deleted by cutover leaves only its name in the job log. It lives in its own class because
 * {@code CollectionParseGateTest} is at the test file size cap.
 */
class NameModelReadCensusTest {

    private static final Pattern IMPORT_LINE = Pattern.compile("(?m)^\\s*import\\s[^;]*;");

    /**
     * Per-file declared count of calls to {@code CollectionRegistration.modelOfConformantName}, the engine's one
     * read of a model out of a collection name (nexus-p2m1x.28). A migration-source site: a re-embed source row
     * deleted by cutover leaves only its name in the job log, and {@code TargetSelection.foreignOrigin} reads the
     * model token from it to tell a sibling on another model from a twin. It reuses the version regex, so the literal
     * literal census in CollectionParseGateTest does not count it; this pin does, in both directions. Retires at RDR-229 P4 (P4.3 finds it here):
     * delete the call, treat a gone row's model as unknown (which refuses), and empty this map.
     */
    private static final Map<String, Integer> NAME_MODEL_READ_CALLS = Map.of(
        "dev/nexus/service/reembed/TargetSelection.java", 1);

    /** Every use of the read's name: a qualified call, a bare call under a static import, a method reference. */
    private static final Pattern NAME_MODEL_READ = Pattern.compile("\\bmodelOfConformantName\\b");

    private static final Pattern DECLARED_AS_STRING_METHOD = Pattern.compile("String\\s+$");

    /**
     * Uses of {@code modelOfConformantName} outside comments, import lines and the declaration of the method itself
     * (the one preceded by its return type). A statically imported bare call and a method reference count, as a
     * qualified call does.
     */
    static int countNameModelReads(String rawSource) {
        String blanked = IMPORT_LINE.matcher(CollectionParseGateTest.blankComments(rawSource)).replaceAll(m -> " ".repeat(m.group().length()));
        Matcher m = NAME_MODEL_READ.matcher(blanked);
        int n = 0;
        while (m.find()) {
            if (!DECLARED_AS_STRING_METHOD.matcher(blanked.substring(Math.max(0, m.start() - 12), m.start())).find()) {
                n++;
            }
        }
        return n;
    }

    /** The model-out-of-a-name read has exactly its declared callers (see {@link #NAME_MODEL_READ_CALLS}). */
    @Test
    void nameModelReads_areExactlyTheDeclaredMigrationSourceCallers() throws IOException {
        Path root = Path.of("src", "main", "java");
        Map<String, Integer> actual = new TreeMap<>();
        int filesWalked;
        try (Stream<Path> files = Files.walk(root)) {
            List<Path> javaFiles = files.filter(p -> p.toString().endsWith(".java")).toList();
            filesWalked = javaFiles.size();
            for (Path p : javaFiles) {
                int n = countNameModelReads(Files.readString(p));
                if (n > 0) actual.put(root.relativize(p).toString().replace(java.io.File.separatorChar, '/'), n);
            }
        }
        assertThat(filesWalked).as("non-vacuity: the walk saw the tree").isGreaterThan(0);
        assertThat(NAME_MODEL_READ_CALLS).as("non-vacuity: a declared caller exists to find").isNotEmpty();
        assertThat(actual).as("callers of modelOfConformantName: a new one is a new name-read site and needs a "
            + "declared reason; a removed one lowers this pin (nexus-p2m1x.28, retires at RDR-229 P4)")
            .isEqualTo(NAME_MODEL_READ_CALLS);
    }

    @Test
    void nameModelReadCount_seesQualifiedBareAndReferenceUses_butNotTheDeclarationCommentsOrImports() {
        String src = String.join("\n",
            "import static dev.nexus.service.db.CollectionRegistration.modelOfConformantName;",
            "// modelOfConformantName(commented)",
            "class A {",
            "    /** modelOfConformantName in javadoc */",
            "    public static String modelOfConformantName(String n) { return null; }",
            "    void q(String n) { CollectionRegistration.modelOfConformantName(n); }",
            "    void b(String n) { modelOfConformantName(n); }",
            "    void r() { java.util.function.Function<String, String> f = CollectionRegistration::modelOfConformantName; }",
            "}");
        assertThat(countNameModelReads(src)).isEqualTo(3);
    }
}
