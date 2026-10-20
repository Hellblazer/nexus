// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Counts the SQL statements written as Java string literals in the test tree, whatever carries them to the
 * database (nexus-cbo4a batch 12). {@link RawSqlGateTest} counts call sites by name ({@code execute(},
 * {@code fetch(}, a local wrapper's declared shape), so a statement held in a constant, built by a helper or handed
 * to an unrecognised sink was invisible to it: the sweep-guard DELETE in CatalogManifestSweepRepositoryTest ran
 * through {@code prepareStatement(CONSTANT)} uncounted. This gate reads the literals instead. A literal (string or
 * text block, comments stripped) is a statement when it opens with an uppercase SQL verb followed by that verb's
 * grammar ({@link #STATEMENT}); prose that opens with the same word ("WITH CHECK must reference", "DELETE trigger",
 * "DO NOTHING") does not match. A statement built from concatenated literals counts once, at its first piece.
 *
 * <p>Two exact per-file maps. {@link #EXECUTED} is SQL the tests send to a database; it drains to zero.
 * {@link #TEXT} is SQL the tests never send: input to a parser or scanner under test, an expected substring of a
 * function body, changeset or error message. It is frozen per file, so a new executed statement in one of those
 * files still fails here. A file absent from both maps must have none.
 */
class SqlStringLiteralGateTest {

    private static final List<String> STATEMENT_GRAMMAR = List.of(
        "SELECT\\s+\\S",
        "INSERT\\s+INTO\\s",
        "UPDATE\\s+[a-z_][\\w.]*+\\s+(?:[a-z]\\w*+\\s+)?SET\\s",
        "DELETE\\s+FROM\\s",
        "WITH\\s+(?:RECURSIVE\\s+)?[a-z_]\\w*+(?:\\s*\\([^)]*+\\))?\\s+AS\\s*(?:(?:NOT\\s+)?MATERIALIZED\\s*)?\\(",
        "(?:CREATE|ALTER|DROP)\\s+(?:OR\\s+REPLACE\\s+|UNIQUE\\s+|TEMP(?:ORARY)?\\s+|MATERIALIZED\\s+)?"
            + "(?:TABLE|INDEX|VIEW|FUNCTION|PROCEDURE|ROLE|USER|SCHEMA|EXTENSION|POLICY|TRIGGER|SEQUENCE|TYPE"
            + "|DATABASE|DEFAULT\\s+PRIVILEGES|PUBLICATION|OWNED)\\s+\\S",
        "(?:GRANT|REVOKE)\\s+[\\w\\x20,]+?\\s+(?:ON|TO|FROM)\\s",
        "SET\\s+(?:LOCAL\\s+|SESSION\\s+)?(?:ROLE\\s|[a-z_][\\w.]*+\\s*(?:=|TO\\s))",
        "RESET\\s+(?:ROLE|ALL|[a-z_][\\w.]*+)\\s*;?\\s*$",
        "(?:EXPLAIN)(?:\\s*\\(|\\s+(?:ANALYZE|VERBOSE|SELECT|WITH|INSERT|UPDATE|DELETE)\\b|\\s*$)",
        "(?:VACUUM)(?:\\s*\\(|\\s+(?:ANALYZE|FULL|VERBOSE)\\b|\\s+[a-z_]+\\.[a-z_]+|\\s*$)",
        "ANALYZE(?:\\s+[a-z_]+\\.[a-z_]+|\\s*$)",
        "REINDEX\\s+(?:INDEX|TABLE|SCHEMA|DATABASE)\\b",
        "TRUNCATE\\s+(?:TABLE\\s|[a-z_]+\\.[a-z_]+)",
        "DO\\s+\\$",
        "COPY\\s+(?:\\(|[a-z_.]+\\s+(?:FROM|TO)\\b)",
        "LOCK\\s+TABLE\\s",
        "CALL\\s+[a-z_.]+\\s*\\(",
        "REFRESH\\s+MATERIALIZED\\s+VIEW",
        "COMMENT\\s+ON\\s",
        "(?:BEGIN|COMMIT|ROLLBACK)\\s*;?\\s*$",
        "SHOW\\s+[a-z_.]+\\s*;?\\s*$");

    static final Pattern STATEMENT = Pattern.compile("\\s*(?:" + String.join("|", STATEMENT_GRAMMAR) + ")");

    /** SQL the tests execute, per file. Reduce-only and exact: lower an entry in the same edit that converts a site. */
    private static final Map<String, Integer> EXECUTED = Map.ofEntries(
        Map.entry("dev/nexus/service/ArbiterCompletenessTest.java", 1),
        Map.entry("dev/nexus/service/CatalogRepositoryTest.java", 5),
        Map.entry("dev/nexus/service/CombinedQueryParityTest.java", 3),
        Map.entry("dev/nexus/service/DdlCounts.java", 1),
        Map.entry("dev/nexus/service/GraphHopParityTest.java", 6),
        Map.entry("dev/nexus/service/SharedCluster.java", 3),
        // a verbatim copy of assign_from_chashes_1024's cross branch, run under a bare EXPLAIN
        Map.entry("dev/nexus/service/TaxonomyAssignCrossLateralHnswTest.java", 8),
        Map.entry("dev/nexus/service/vectors/ChunkLiveOwnersInlineRecallIntegrationTest.java", 2),
        Map.entry("dev/nexus/service/vectors/ChunkLiveOwnersMsz9iScaleIntegrationTest.java", 7),
        Map.entry("dev/nexus/service/vectors/ChunkLiveOwnersRecallExtendedIntegrationTest.java", 3),
        Map.entry("dev/nexus/service/vectors/ManifestLessCensusNotesGuardIndexPlanShapeTest.java", 1));

    private static final int EXECUTED_TOTAL = 40;

    /** SQL the tests never send to a database, per file, frozen. Each entry says what the text is for. */
    private static final Map<String, Integer> TEXT = Map.ofEntries(
        // input to the changelog bulk-grant scanner under test
        Map.entry("dev/nexus/service/ChangelogBulkGrantConformanceTest.java", 4),
        // an expected message substring ("ALTER TABLE on chunks leaves...")
        Map.entry("dev/nexus/service/ChunkLeafAutovacuumReloptionsIntegrationTest.java", 1),
        // input to DdlLogParser under test
        Map.entry("dev/nexus/service/DdlLogParserTest.java", 23),
        // input to the extension-bootstrap site scanner under test
        Map.entry("dev/nexus/service/ExtensionBootstrapSiteConformanceTest.java", 6),
        // an expected message substring
        Map.entry("dev/nexus/service/GrantsPgMonitorTest.java", 1),
        // expected changeset text read back by PgContainerHelper.changesetForwardText
        Map.entry("dev/nexus/service/PgContainerHelperChangesetTextTest.java", 5),
        // an expected remedy in the migrator's refusal message
        Map.entry("dev/nexus/service/SchemaMigratorIntegrationTest.java", 1),
        // expected changeset text, forward and rollback
        Map.entry("dev/nexus/service/StagingSchemaDropLiquibaseTest.java", 2),
        // an expected remedy in the changeset's refusal message
        Map.entry("dev/nexus/service/Taxonomy011ForeignOwnedDiagViewTest.java", 1),
        // the shared CTE span pinned verbatim in two function bodies
        Map.entry("dev/nexus/service/db/CrossPreviewDriftTest.java", 5),
        // an expected remedy in ChunksIsolationCheck's refusal
        Map.entry("dev/nexus/service/db/ChunksIsolationCheckTest.java", 1),
        // the raw SQL gate's own scanner fixtures and messages
        Map.entry("dev/nexus/service/db/RawSqlGateTest.java", 6),
        // input to the reapable-consumers scanner under test
        Map.entry("dev/nexus/service/db/ReapableConsumersScanTest.java", 6),
        // a pattern matched against changelog text
        Map.entry("dev/nexus/service/db/TenantScopeVacuumMaintainGrantParityTest.java", 1),
        // expected substrings of the trigger function bodies
        Map.entry("dev/nexus/service/vectors/ChunkIsReapableIntegrationTest.java", 1),
        // an expected substring of the gc function body
        Map.entry("dev/nexus/service/vectors/PgVectorRepositoryGcQuarantineTest.java", 1));

    private static final int TEXT_TOTAL = 65;

    /** Floors that keep the scan from passing on an empty or misrooted walk. */
    private static final int MIN_FILES = 500;
    private static final int MIN_LITERALS = 20_000;

    /** Every string literal and text block body in {@code src}, comments ignored, in source order. */
    static List<String> literals(String src) {
        String s = RawSqlGateTest.stripJavaComments(src);
        List<String> out = new ArrayList<>();
        int i = 0;
        int n = s.length();
        while (i < n) {
            char c = s.charAt(i);
            if (s.startsWith("\"\"\"", i)) {
                int end = s.indexOf("\"\"\"", i + 3);
                end = end < 0 ? n : end;
                out.add(s.substring(i + 3, end));
                i = Math.min(end + 3, n);
            } else if (c == '"') {
                int j = i + 1;
                while (j < n && s.charAt(j) != '"') {
                    j += s.charAt(j) == '\\' ? 2 : 1;
                }
                out.add(s.substring(i + 1, Math.min(j, n)));
                i = j + 1;
            } else if (c == '\'') {
                int j = i + 1;
                while (j < n && s.charAt(j) != '\'') {
                    j += s.charAt(j) == '\\' ? 2 : 1;
                }
                i = j + 1;
            } else {
                i++;
            }
        }
        return out;
    }

    static boolean isStatement(String literal) {
        return STATEMENT.matcher(literal).lookingAt();
    }

    static long statementCount(String src) {
        return literals(src).stream().filter(SqlStringLiteralGateTest::isStatement).count();
    }

    @Test
    void sqlStringLiterals_inTheTestTree_matchTheExecutedAndTextMapsExactly() throws IOException {
        Path root = Path.of("src", "test", "java");
        assertThat(root).exists();
        Map<String, Integer> actual = new TreeMap<>();
        int[] files = {0};
        long[] examined = {0};
        try (Stream<Path> walk = Files.walk(root)) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).toList()) {
                String src = Files.readString(p);
                files[0]++;
                List<String> lits = literals(src);
                examined[0] += lits.size();
                int count = (int) lits.stream().filter(SqlStringLiteralGateTest::isStatement).count();
                if (count > 0) {
                    actual.put(root.relativize(p).toString().replace(java.io.File.separatorChar, '/'), count);
                }
            }
        }
        assertThat(files[0]).as("test files scanned").isGreaterThanOrEqualTo(MIN_FILES);
        assertThat(examined[0]).as("string literals examined").isGreaterThanOrEqualTo(MIN_LITERALS);

        assertThat(EXECUTED.keySet()).as("a file sits in one map").doesNotContainAnyElementsOf(TEXT.keySet());
        List<String> violations = new ArrayList<>();
        for (var e : actual.entrySet()) {
            int declared = EXECUTED.getOrDefault(e.getKey(), TEXT.getOrDefault(e.getKey(), 0));
            if (e.getValue() > declared) {
                violations.add(e.getKey() + ": " + e.getValue() + " SQL statement literal(s), map declares " + declared
                    + " -- write the statement as typed jOOQ, a validated PgContainerHelper helper or a test changelog");
            }
        }
        for (Map<String, Integer> map : List.of(EXECUTED, TEXT)) {
            for (var e : map.entrySet()) {
                int found = actual.getOrDefault(e.getKey(), 0);
                if (found < e.getValue()) {
                    violations.add(e.getKey() + ": map declares " + e.getValue() + ", found " + found
                        + " -- lower the entry (and its total) in the same edit, or remove it at zero");
                }
            }
        }
        assertThat(violations).as("SQL string literals in service/src/test/java (nexus-cbo4a)").isEmpty();
        assertThat(EXECUTED.values().stream().mapToInt(Integer::intValue).sum()).isEqualTo(EXECUTED_TOTAL);
        assertThat(TEXT.values().stream().mapToInt(Integer::intValue).sum()).isEqualTo(TEXT_TOTAL);
    }

    /** One fixture per line after the header line, which keeps the block itself from reading as a statement. */
    private static List<String> fixtureLines(String block) {
        return block.lines().skip(1).toList();
    }

    @Test
    void statementGrammar_matchesStatements_andNotProseOpeningWithTheSameWord() {
        assertThat(fixtureLines("""
                statements:
                SELECT count(*) FROM nexus.chunks c
                  INSERT INTO nexus.catalog_owners (tenant_id) VALUES (?)
                UPDATE nexus.chunks SET collection = ?
                DELETE FROM nexus.chunks c\s
                WITH batch AS (
                WITH RECURSIVE reach(tumbler, d) AS (
                CREATE OR REPLACE FUNCTION nexus.f()
                ALTER TABLE nexus.databasechangelog SET SCHEMA public
                DROP POLICY p ON nexus.chunks
                GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA nexus TO nexus_svc
                SET LOCAL statement_timeout = 0
                SET ROLE nexus_svc
                EXPLAIN (ANALYZE, BUFFERS) SELECT 1
                EXPLAIN\s
                VACUUM (ANALYZE) nexus.chunks
                REINDEX INDEX nexus.i
                DO $$ BEGIN END $$
                COMMIT
                """))
            .hasSize(18)
            .allMatch(SqlStringLiteralGateTest::isStatement);
        assertThat(fixtureLines("""
                prose:
                WITH CHECK must reference the tenant GUC
                DELETE trigger: tenant B's chunk is not tenant A's
                SET NOT NULL on doc_id must reject a NULL
                SET clause: CREATE FUNCTION ... SET hnsw.* is refused
                EXPLAIN cannot then see the index scan
                DO NOTHING: still exactly one row
                VACUUM is not given a network timeout
                ANALYZE ran on %s
                CREATE EXTENSION
                SECURITY INVOKER, not STRICT
                select 1
                nexus.chunks
                """))
            .hasSize(12)
            .noneMatch(SqlStringLiteralGateTest::isStatement);
    }

    @Test
    void literalScan_countsStringsAndTextBlocks_oncePerStatement_andSkipsComments() {
        String tq = "\"\"\"";
        String src = "class X {\n"
            + "    // \"SELECT 1 FROM t\" in a line comment\n"
            + "    /* \"DELETE FROM t\" in a block comment */\n"
            + "    static final String A = \"DELETE FROM nexus.chunks c \"\n"
            + "        + \"WHERE c.tenant_id = ?\";\n"
            + "    static final String B = " + tq + "\n        SELECT 1 FROM nexus.chunks\n        " + tq + ";\n"
            + "    static final char Q = '\"';\n"
            + "    static final String C = \"WITH CHECK must reference the GUC\";\n"
            + "    void f() { run(\"SET LOCAL work_mem = '1MB'\"); }\n"
            + "}\n";
        assertThat(statementCount(src)).isEqualTo(3);
    }

    @Test
    void literalScan_aHugeLiteral_doesNotOverflowTheStack() {
        String src = "class X { String s = \"SELECT " + "a".repeat(200_000) + "\"; }";
        assertThat(statementCount(src)).isEqualTo(1);
    }
}
