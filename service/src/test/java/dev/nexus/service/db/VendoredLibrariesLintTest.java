// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * nexus-f3yqd.2.11: the vendored ddl_utils and dml_utils changelogs touch nothing of nexus's.
 *
 * <p>{@link DmlUtilsInstaller} walks them after it decided what to do about the index builders, with that decision
 * keyed on nexus's own changesets only, and {@link SchemaMigrator} says why that is safe: the library changesets
 * create objects in the libraries' own schemas and take no lock on any {@code nexus} relation. That holds at the
 * pinned revisions. A re-pin that adds a reference to a nexus relation would break it silently (the builder-survival
 * test uses an idle session), so this lint reads every vendored file and rejects:
 * <ul>
 *   <li>any {@code nexus} identifier, in any case (the schema, the {@code nexus_*} roles and tables);</li>
 *   <li>a name qualified with another application schema of this database ({@code t1}, {@code staging},
 *       {@code nexus_test}), and with {@code public} unless it names a {@code pg_background_*} function, the only
 *       {@code public} objects the libraries use (the extension installs them there).</li>
 * </ul>
 * Anything else the libraries qualify is their own ({@code ddl_utils*}, {@code dml_utils*}, {@code liquibase}) or the
 * system catalog.
 */
class VendoredLibrariesLintTest {

    private static final Path VENDOR = Path.of("src", "main", "resources", "db", "vendor");

    private static final Pattern NEXUS = Pattern.compile("(?i)nexus");
    private static final Pattern OTHER_SCHEMA = Pattern.compile("(?i)\\b(t1|staging|nexus_test)\"?\\s*\\.\\s*\"?[a-z_]");
    private static final Pattern PUBLIC_QUALIFIED = Pattern.compile("(?i)\\bpublic\"?\\s*\\.\\s*\"?([a-z_][a-z0-9_]*)");

    private static final Pattern COMMENTS = Pattern.compile("(?s)<!--.*?-->|/\\*.*?\\*/|--[^\n]*");

    /**
     * The offences in one file's text, each as a short description; empty when the file is clean. Comments are not
     * read: prose may say "public. The" or name a project, and locks come from statements.
     */
    static List<String> offences(String commented) {
        String text = COMMENTS.matcher(commented).replaceAll(" ");
        List<String> found = new ArrayList<>();
        Matcher m = NEXUS.matcher(text);
        while (m.find()) {
            found.add("nexus identifier at " + m.start());
        }
        m = OTHER_SCHEMA.matcher(text);
        while (m.find()) {
            found.add("schema-qualified name " + m.group().trim() + " at " + m.start());
        }
        m = PUBLIC_QUALIFIED.matcher(text);
        while (m.find()) {
            if (!m.group(1).toLowerCase().startsWith("pg_background_")) {
                found.add("public." + m.group(1) + " at " + m.start());
            }
        }
        return found;
    }

    @Test
    void theVendoredLibraries_referenceNothingOfNexus() throws IOException {
        List<String> offenders = new ArrayList<>();
        int scanned = 0;
        int sql = 0;
        try (Stream<Path> files = Files.walk(VENDOR)) {
            for (Path file : files.filter(Files::isRegularFile).toList()) {
                String name = file.getFileName().toString();
                if (name.equals("PIN.manifest") || name.equals("pins.properties")) {
                    continue; // nexus's own bookkeeping about the copy, not library text
                }
                scanned++;
                if (name.endsWith(".sql")) {
                    sql++;
                }
                for (String offence : offences(Files.readString(file, StandardCharsets.UTF_8))) {
                    offenders.add(VENDOR.relativize(file) + ": " + offence);
                }
            }
        }
        assertThat(scanned).as("non-vacuity: vendored files scanned").isGreaterThan(100);
        assertThat(sql).as("non-vacuity: vendored .sql files scanned").isGreaterThan(100);
        assertThat(offenders)
            .as("a vendored library names a nexus object or an application schema; the install's builder-safety argument "
                + "(DmlUtilsInstaller, SchemaMigrator) needs revisiting before this re-pin ships")
            .isEmpty();
    }

    @Test
    void theScan_seesWhatItIsMeantToReject() {
        assertThat(offences("x nexus.chunks y")).isNotEmpty();
        assertThat(offences("IN SCHEMA NEXUS TO r")).isNotEmpty();
        assertThat(offences("from t1.scratch")).hasSize(1);
        assertThat(offences("from staging.x")).hasSize(1);
        assertThat(offences("a public.mine b")).containsExactly("public.mine at 2");
        assertThat(offences("public.pg_background_launch(x)")).isEmpty();
        assertThat(offences("dml_utils.run_chunks liquibase.x pg_catalog.now")).isEmpty();
        // quoted identifiers
        assertThat(offences("a \"nexus\".\"chunks\" b")).isNotEmpty();
        assertThat(offences("a \"public\".\"mine\" b")).hasSize(1);
        assertThat(offences("a \"t1\".\"scratch\" b")).hasSize(1);
        assertThat(offences("\"public\".\"pg_background_launch\"(x)")).isEmpty();
        assertThat(offences("<!-- nexus.chunks and public. The end --> -- nexus.x\n/* t1.x */ ok")).isEmpty();
    }
}
