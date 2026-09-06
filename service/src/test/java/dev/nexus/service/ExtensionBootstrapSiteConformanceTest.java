// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URISyntaxException;
import java.net.URL;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Class-level tripwire for the extension-bootstrap-site hazard (nexus-cbo4a
 * batch 9 item 0, Sam's directive, 2026-09-05): {@code CREATE EXTENSION} may
 * appear as a real {@code <sql>} statement ONLY at the sanctioned bootstrap
 * sites named in {@link #SANCTIONED_FILES} below — never in any other
 * changeset, product or test.
 *
 * <p>This is deliberately NOT "no {@code CREATE EXTENSION} without an
 * explicit {@code SCHEMA} clause" — that phrasing would incorrectly flag
 * {@code vectors-001-baseline.xml}'s {@code vectors-001-1}, which is applied
 * everywhere and therefore can never be edited to add one (see the hot rule:
 * never edit an applied changeset). The extensions are relocated into {@code
 * nexus} AFTER creation instead, by {@code
 * search-path-001-relocate-vector-extensions.xml}'s guarded {@code ALTER
 * EXTENSION ... SET SCHEMA nexus} (superuser-only; see that file's own
 * header for why relocation, not schema-at-create-time, is the mechanism).
 * The real invariant this gate enforces is narrower and mechanical: {@code
 * CREATE EXTENSION} is a superuser-only, install-once operation, and every
 * site that runs it must be one of the three DOCUMENTED bootstrap sites
 * (product changelog, client-side local-install provisioning, or the
 * production DBA prerequisite) — never a NEW changeset, and never
 * accidentally reintroduced somewhere a non-superuser migration role will
 * hit it and fail.
 *
 * <p>Sites reachable from THIS repository (the production DBA prerequisite
 * is documented, not code — see {@code SchemaMigrator}'s javadoc and {@code
 * src/nexus/db/pg_provision.py}'s {@code provision()} for the other two):
 * <ul>
 *   <li>{@code vectors-001-baseline.xml} (changeset {@code vectors-001-1}) —
 *       the product changelog's own bootstrap, applied everywhere, never
 *       edited.</li>
 * </ul>
 *
 * <p>Test-side bootstrap fixtures (this repository's OWN superuser-connection
 * setup, modeling the production DBA pre-step) are ALSO sanctioned per-file,
 * in {@link #SANCTIONED_JAVA_FILES} — they are not product changesets, but a
 * real regression here (a test silently losing its {@code CREATE EXTENSION}
 * step, or a NEW file adding one outside this list) is exactly the class of
 * drift this gate exists to catch, so they are named explicitly rather than
 * exempted by directory. The scan walks BOTH {@code src/main/java} and
 * {@code src/test/java} (relative to the module root, matching {@link
 * dev.nexus.service.db.RawSqlGateTest}'s own convention for locating Java
 * sources) in addition to the changelog XML roots above — a javadoc
 * genuinely NEEDS to say "CREATE EXTENSION" in prose sometimes (see this very
 * class's own javadoc), so the Java scanner strips line and block comments
 * before searching quoted string literals, the same false-positive concern
 * the XML scanner's {@code <comment>} exclusion already handles for
 * changelog files.
 */
class ExtensionBootstrapSiteConformanceTest {

    private static final Pattern CREATE_EXTENSION_PATTERN =
        Pattern.compile("\\bCREATE\\s+EXTENSION\\b", Pattern.CASE_INSENSITIVE);

    private static final Pattern OPEN_SQL_TAG = Pattern.compile("<sql[\\s>]");

    /**
     * (filename) allowed to carry a real {@code CREATE EXTENSION} {@code
     * <sql>} statement. File-level granularity matches {@link
     * ChangelogBulkGrantConformanceTest}'s allowlist precedent — each entry
     * must be independently verified, not merely present.
     *
     * <p><b>vectors-001-baseline.xml</b>: {@code vectors-001-1}, the
     * product's sole bootstrap changeset for {@code vector}/{@code
     * pg_trgm}. Applied everywhere; never edited (hot rule).
     */
    private static final Set<String> SANCTIONED_FILES = Set.of("vectors-001-baseline.xml");

    /**
     * (filename) Java test fixtures sanctioned to carry a literal {@code
     * CREATE EXTENSION IF NOT EXISTS vector}/{@code pg_trgm} string. Two
     * shapes:
     *
     * <p>Eleven of them model the production DBA / client-provisioning
     * pre-step under a superuser connection so their own {@code
     * SchemaMigrator}/Liquibase walk (run as a NOSUPERUSER migrating role,
     * mirroring production) has the extensions already present — see {@code
     * SchemaMigratorIntegrationTest.bootstrapVectorExtensionsForFreshWalk}'s
     * javadoc for the full derivation of why these specific files need this
     * (every OTHER test either never runs Liquibase at all, or runs it via
     * {@code PgContainerHelper#applyProductSchema}/{@code #start()}, whose
     * shared, already-migrated cluster needs no separate extension bootstrap
     * since {@code vectors-001-baseline.xml}'s own {@code vectors-001-1}
     * already created it there).
     *
     * <p>{@code PgSessionEfSearchReadbackIntegrationTest} is the twelfth,
     * different shape: it never runs Liquibase itself at all (it drives
     * {@code PgSession#setHnswEfSearch} directly against a shared, already-
     * migrated {@code PgContainerHelper#start()} cluster), and its bare
     * {@code CREATE EXTENSION IF NOT EXISTS vector} is a purely DEFENSIVE,
     * idempotent no-op (the shared cluster's own bootstrap already created
     * and relocated the extension) that predates this batch — kept as
     * belt-and-braces so the test does not depend on load ordering relative
     * to whichever other test class happens to migrate the shared cluster
     * first within a given surefire fork.
     */
    private static final Set<String> SANCTIONED_JAVA_FILES = Set.of(
        "PgSessionEfSearchReadbackIntegrationTest.java",
        "AspectDocIdBackfillTest.java",
        "GrantsNexusDiagViewAccessIntegrationTest.java",
        "GrantsPgMonitorTest.java",
        "GrantsSvcForeignOwnedRelationTest.java",
        "Hygiene001NotNullMigrationRlsTest.java",
        "SchemaMigratorIntegrationTest.java",
        "SchemaRollbackRoundTripIntegrationTest.java",
        "SchemaUpgradeRehearsalIntegrationTest.java",
        "VectorsRepointFunctionsIntegrationTest.java",
        "VectorsUnifyCentroidsIntegrationTest.java",
        "VectorsUnifyChunksIntegrationTest.java");

    @Test
    void createExtensionAppearsOnlyAtSanctionedBootstrapSites() throws IOException, URISyntaxException {
        List<String> violations = new ArrayList<>();

        for (Path root : changelogScanRoots()) {
            try (var walk = Files.walk(root)) {
                for (Path file : walk.filter(p -> p.toString().endsWith(".xml")).toList()) {
                    String filename = file.getFileName().toString();
                    if (SANCTIONED_FILES.contains(filename)) {
                        continue;
                    }
                    violations.addAll(
                        scanForCreateExtension(filename, Files.readAllLines(file)));
                }
            }
        }

        assertThat(violations)
            .as("CREATE EXTENSION outside a sanctioned bootstrap site (nexus-cbo4a batch 9 "
                + "item 0): CREATE EXTENSION is a superuser-only, install-once operation. A "
                + "NEW changeset running it will fail hard the instant a real deployment's "
                + "migration role is NOT superuser (the two-role production shape). Either "
                + "the extension is already provided by vectors-001-baseline.xml's "
                + "vectors-001-1, or this needs the production DBA / client-provisioning "
                + "pre-step documented in SchemaMigrator's javadoc, not a new changeset.")
            .isEmpty();
    }

    /**
     * Java-source half of the same invariant (F1, code review follow-up on
     * this class's own javadoc claiming Java sites were sanctioned when the
     * scan never actually walked {@code src/*}/java): every {@code CREATE
     * EXTENSION} string literal in {@code src/main/java} or {@code
     * src/test/java} must live in one of {@link #SANCTIONED_JAVA_FILES} — a
     * NEW test silently adding its own extension-bootstrap dance (or a
     * REMOVED file's entry going stale) is exactly the drift class this gate
     * exists to catch on the XML side; this closes the identical gap for
     * Java.
     */
    @Test
    void createExtensionInJavaSourcesOnlyAtSanctionedFixtures() throws IOException {
        List<String> violations = new ArrayList<>();

        // This class's OWN filename is excluded: its positive-case synthetic
        // fixtures (javaStringLiteral_withCreateExtension_flags) deliberately
        // embed a real "CREATE EXTENSION" string literal to prove the scanner
        // catches it — that literal is test-fixture data, not an actual
        // bootstrap site, and would otherwise flag the gate against itself.
        String selfFilename = ExtensionBootstrapSiteConformanceTest.class.getSimpleName() + ".java";

        for (String srcRoot : List.of("main", "test")) {
            Path root = Path.of("src", srcRoot, "java");
            if (!Files.exists(root)) {
                continue;
            }
            try (var walk = Files.walk(root)) {
                for (Path file : walk.filter(p -> p.toString().endsWith(".java")).toList()) {
                    String filename = file.getFileName().toString();
                    if (SANCTIONED_JAVA_FILES.contains(filename) || filename.equals(selfFilename)) {
                        continue;
                    }
                    violations.addAll(
                        scanJavaForCreateExtension(filename, Files.readString(file)));
                }
            }
        }

        assertThat(violations)
            .as("CREATE EXTENSION string literal in a Java source file outside "
                + "SANCTIONED_JAVA_FILES (nexus-cbo4a batch 9 item 0, F1): either add the "
                + "new fixture to that allowlist (if it genuinely needs its own superuser "
                + "extension bootstrap, mirroring SchemaMigratorIntegrationTest."
                + "bootstrapVectorExtensionsForFreshWalk) or use PgContainerHelper#"
                + "applyProductSchema instead, which needs no separate bootstrap.")
            .isEmpty();
    }

    /**
     * Positive case for the Java-source scanner: a real string literal
     * containing {@code CREATE EXTENSION} must flag when the file is not on
     * the Java allowlist.
     */
    @Test
    void javaStringLiteral_withCreateExtension_flags() {
        String synthetic =
            "class Synthetic {\n"
            + "    void bootstrap() throws Exception {\n"
            + "        su.createStatement().execute(\"CREATE EXTENSION IF NOT EXISTS hstore\");\n"
            + "    }\n"
            + "}\n";

        assertThat(scanJavaForCreateExtension("Synthetic.java", synthetic))
            .as("a CREATE EXTENSION string literal outside the Java allowlist must be flagged")
            .hasSize(1);
    }

    /**
     * Comment-prose evasion for the Java scanner: {@code CREATE EXTENSION}
     * mentioned only in a {@code //} line comment or a javadoc block comment
     * (like this very class's own javadoc) must NOT flag.
     */
    @Test
    void javaCommentProseMentioningCreateExtension_doesNotFlag() {
        String synthetic =
            "class Synthetic {\n"
            + "    // PREREQUISITE: CREATE EXTENSION vector runs elsewhere, not here.\n"
            + "    /**\n"
            + "     * Also mentions CREATE EXTENSION in a javadoc block, same as this\n"
            + "     * class's own javadoc above.\n"
            + "     */\n"
            + "    void bootstrap() {\n"
            + "        // nothing here actually calls CREATE EXTENSION\n"
            + "    }\n"
            + "}\n";

        assertThat(scanJavaForCreateExtension("Synthetic.java", synthetic))
            .as("CREATE EXTENSION mentioned only in Java comment prose must not false-positive")
            .isEmpty();
    }

    /**
     * Non-vacuity check for the Java-source allowlist, mirroring {@link
     * #sanctionedFile_actuallyContainsCreateExtension} for the XML side:
     * every entry in {@link #SANCTIONED_JAVA_FILES} must actually exist and
     * actually contain a real CREATE EXTENSION string literal, or the
     * allowlist entry is stale.
     */
    @Test
    void everySanctionedJavaFile_actuallyExistsAndContainsCreateExtension() throws IOException {
        // Searched by walking src/test/java rather than assuming a flat
        // dev/nexus/service/ layout: most SANCTIONED_JAVA_FILES entries live
        // there directly, but PgSessionEfSearchReadbackIntegrationTest.java
        // lives one level deeper, under dev/nexus/service/db/.
        Path testRoot = Path.of("src", "test", "java");
        for (String filename : SANCTIONED_JAVA_FILES) {
            Path found;
            try (var walk = Files.walk(testRoot)) {
                found = walk.filter(p -> p.getFileName().toString().equals(filename))
                    .findFirst()
                    .orElse(null);
            }
            assertThat(found)
                .as(filename + " must exist somewhere under src/test/java — the allowlist "
                    + "names a real file")
                .isNotNull();
            assertThat(scanJavaForCreateExtension(filename, Files.readString(found)))
                .as(filename + " must itself contain a real CREATE EXTENSION string literal "
                    + "— otherwise the allowlist entry is stale")
                .isNotEmpty();
        }
    }

    /**
     * Companion positive case: a real {@code <sql>} body containing {@code
     * CREATE EXTENSION} in a file NOT on the allowlist must still flag.
     */
    @Test
    void realSqlBody_withCreateExtension_flags() {
        List<String> synthetic = List.of(
            "<changeSet id=\"synthetic-1\" author=\"test\">",
            "    <sql splitStatements=\"false\">",
            "CREATE EXTENSION IF NOT EXISTS hstore;",
            "    </sql>",
            "</changeSet>");

        assertThat(scanForCreateExtension("synthetic.xml", synthetic))
            .as("a CREATE EXTENSION in a real <sql> body, outside the sanctioned-file "
                + "allowlist, must be flagged")
            .hasSize(1);
    }

    /**
     * Comment-prose evasion: {@code CREATE EXTENSION} mentioned in a {@code
     * <comment>} block (discussing a prerequisite, a known-absent extension,
     * etc.) must NOT flag — matching {@link
     * ChangelogBulkGrantConformanceTest#sqlCheckPrecondition_doesNotLeakIntoSubsequentCommentProse}'s
     * precedent. Several real changelog files (taxonomy-002-centroids.xml,
     * catalog-017-fts-diacritic-folding.xml, memory-002-fts-separator-
     * tokens.xml) mention CREATE EXTENSION in prose this way.
     */
    @Test
    void commentProseMentioningCreateExtension_doesNotFlag() {
        List<String> synthetic = List.of(
            "<changeSet id=\"synthetic-2\" author=\"test\">",
            "    <comment>",
            "        PREREQUISITE: the `vector` extension (CREATE EXTENSION vector) is",
            "        provided by vectors-001-baseline.xml, not here.",
            "    </comment>",
            "    <sql splitStatements=\"false\">",
            "SELECT 1;",
            "    </sql>",
            "</changeSet>");

        assertThat(scanForCreateExtension("synthetic.xml", synthetic))
            .as("CREATE EXTENSION mentioned only in comment prose must not false-positive")
            .isEmpty();
    }

    /**
     * Case evasion: PostgreSQL keywords are case-insensitive — a lowercase
     * {@code create extension} must still be flagged.
     */
    @Test
    void lowercaseCreateExtension_flags() {
        List<String> synthetic = List.of(
            "<changeSet id=\"synthetic-3\" author=\"test\">",
            "    <sql splitStatements=\"false\">",
            "create extension if not exists hstore;",
            "    </sql>",
            "</changeSet>");

        assertThat(scanForCreateExtension("synthetic.xml", synthetic))
            .as("lowercase create extension must still be flagged (PG keywords are "
                + "case-insensitive)")
            .hasSize(1);
    }

    /**
     * The sanctioned site itself must actually exist and actually carry the
     * statement — a non-vacuity check so this test class cannot silently
     * pass by having nothing left to allowlist.
     */
    @Test
    void sanctionedFile_actuallyContainsCreateExtension() throws IOException, URISyntaxException {
        Path vectors001 = changelogResourceDir().resolve("vectors-001-baseline.xml");
        assertThat(Files.exists(vectors001))
            .as("vectors-001-baseline.xml must exist — the allowlist names a real file")
            .isTrue();
        assertThat(scanForCreateExtension("vectors-001-baseline.xml", Files.readAllLines(vectors001)))
            .as("vectors-001-baseline.xml must itself contain a real CREATE EXTENSION "
                + "statement — otherwise the allowlist entry is stale")
            .isNotEmpty();
    }

    // ── Scan roots + scanner ─────────────────────────────────────────────────

    private static Path changelogResourceDir() throws URISyntaxException {
        URL url = ExtensionBootstrapSiteConformanceTest.class.getResource("/db/changelog");
        assertThat(url).as("db/changelog must be on the test classpath").isNotNull();
        return Paths.get(url.toURI());
    }

    private static Path changelogTestResourceDir() throws URISyntaxException {
        URL url = ExtensionBootstrapSiteConformanceTest.class.getResource("/db/changelog-test");
        assertThat(url).as("db/changelog-test must be on the test classpath").isNotNull();
        return Paths.get(url.toURI());
    }

    private static List<Path> changelogScanRoots() throws URISyntaxException {
        return List.of(changelogResourceDir(), changelogTestResourceDir());
    }

    /**
     * Scans {@code <sql>...</sql>} BODIES (not individual lines, and never
     * {@code <comment>} prose) for {@code CREATE EXTENSION}, mirroring
     * {@link ChangelogBulkGrantConformanceTest#scanForUnguardedBulkGrants}'s
     * inSql state-tracking exactly, for the identical false-positive-
     * avoidance reasons documented there.
     */
    private static List<String> scanForCreateExtension(String filename, List<String> lines) {
        List<String> violations = new ArrayList<>();
        boolean inSql = false;
        int blockStartLine = -1;
        StringBuilder body = new StringBuilder();

        for (int i = 0; i < lines.size(); i++) {
            String line = lines.get(i);
            boolean opensHere = OPEN_SQL_TAG.matcher(line).find();
            boolean closesHere = line.contains("</sql>");

            if (opensHere && !inSql) {
                inSql = true;
                blockStartLine = i + 1;
                body.setLength(0);
                continue; // the tag line itself is not SQL content
            }
            if (!inSql) {
                continue;
            }
            if (closesHere) {
                String collapsed = body.toString().replaceAll("\\s+", " ").trim();
                if (CREATE_EXTENSION_PATTERN.matcher(collapsed).find()) {
                    violations.add(filename + " (<sql> block starting line " + blockStartLine
                        + "): CREATE EXTENSION outside the sanctioned-bootstrap-site "
                        + "allowlist.");
                }
                inSql = false;
                body.setLength(0);
                continue;
            }
            body.append(line).append(' ');
        }
        return violations;
    }

    /**
     * Scans Java SOURCE (not the XML changelog) for a {@code CREATE
     * EXTENSION} string literal, after stripping {@code //} line comments and
     * {@code /* ... * /} block comments (javadoc included — both use the
     * same delimiter). Deliberately simple relative to {@link
     * dev.nexus.service.db.RawSqlGateTest}'s own full statement-aware
     * scanner: this gate only needs to know WHETHER the phrase appears in
     * real code, not extract or canonicalize the statement, so a
     * comment-stripped substring search is sufficient and avoids
     * reimplementing that class's machinery for a narrower question.
     */
    private static List<String> scanJavaForCreateExtension(String filename, String source) {
        List<String> violations = new ArrayList<>();
        StringBuilder stripped = new StringBuilder(source.length());
        boolean inBlockComment = false;
        for (int i = 0; i < source.length(); i++) {
            char c = source.charAt(i);
            if (inBlockComment) {
                if (c == '*' && i + 1 < source.length() && source.charAt(i + 1) == '/') {
                    inBlockComment = false;
                    i++;
                }
                continue;
            }
            if (c == '/' && i + 1 < source.length() && source.charAt(i + 1) == '*') {
                inBlockComment = true;
                i++;
                continue;
            }
            if (c == '/' && i + 1 < source.length() && source.charAt(i + 1) == '/') {
                // Skip to end of line — the newline itself is preserved so
                // downstream line-number reasoning (not needed here, but kept
                // for parity with the XML scanner's shape) stays sane.
                while (i < source.length() && source.charAt(i) != '\n') {
                    i++;
                }
                stripped.append('\n');
                continue;
            }
            stripped.append(c);
        }

        if (CREATE_EXTENSION_PATTERN.matcher(stripped).find()) {
            violations.add(filename + ": CREATE EXTENSION string literal outside the "
                + "SANCTIONED_JAVA_FILES allowlist.");
        }
        return violations;
    }
}
