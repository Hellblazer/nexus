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
import java.util.Optional;
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
     * .../>} (its target is read and scanned separately — see
     * {@link #everySqlFileTargetIsReadAndScanned()}). Neither has
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

    /** Matches a whole {@code <sqlFile ...>} include tag (attributes may span lines), self-closing or not. */
    private static final Pattern SQL_FILE_TAG = Pattern.compile("<sqlFile[\\s/>][^>]*>");

    private static final Pattern PATH_ATTR = Pattern.compile("\\bpath\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)')");

    private static final Pattern RELATIVE_ATTR =
        Pattern.compile("\\brelativeToChangelogFile\\s*=\\s*(?:\"true\"|'true')", Pattern.CASE_INSENSITIVE);

    /** Where a {@code <sqlFile>} target is read from: the text, or empty when it cannot be found. */
    @FunctionalInterface
    interface SqlFileResolver {
        Optional<String> read(String path, boolean relativeToChangelogFile);
    }

    /**
     * (filename, changeset id) pairs allowed to keep a bulk
     * GRANT/REVOKE-ON-ALL statement. Each entry must be independently
     * verified — see the per-entry rationale below — not merely present.
     *
     * <p><b>db.changelog-test-role.xml</b> (nexus-cbo4a batch 1a review follow-up,
     * T2 critique-cbo4a-batch1a-2026-09-04 [24371]): carries three bulk grants
     * — {@code SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus},
     * {@code USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus}, and {@code SELECT,
     * INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA staging} — to the
     * per-test throwaway {@code ${svcRole}}. Granting a test role broad DML
     * across every table it will touch is the entire point of this file
     * (PgContainerHelper#bootstrapServiceRole's javadoc); the file is NOT a
     * candidate for grants-nexus-svc.xml's per-relation owner-restricted
     * iteration pattern, since that pattern exists specifically to survive a
     * FOREIGN-owned relation (e.g. a superuser-owned diagnostic view) —
     * something the test schemas (created wholesale, moments earlier in the
     * SAME @BeforeAll, by the same nexus_admin migration role that runs
     * db.changelog-master.xml) never have. Every relation under nexus/staging
     * at test-bootstrap time is owned by nexus_admin, so the GH #1402 /
     * nexus-46yy3 hazard this allowlist otherwise guards against cannot occur
     * here — verified, not merely asserted by the file's presence in this set.
     *
     * <p><b>db.changelog-test-admin-role.xml</b> (nexus-4a8pn, taxonomy-017's
     * case (f)/(h) fixture bootstrap): the IDENTICAL shape and IDENTICAL
     * verified reasoning as db.changelog-test-role.xml above — a per-test
     * throwaway role (this one modeling nexus_admin's NOSUPERUSER/NOBYPASSRLS
     * posture) granted broad DML across nexus/staging, applied in the SAME
     * {@code @BeforeAll} immediately after {@code applyProductSchema}, before
     * this file's own trailing {@code ALTER TABLE nexus.topic_assignments
     * OWNER TO ${adminRole}} statement runs. At the moment the bulk GRANTs
     * execute every relation under nexus/staging is still owned by the
     * container superuser that just ran the product changelog (same as
     * db.changelog-test-role.xml's case) — the GH #1402/nexus-46yy3
     * foreign-ownership hazard this allowlist otherwise guards against cannot
     * occur here either. The LATER ownership transfers (topic_assignments, and
     * since RDR-207 / nexus-l3yuc.1 memory and memory_summaries too) are
     * targeted {@code ALTER TABLE ... OWNER TO} statements, not bulk grants,
     * and are outside this scanner's {@code ON ALL} pattern entirely.
     */
    private static final Set<String> ALLOWLISTED_FILES =
        Set.of("grants-nexus-diag.xml", "db.changelog-test-role.xml",
               "db.changelog-test-admin-role.xml");

    @Test
    void noUnguardedBulkGrantOutsideAllowlist() throws IOException, URISyntaxException {
        List<String> violations = new ArrayList<>();

        for (Path root : changelogScanRoots()) {
            try (var walk = Files.walk(root)) {
                for (Path file : walk.filter(p -> p.toString().endsWith(".xml")).toList()) {
                    String filename = file.getFileName().toString();
                    violations.addAll(
                        scanForUnguardedBulkGrants(filename, Files.readAllLines(file)));
                }
            }
        }

        assertThat(violations).as("unguarded bulk GRANT/REVOKE statements").isEmpty();
    }

    /**
     * The scanner reads the target of every {@code <sqlFile>} include and applies the {@code <sql>} body rules to it (it used
     * to fail on the mere presence of one, because it was blind behind the include: RDR-229 P3.1's test changelog
     * {@code db.changelog-test-p31-downstep.xml} is the first use). A target that cannot be found is a violation, never a
     * skip: a scan that cannot read what it is told to read has examined nothing.
     *
     * <p>Non-vacuity: the include the scan is known to meet must be seen and read, so a walk that finds no
     * {@code <sqlFile>}, or reads an empty target, fails here instead of passing over nothing.
     */
    @Test
    void everySqlFileTargetIsReadAndScanned() throws IOException, URISyntaxException {
        List<String> violations = new ArrayList<>();
        int includes = 0;
        for (Path root : changelogScanRoots()) {
            try (var walk = Files.walk(root)) {
                for (Path file : walk.filter(p -> p.toString().endsWith(".xml")).toList()) {
                    String xml = Files.readString(file);
                    includes += scanForSqlFileIncludes(file.getFileName().toString(),
                        Files.readAllLines(file)).size();
                    violations.addAll(scanSqlFileTargets(file.getFileName().toString(), xml,
                        (path, relative) -> readFromDisk(file.getParent(), path, relative)));
                }
            }
        }
        assertThat(includes).as("non-vacuity: the walk met the P3.1 down-step's <sqlFile> include").isGreaterThanOrEqualTo(1);
        assertThat(readFromDisk(changelogTestResourceDir(), "db/rdr229/p31-downstep.sql", false))
            .as("non-vacuity: the classpath resolver reads the down-step's SQL, not an empty string")
            .hasValueSatisfying(sql -> assertThat(sql).contains("DROP CONSTRAINT IF EXISTS"));
        assertThat(violations).as("unguarded bulk GRANT/REVOKE statements, or unreadable targets, behind <sqlFile>").isEmpty();
    }

    /** Mutation: a {@code <sqlFile>} target that holds a bulk grant must be flagged, whatever its spelling. */
    @Test
    void sqlFileTarget_withUnguardedBulkGrant_flags() {
        List<String> xml = List.of(
            "<changeSet id=\"synthetic-6\" author=\"test\">",
            "    <sqlFile path=\"db/some-grant.sql\" relativeToChangelogFile=\"false\"/>",
            "</changeSet>");
        SqlFileResolver resolver = (path, relative) -> path.equals("db/some-grant.sql")
            ? Optional.of("-- a comment\nGRANT SELECT ON ALL\n  TABLES IN SCHEMA nexus TO nexus_svc;\n") : Optional.empty();

        assertThat(scanSqlFileTargets("synthetic.xml", String.join("\n", xml), resolver))
            .as("a bulk grant behind a <sqlFile> include (reflowed across lines) must be flagged")
            .hasSize(1);
        assertThat(scanSqlFileTargets("synthetic.xml", String.join("\n", xml),
                (path, relative) -> Optional.of("grant select on all sequences in schema nexus to nexus_svc;")))
            .as("lowercase, and the last statement without a terminating semicolon")
            .hasSize(1);
        assertThat(scanSqlFileTargets("grants-nexus-diag.xml", String.join("\n", xml), resolver))
            .as("the allowlist is by the including changelog's file name, as for a <sql> body")
            .isEmpty();
    }

    @Test
    void sqlFileTarget_withNoBulkGrant_doesNotFlag_andTheAttributesMayBeSplitAcrossLines() {
        String xml = "<changeSet id=\"synthetic-7\" author=\"test\">\n"
            + "    <sqlFile\n        splitStatements=\"true\"\n        path='down.sql'\n"
            + "        relativeToChangelogFile=\"true\"/>\n</changeSet>";
        List<String> asked = new ArrayList<>();
        SqlFileResolver resolver = (path, relative) -> {
            asked.add(path + ":" + relative);
            return Optional.of("ALTER TABLE nexus.t DROP CONSTRAINT IF EXISTS c;\n"
                + "ALTER DEFAULT PRIVILEGES IN SCHEMA nexus GRANT SELECT ON TABLES TO nexus_svc;");
        };

        assertThat(scanSqlFileTargets("synthetic.xml", xml, resolver)).isEmpty();
        assertThat(asked).as("the target is resolved by the tag's own path and relativeToChangelogFile")
            .containsExactly("down.sql:true");
    }

    /** A target the scan cannot read is a violation: the scan would otherwise pass over SQL it never saw. */
    @Test
    void sqlFileTarget_thatCannotBeRead_flags() {
        String xml = "<sqlFile path=\"db/missing.sql\"/>\n<sqlFile relativeToChangelogFile=\"true\"/>";

        assertThat(scanSqlFileTargets("synthetic.xml", xml, (path, relative) -> Optional.empty()))
            .as("an unresolvable target, and a tag with no path at all")
            .hasSize(2);
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
     * Companion to {@link #everySqlFileTargetIsReadAndScanned()}: confirms the
     * detector itself counts a synthetic {@code <sqlFile>} tag regardless of
     * attribute order or self-closing form.
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
     * grants-nexus-diag-1) must still actually be gated on the view's ABSENCE
     * — a view-owned-by-superuser-once-it-exists hazard the same as #1402, but
     * here avoided by never running the bulk form once the view is present. If
     * that gate is ever removed, this assertion (not just the presence of the
     * filename in the allowlist) must fail.
     *
     * <p><strong>The gate MOVED, it was not removed (nexus-ixsxa).</strong> It
     * was a whole-changeset {@code <preConditions expectedResult="0">}; it is
     * now an early {@code RETURN} guard inside the {@code DO $$} body. A
     * precondition on a {@code runAlways} changeset resolves
     * {@code onFail=MARK_RAN}, which Liquibase records by INSERTING a
     * DATABASECHANGELOG row — one per boot, without bound. The safety property
     * is identical, and the guard is now evaluated in the same transaction as
     * the grants it gates. ORDER is asserted explicitly here, because a guard
     * sitting after the bulk grant would gate nothing at all.
     */
    @Test
    void allowlistedOccurrence_isStillGatedByViewAbsence() throws IOException,
            URISyntaxException {
        Path file = changelogResourceDir().resolve("grants-nexus-diag.xml");
        String content = Files.readString(file);

        int changesetIdx = content.indexOf("id=\"grants-nexus-diag-1\"");
        assertThat(changesetIdx).as("grants-nexus-diag-1 changeset must exist").isNotNegative();

        int nextChangesetIdx = content.indexOf("<changeSet", changesetIdx + 1);
        String changesetBody = nextChangesetIdx > 0
            ? content.substring(changesetIdx, nextChangesetIdx)
            : content.substring(changesetIdx);

        java.util.regex.Matcher bulk = BULK_GRANT_PATTERN.matcher(changesetBody);
        assertThat(bulk.find())
            .as("bulk ON ALL TABLES must still be present in the gated changeset — "
                + "without it this test guards nothing")
            .isTrue();

        // "IF EXISTS (" deliberately does not match the role guard's
        // "IF NOT EXISTS (" that precedes it.
        int guardIdx = changesetBody.indexOf("IF EXISTS (");
        assertThat(guardIdx)
            .as("grants-nexus-diag-1 must remain gated on the diag view's absence: an "
                + "IF EXISTS(...diag_chash_conformance...) THEN RETURN guard in the body")
            .isNotNegative();
        assertThat(changesetBody.substring(guardIdx))
            .as("the era guard must test for the diag view and RETURN before granting")
            .contains("diag_chash_conformance")
            .contains("RETURN;");
        assertThat(guardIdx)
            .as("the era guard must PRECEDE the bulk grant — a guard after it gates nothing")
            .isLessThan(bulk.start());
    }

    private static Path changelogResourceDir() throws URISyntaxException {
        URL url = ChangelogBulkGrantConformanceTest.class.getResource("/db/changelog");
        assertThat(url).as("db/changelog must be on the test classpath").isNotNull();
        return Paths.get(url.toURI());
    }

    /**
     * {@code db/changelog-test/} — the test-role bootstrap changelog's own
     * resource root (nexus-cbo4a batch 1a), deliberately a SIBLING of {@code
     * db/changelog/} rather than nested inside it (see
     * db.changelog-test-role.xml's own header comment for why: nesting it
     * under {@code db/changelog/} risked Maven's test-classpath ordering
     * shadowing the real changelog directory for {@link #changelogResourceDir}'s
     * single {@code getResource("/db/changelog")} call). Being a sibling is
     * exactly why it needs its OWN root here — {@link #changelogResourceDir}'s
     * walk never reaches it.
     */
    private static Path changelogTestResourceDir() throws URISyntaxException {
        URL url = ChangelogBulkGrantConformanceTest.class.getResource("/db/changelog-test");
        assertThat(url).as("db/changelog-test must be on the test classpath").isNotNull();
        return Paths.get(url.toURI());
    }

    /**
     * Both changelog resource roots this class's bulk-grant and sqlFile scans
     * walk (nexus-cbo4a batch 1a review follow-up, T2 critique-cbo4a-batch1a-
     * 2026-09-04 [24371]): {@code db/changelog/} (the product master changelog
     * and its includes) and {@code db/changelog-test/} (the test-role bootstrap
     * changelog). {@link #allowlistedOccurrence_isStillGatedByViewAbsence}
     * deliberately keeps using {@link #changelogResourceDir} directly — it
     * targets one named file that lives under {@code db/changelog/} only.
     */
    private static List<Path> changelogScanRoots() throws URISyntaxException {
        return List.of(changelogResourceDir(), changelogTestResourceDir());
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
        String text = String.join("\n", lines);
        var tags = SQL_FILE_TAG.matcher(text);
        while (tags.find()) {   // a tag whose attributes span lines is one include
            offenders.add(filename + ":" + (1 + (int) text.substring(0, tags.start()).chars().filter(c -> c == '\n').count()));
        }
        return offenders;
    }

    /**
     * Reads the SQL behind each {@code <sqlFile>} tag of {@code xml} and scans it as a {@code <sql>} body is scanned: per
     * statement, whitespace-collapsed, {@code ALTER DEFAULT PRIVILEGES} exempt, the allowlist keyed by the including
     * changelog's name. SQL comments are not stripped (as in a {@code <sql>} body), so the scan can only be stricter than
     * the database. A tag with no {@code path}, and a target the resolver cannot read, are violations.
     */
    private static List<String> scanSqlFileTargets(String filename, String xml, SqlFileResolver resolver) {
        List<String> violations = new ArrayList<>();
        var tags = SQL_FILE_TAG.matcher(xml);
        while (tags.find()) {
            String tag = tags.group();
            int line = 1 + (int) xml.substring(0, tags.start()).chars().filter(c -> c == '\n').count();
            var path = PATH_ATTR.matcher(tag);
            if (!path.find()) {
                violations.add(filename + ":" + line + ": a <sqlFile> tag with no path attribute; its SQL cannot be scanned");
                continue;
            }
            String target = path.group(1) != null ? path.group(1) : path.group(2);
            Optional<String> sql = resolver.read(target, RELATIVE_ATTR.matcher(tag).find());
            if (sql.isEmpty()) {
                violations.add(filename + ":" + line + ": the <sqlFile> target " + target
                    + " cannot be read, so the scan cannot see its SQL; fix the path or the resolver");
                continue;
            }
            violations.addAll(scanSqlBody(filename + " -> " + target, 1, sql.get()).stream()
                .filter(v -> !ALLOWLISTED_FILES.contains(filename)).toList());
        }
        return violations;
    }

    /** A target relative to the changelog's directory, or a classpath path (main resources first, then test resources). */
    private static Optional<String> readFromDisk(Path changelogDir, String path, boolean relativeToChangelogFile) {
        try {
            if (relativeToChangelogFile) {
                Path p = changelogDir.resolve(path);
                return Files.isRegularFile(p) ? Optional.of(Files.readString(p)) : Optional.empty();
            }
            for (String root : List.of("src/main/resources/", "src/test/resources/")) {
                Path p = Path.of(root + path);
                if (Files.isRegularFile(p)) {
                    return Optional.of(Files.readString(p));
                }
            }
            return Optional.empty();
        } catch (IOException e) {
            return Optional.empty();
        }
    }
}
