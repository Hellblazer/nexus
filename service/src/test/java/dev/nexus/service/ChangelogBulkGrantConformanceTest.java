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
 * Class-level tripwire for the bulk-GRANT/REVOKE ownership hazard
 * (nexus-46yy3 class): {@code ON ALL TABLES}, {@code ON ALL SEQUENCES}, and
 * {@code ON ALL FUNCTIONS} hard-error on any relation the acting role does
 * not own, aborting the whole changeset. This is the THIRD grants/ownership
 * incident in this subsystem (see grants-nexus-diag-2 / nexus-46yy3, and
 * grants-nexus-svc-1 / GH #1402, nexus-0gis0) — every future occurrence must
 * be either owner-restricted per-relation iteration or explicitly justified
 * and allowlisted here.
 *
 * <p>{@code ALTER DEFAULT PRIVILEGES ... ON TABLES/SEQUENCES} statements are
 * a different, safe construct (they configure future grants, not an
 * immediate bulk operation across existing relations) and are exempted
 * per-STATEMENT (not per-body — a {@code <sql>} block containing both a safe
 * {@code ALTER DEFAULT PRIVILEGES} statement and an unguarded bulk grant
 * elsewhere must still flag the latter).
 */
class ChangelogBulkGrantConformanceTest {

    private static final Pattern BULK_GRANT_PATTERN =
        Pattern.compile("\\bON ALL (TABLES|SEQUENCES|FUNCTIONS)\\b", Pattern.CASE_INSENSITIVE);

    private static final Pattern ALTER_DEFAULT_PRIVILEGES_PATTERN =
        Pattern.compile("ALTER DEFAULT PRIVILEGES", Pattern.CASE_INSENSITIVE);

    /**
     * Matches the opening {@code <sql} / {@code <sql ...>} tag ONLY — a
     * trailing whitespace or {@code >} boundary excludes sibling Liquibase
     * tags that merely share the "sql" prefix: {@code <sqlCheck>} (used by
     * {@code <preConditions>} in grants-nexus-diag.xml and the
     * fk-00[23]-validate.xml / catalog-013 changesets) and {@code <sqlFile
     * .../>} (zero uses today — see
     * {@link #noSqlFileIncludesPresent_untilScannerReadsThem()}). Neither has
     * a whitespace-or-{@code >} character immediately after "sql" ("C" / "F"
     * respectively), so this pattern never opens an SQL-body region for
     * them. Without this boundary a precondition's {@code <sqlCheck>} would
     * flip {@code inSql} true and — because {@code </sqlCheck>} does not
     * match the closing check either — leak that state through subsequent
     * {@code <comment>} prose until the file's next real {@code </sql>},
     * causing false positives on comment text that merely discusses bulk
     * grants (review finding, GH #1402 follow-up).
     */
    private static final Pattern OPEN_SQL_TAG = Pattern.compile("<sql[\\s>]");

    /** Matches a {@code <sqlFile>} include tag, self-closing or not. */
    private static final Pattern SQL_FILE_TAG = Pattern.compile("<sqlFile[\\s>/]");

    /**
     * (filename, changeset id) pairs allowed to keep a bulk
     * GRANT/REVOKE-ON-ALL statement. Each entry must be independently
     * verified — see the per-entry rationale below — not merely present.
     */
    private static final Set<String> ALLOWLISTED_FILES = Set.of("grants-nexus-diag.xml");

    @Test
    void noUnguardedBulkGrantOutsideAllowlist() throws IOException, URISyntaxException {
        Path changelogDir = changelogResourceDir();
        List<String> violations = new ArrayList<>();

        try (var walk = Files.walk(changelogDir)) {
            for (Path file : walk.filter(p -> p.toString().endsWith(".xml")).toList()) {
                String filename = file.getFileName().toString();
                violations.addAll(
                    scanForUnguardedBulkGrants(filename, Files.readAllLines(file)));
            }
        }

        assertThat(violations).as("unguarded bulk GRANT/REVOKE statements").isEmpty();
    }

    /**
     * Cheapest sound posture for {@code <sqlFile>} includes (review finding,
     * GH #1402 follow-up): this scanner reads .xml source text directly and
     * has no mechanism to resolve or read a {@code <sqlFile path="..."/>}
     * target, so any SQL hidden behind such an include is invisible to it.
     * Rather than silently under-scanning, the MERE PRESENCE of a {@code
     * <sqlFile>} tag anywhere under {@code db/changelog/} fails this test
     * until the scanner is extended to read sqlFile targets. There are zero
     * uses today; this assertion keeps it that way (or forces a scanner
     * upgrade the day a real use appears).
     */
    @Test
    void noSqlFileIncludesPresent_untilScannerReadsThem() throws IOException, URISyntaxException {
        Path changelogDir = changelogResourceDir();
        List<String> offenders = new ArrayList<>();

        try (var walk = Files.walk(changelogDir)) {
            for (Path file : walk.filter(p -> p.toString().endsWith(".xml")).toList()) {
                offenders.addAll(
                    scanForSqlFileIncludes(file.getFileName().toString(),
                        Files.readAllLines(file)));
            }
        }

        assertThat(offenders)
            .as("a <sqlFile> include appeared under db/changelog/ — extend "
                + "ChangelogBulkGrantConformanceTest to read sqlFile targets "
                + "before using them; it is currently blind to SQL hidden "
                + "behind sqlFile includes")
            .isEmpty();
    }

    /**
     * Review regression (GH #1402 follow-up): a {@code <sqlCheck>}
     * precondition must not leak {@code inSql} state into subsequent
     * {@code <comment>} prose. Synthetic changeset: a sqlCheck precondition,
     * then a comment mentioning "ON ALL TABLES" prose, then a real
     * {@code <sql>} body with no bulk grant. Must NOT flag.
     */
    @Test
    void sqlCheckPrecondition_doesNotLeakIntoSubsequentCommentProse() {
        List<String> synthetic = List.of(
            "<changeSet id=\"synthetic-1\" author=\"test\">",
            "    <preConditions onFail=\"MARK_RAN\">",
            "        <sqlCheck expectedResult=\"0\">",
            "            SELECT count(*) FROM pg_class WHERE relname = 'x'",
            "        </sqlCheck>",
            "    </preConditions>",
            "    <comment>",
            "        Unlike grants-nexus-svc-1's old form, this changeset never uses",
            "        GRANT ... ON ALL TABLES IN SCHEMA — that prose is discussion only.",
            "    </comment>",
            "    <sql splitStatements=\"false\">",
            "GRANT USAGE ON SCHEMA nexus TO nexus_svc;",
            "    </sql>",
            "</changeSet>");

        assertThat(scanForUnguardedBulkGrants("synthetic.xml", synthetic))
            .as("sqlCheck precondition + comment prose must not false-positive")
            .isEmpty();
    }

    /**
     * Companion positive case: a real {@code <sql>} body containing an
     * unguarded bulk grant, in a file NOT on the allowlist, must still flag.
     */
    @Test
    void realSqlBody_withUnguardedBulkGrant_flags() {
        List<String> synthetic = List.of(
            "<changeSet id=\"synthetic-2\" author=\"test\">",
            "    <sql splitStatements=\"false\">",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO nexus_svc;",
            "    </sql>",
            "</changeSet>");

        assertThat(scanForUnguardedBulkGrants("synthetic.xml", synthetic))
            .as("an unguarded bulk grant in a real <sql> body must be flagged")
            .hasSize(1);
    }

    /**
     * Case evasion (review finding): PostgreSQL keywords are
     * case-insensitive — a lowercase bulk grant must still be flagged.
     */
    @Test
    void lowercaseBulkGrant_flags() {
        List<String> synthetic = List.of(
            "<changeSet id=\"synthetic-3\" author=\"test\">",
            "    <sql splitStatements=\"false\">",
            "grant select, insert, update, delete on all tables in schema nexus to nexus_svc;",
            "    </sql>",
            "</changeSet>");

        assertThat(scanForUnguardedBulkGrants("synthetic.xml", synthetic))
            .as("lowercase bulk grant must still be flagged (PG keywords are case-insensitive)")
            .hasSize(1);
    }

    /**
     * Line-split evasion (review finding): a newline between "ON ALL" and
     * "TABLES" must not slip a per-line matcher. The scanner accumulates
     * each {@code <sql>} body into a single whitespace-collapsed string
     * before matching.
     */
    @Test
    void reflowedMultilineBulkGrant_flags() {
        List<String> synthetic = List.of(
            "<changeSet id=\"synthetic-4\" author=\"test\">",
            "    <sql splitStatements=\"false\">",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL",
            "TABLES IN SCHEMA nexus TO nexus_svc;",
            "    </sql>",
            "</changeSet>");

        assertThat(scanForUnguardedBulkGrants("synthetic.xml", synthetic))
            .as("a bulk grant statement split across lines must still be flagged")
            .hasSize(1);
    }

    /**
     * Companion to {@link #noSqlFileIncludesPresent_untilScannerReadsThem()}:
     * confirms the detector itself catches a synthetic {@code <sqlFile>} tag
     * regardless of attribute order or self-closing form.
     */
    @Test
    void sqlFilePresence_synthetic_isDetected() {
        List<String> synthetic = List.of(
            "<changeSet id=\"synthetic-5\" author=\"test\">",
            "    <sqlFile path=\"db/changelog/sql/some-grant.sql\" splitStatements=\"false\"/>",
            "</changeSet>");

        assertThat(scanForSqlFileIncludes("synthetic.xml", synthetic))
            .as("a <sqlFile> include must be detected")
            .hasSize(1);
    }

    /**
     * The one allowlisted occurrence (grants-nexus-diag.xml, changeset
     * grants-nexus-diag-1) must still actually be gated by the view-absence
     * preCondition that makes it safe — a view-owned-by-superuser-once-it-
     * exists hazard the same as #1402, but here avoided by never running the
     * bulk form once the view is present. If that gate is ever removed, this
     * assertion (not just the presence of the filename in the allowlist)
     * must fail.
     */
    @Test
    void allowlistedOccurrence_isStillGatedByViewAbsencePrecondition() throws IOException,
            URISyntaxException {
        Path file = changelogResourceDir().resolve("grants-nexus-diag.xml");
        String content = Files.readString(file);

        int changesetIdx = content.indexOf("id=\"grants-nexus-diag-1\"");
        assertThat(changesetIdx).as("grants-nexus-diag-1 changeset must exist").isNotNegative();

        int nextChangesetIdx = content.indexOf("<changeSet", changesetIdx + 1);
        String changesetBody = nextChangesetIdx > 0
            ? content.substring(changesetIdx, nextChangesetIdx)
            : content.substring(changesetIdx);

        assertThat(changesetBody)
            .as("grants-nexus-diag-1 must remain gated on the diag view's absence")
            .contains("<preConditions")
            .contains("expectedResult=\"0\"")
            .contains("diag_chash_conformance");
        assertThat(changesetBody)
            .as("bulk ON ALL TABLES must still be present in the gated changeset")
            .containsPattern(BULK_GRANT_PATTERN);
    }

    private static Path changelogResourceDir() throws URISyntaxException {
        URL url = ChangelogBulkGrantConformanceTest.class.getResource("/db/changelog");
        assertThat(url).as("db/changelog must be on the test classpath").isNotNull();
        return Paths.get(url.toURI());
    }

    /**
     * Scans {@code <sql>...</sql>} BODIES (not individual lines) for
     * unguarded bulk GRANT/REVOKE-ON-ALL statements. Each body is
     * accumulated across lines, whitespace-collapsed, and split into
     * individual {@code ;}-delimited statements so that (a) a statement
     * split across multiple lines is still detected as one unit
     * (line-split evasion) and (b) the {@code ALTER DEFAULT PRIVILEGES}
     * exemption applies per-STATEMENT, not per-body.
     */
    private static List<String> scanForUnguardedBulkGrants(String filename, List<String> lines) {
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
                violations.addAll(scanSqlBody(filename, blockStartLine, body.toString()));
                inSql = false;
                body.setLength(0);
                continue;
            }
            body.append(line).append(' ');
        }
        return violations;
    }

    private static List<String> scanSqlBody(String filename, int startLine, String rawBody) {
        List<String> violations = new ArrayList<>();
        String collapsed = rawBody.replaceAll("\\s+", " ").trim();
        for (String rawStatement : collapsed.split(";")) {
            String statement = rawStatement.trim();
            if (statement.isEmpty()) {
                continue;
            }
            if (!BULK_GRANT_PATTERN.matcher(statement).find()) {
                continue;
            }
            if (ALTER_DEFAULT_PRIVILEGES_PATTERN.matcher(statement).find()) {
                continue;
            }
            if (ALLOWLISTED_FILES.contains(filename)) {
                continue;
            }
            violations.add(filename + " (<sql> block starting line " + startLine + "): "
                + statement + " — unguarded bulk GRANT/REVOKE ON ALL. This is the "
                + "nexus-46yy3 / GH #1402 hazard class: bulk GRANT/REVOKE "
                + "hard-errors on any relation the acting role does not "
                + "own. Use per-relation, owner-restricted iteration "
                + "(see grants-nexus-diag.xml changeset grants-nexus-diag-2 "
                + "or grants-nexus-svc.xml changeset grants-nexus-svc-1), "
                + "or add an explicitly-justified allowlist entry here.");
        }
        return violations;
    }

    private static List<String> scanForSqlFileIncludes(String filename, List<String> lines) {
        List<String> offenders = new ArrayList<>();
        for (int i = 0; i < lines.size(); i++) {
            if (SQL_FILE_TAG.matcher(lines.get(i)).find()) {
                offenders.add(filename + ":" + (i + 1));
            }
        }
        return offenders;
    }
}
