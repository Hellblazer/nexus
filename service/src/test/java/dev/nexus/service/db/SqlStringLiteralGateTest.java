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
 * text block, comments stripped, a leading SQL comment skipped) counts when it opens with an uppercase SQL verb
 * followed by that verb's grammar ({@link #STATEMENT}), including a statement split right after its keyword
 * ({@code "ALTER TABLE " + t}); prose that opens with the same word ("WITH CHECK must reference", "DELETE trigger",
 * "DO NOTHING", "ALTER TABLE on chunks leaves") does not. Every literal piece that opens a statement counts, so the
 * maps are literal counts and a statement assembled from several such pieces counts once per piece.
 *
 * <p>Two exact per-file literal maps. {@link #EXECUTED} is SQL the tests send to a database; it drains to zero.
 * {@link #TEXT} is SQL the tests never send: input to a parser or scanner under test, an expected substring of a
 * function body, changeset or error message. It is frozen per file, so a new executed statement in one of those
 * files still fails here. A file absent from both maps must have none.
 *
 * <p>Executed SQL that has no literal of its own reaches the database two ways, and each has its own exact map:
 * {@link #JDBC_ENTRY} counts {@code createStatement(}, {@code prepareStatement(} and {@code prepareCall(} calls (the
 * only JDBC routes that take SQL text: changeset or resource text replayed by hand arrives this way), and
 * {@link #PRODUCT_SQL} counts references to a product {@code *_SQL} constant. The drain is done when EXECUTED is
 * empty and every JDBC_ENTRY and PRODUCT_SQL entry left is one Sam has accepted. Lowercase SQL is outside the
 * grammar; the tree has none executed today.
 */
class SqlStringLiteralGateTest {

    /** An identifier part: plain, or quoted as {@code "x"} in a text block or {@code \\"x\\"} in a string. */
    private static final String PART = "(?:\\\\?\"[^\"\\\\]*+\\\\?\"|[A-Za-z_][\\w$]*+)";
    /**
     * A possibly schema-qualified identifier: a first part, then a possessive run of one character class, never a
     * repeated group, so a 200k-character literal cannot overflow the stack.
     */
    private static final String ID = PART + "[\\w$.\"\\\\]*+";

    private static final List<String> STATEMENT_GRAMMAR = List.of(
        // select, also split right after the verb or opening a subquery
        "\\(?\\s*SELECT\\s+(?:\\S|$)",
        "(?:INSERT|MERGE)\\s+INTO\\s",
        "VALUES\\s*\\(",
        "UPDATE\\s+(?:$|(?:ONLY\\s+)?" + ID + "(?:\\s*$|\\s+(?:(?:AS\\s+)?[A-Za-z_]\\w*+\\s+)?SET\\s))",
        "DELETE\\s+FROM\\s",
        "WITH\\s+(?:RECURSIVE\\s+)?" + ID + "(?:\\s*\\([^)]*+\\))?\\s+AS\\s*(?:(?:NOT\\s+)?MATERIALIZED\\s*)?\\(",
        // create or drop an object, also split right after the object word
        "(?:CREATE|DROP)\\s+(?:OR\\s+REPLACE\\s+)?(?:UNIQUE\\s+|TEMP(?:ORARY)?\\s+|UNLOGGED\\s+|MATERIALIZED\\s+)?(?:TABLE|INDEX|VIEW|FUNCTION|PROCEDURE|ROLE|USER|SCHEMA|EXTENSION|POLICY|TRIGGER|EVENT\\s+TRIGGER|RULE|SEQUENCE|TYPE|DATABASE|PUBLICATION|DOMAIN|AGGREGATE|OPERATOR|SERVER|STATISTICS|COLLATION|CAST|OWNED)\\s+(?:\\S|$)",
        // alter needs an object name, then a sub-verb or the end of the literal, so "ALTER TABLE on chunks" prose
        // does not match
        "ALTER\\s+(?:DEFAULT\\s+PRIVILEGES\\s|SYSTEM\\s|(?:MATERIALIZED\\s+)?(?:TABLE|INDEX|VIEW|FUNCTION|PROCEDURE|ROLE|USER|SCHEMA|EXTENSION|POLICY|TRIGGER|EVENT\\s+TRIGGER|RULE|SEQUENCE|TYPE|DATABASE|PUBLICATION|DOMAIN|AGGREGATE|OPERATOR|SERVER|STATISTICS|COLLATION|CAST)\\s+(?:$|(?:IF\\s+EXISTS\\s+)?(?:ONLY\\s+)?" + ID + "(?:\\s*\\([^)]*+\\))?(?:\\s*$|\\s+(?:ADD|DROP|ALTER|RENAME|SET|RESET|OWNER|ENABLE|DISABLE|FORCE|NO|ATTACH|DETACH|VALIDATE|INHERIT|NOINHERIT|CLUSTER|REPLICA|OF|NOT|WITH|IN|DEPENDS|REFRESH|CONNECTION|USING|OPTIONS|SUPERUSER|NOSUPERUSER|LOGIN|NOLOGIN|CREATEROLE|NOCREATEROLE|CREATEDB|NOCREATEDB|PASSWORD|BYPASSRLS|NOBYPASSRLS|VALID|UPDATE|ON|ALL|SECURITY|STABLE|IMMUTABLE|VOLATILE|STRICT|CALLED|COST|ROWS|PARALLEL|LEAKPROOF|NOT|RETURNS|TO)\\b)))",
        "(?:GRANT|REVOKE)\\s++[\\w,\\s]*?\\s(?:ON|TO|FROM)\\s",
        "SET\\s+(?:(?:LOCAL|SESSION)\\s+(?:\\S|$)|ROLE\\s|TRANSACTION\\s|CONSTRAINTS\\s+(?:ALL\\b|" + ID + ")|[a-z_][\\w.]*+\\s*(?:=|TO\\s))",
        "RESET\\s+(?:ROLE|ALL|[a-z_][\\w.]*+)\\s*;?\\s*$",
        "(?:EXPLAIN)(?:\\s*\\(|\\s+(?:ANALYZE|VERBOSE|SELECT|WITH|INSERT|UPDATE|DELETE)\\b|\\s*$)",
        "(?:VACUUM)(?:\\s*\\(|\\s+(?:ANALYZE|FULL|VERBOSE)\\b|\\s+" + ID + "\\s*;?\\s*$|\\s*;?\\s*$)",
        "(?:ANALYZE)(?:\\s+(?:VERBOSE\\s+)?" + ID + "\\s*;?\\s*$|\\s*;?\\s*$)",
        "(?:CLUSTER)(?:\\s+(?:VERBOSE\\s+)?" + ID + "(?:\\s+USING\\s+" + ID + ")?)?\\s*;?\\s*$",
        "REINDEX\\s+(?:\\([^)]*+\\)\\s*)?(?:INDEX|TABLE|SCHEMA|DATABASE|SYSTEM)\\b",
        "TRUNCATE\\s+(?:TABLE\\s+)?(?:$|(?:ONLY\\s+)?" + ID + "\\s*(?:[,;]|CASCADE\\b|RESTRICT\\b|RESTART\\b|CONTINUE\\b|$))",
        "DO\\s+[$']",
        "COPY\\s+(?:\\(|" + ID + "(?:\\s*\\([^)]*+\\))?(?:\\s*$|\\s+(?:FROM|TO)\\b))",
        "LOCK\\s+(?:TABLE\\s+)?(?:$|(?:ONLY\\s+)?" + ID + "\\s*(?:IN\\b|NOWAIT\\b|[,;]|$))",
        "CALL\\s+" + ID + "\\s*\\(",
        "REFRESH\\s+MATERIALIZED\\s+VIEW\\s",
        "COMMENT\\s+ON\\s",
        // a bare transaction verb needs its semicolon, so hasMessageContaining("ROLLBACK") does not match
        "(?:BEGIN|COMMIT|ROLLBACK|END|ABORT)(?:\\s+(?:WORK|TRANSACTION))?\\s*;\\s*$",
        "(?:BEGIN|START\\s+TRANSACTION)\\s+(?:ISOLATION|READ|TRANSACTION\\s+ISOLATION)\\b",
        "START\\s+TRANSACTION\\s*;?\\s*$",
        "(?:SAVEPOINT|RELEASE(?:\\s+SAVEPOINT)?)\\s+" + ID + "\\s*;?\\s*$",
        "(?:ROLLBACK)\\s+(?:WORK\\s+|TRANSACTION\\s+)?TO\\s+(?:SAVEPOINT\\s+)?" + ID,
        "(?:COMMIT|ROLLBACK)\\s+(?:AND\\s+(?:NO\\s+)?CHAIN|PREPARED)\\b",
        "PREPARE\\s+" + ID + "\\s*(?:\\([^)]*+\\)\\s*)?AS\\s",
        "SHOW\\s+(?:ALL|[a-z_][\\w.]*+)\\s*;?\\s*$",
        "DISCARD\\s+(?:ALL|PLANS|SEQUENCES|TEMP|TEMPORARY)\\b",
        "(?:LISTEN|UNLISTEN|NOTIFY)\\s+" + ID + "\\s*(?:[,;]|$)",
        "CHECKPOINT\\s*;?\\s*$");

    static final Pattern STATEMENT = Pattern.compile("(?:" + String.join("|", STATEMENT_GRAMMAR) + ")");

    /** SQL the tests execute, per file. Reduce-only and exact: lower an entry in the same edit that converts a site. */
    private static final Map<String, Integer> EXECUTED = Map.ofEntries(
        Map.entry("dev/nexus/service/ArbiterCompletenessTest.java", 2),
        Map.entry("dev/nexus/service/CatalogRepositoryTest.java", 8),
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

    private static final int EXECUTED_TOTAL = 44;

    /** SQL the tests never send to a database, per file, frozen. Each entry says what the text is for. */
    private static final Map<String, Integer> TEXT = Map.ofEntries(
        // input to the changelog bulk-grant scanner under test
        Map.entry("dev/nexus/service/ChangelogBulkGrantConformanceTest.java", 4),
        // input to DdlLogParser under test
        Map.entry("dev/nexus/service/DdlLogParserTest.java", 28),
        // input to the extension-bootstrap site scanner under test
        Map.entry("dev/nexus/service/ExtensionBootstrapSiteConformanceTest.java", 6),
        // an expected message substring
        Map.entry("dev/nexus/service/GrantsPgMonitorTest.java", 1),
        // expected changeset text read back by PgContainerHelper.changesetForwardText
        Map.entry("dev/nexus/service/PgContainerHelperChangesetTextTest.java", 4),
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
        Map.entry("dev/nexus/service/db/RawSqlGateTest.java", 3),
        // input to the reapable-consumers scanner under test
        Map.entry("dev/nexus/service/db/ReapableConsumersScanTest.java", 6),
        // a pattern matched against changelog text
        Map.entry("dev/nexus/service/db/TenantScopeVacuumMaintainGrantParityTest.java", 1),
        // expected substrings of the trigger function bodies
        Map.entry("dev/nexus/service/vectors/ChunkIsReapableIntegrationTest.java", 3),
        // an expected substring of the gc function body
        Map.entry("dev/nexus/service/vectors/PgVectorRepositoryGcQuarantineTest.java", 1));

    private static final int TEXT_TOTAL = 67;

    /** JDBC calls that take SQL text, per file, exact. Each entry says what the SQL is. */
    private static final Map<String, Integer> JDBC_ENTRY = Map.ofEntries(
        // the seed INSERT counted in EXECUTED
        Map.entry("dev/nexus/service/ArbiterCompletenessTest.java", 1),
        // catalog-016-0's shipped dedup text with NULLIF(indexed_at, '') rewritten for the column's later timestamptz
        // type, so it cannot go through replayChangeset verbatim
        Map.entry("dev/nexus/service/Catalog016SourceUriUniqueTest.java", 1),
        // the statements counted in EXECUTED; held by another session
        Map.entry("dev/nexus/service/CatalogRepositoryTest.java", 4),
        // oracle and EXPLAIN statements counted in EXECUTED
        Map.entry("dev/nexus/service/CombinedQueryParityTest.java", 1),
        Map.entry("dev/nexus/service/GraphHopParityTest.java", 2),
        // replayChangeset: product changeset text run verbatim for its NOTICE chain (SANCTIONED RAW)
        Map.entry("dev/nexus/service/PgContainerHelper.java", 1),
        // plpgsql_check's own report query, read from its resource
        Map.entry("dev/nexus/service/PlpgsqlCheck.java", 1),
        // cluster bootstrap: role reset DO block, CREATE and DROP DATABASE counted in EXECUTED
        Map.entry("dev/nexus/service/SharedCluster.java", 3),
        // a typed jOOQ insert rendered by ctx.render and bound by hand, for its lock order
        Map.entry("dev/nexus/service/TopicsDocCountDeadlockConcurrencyTest.java", 1));

    private static final int JDBC_ENTRY_TOTAL = 15;

    /** References to a product {@code *_SQL} constant, per file, exact. */
    private static final Map<String, Integer> PRODUCT_SQL = Map.ofEntries(
        // ChashRepository.PROBE_SQL explained through resultQuery
        Map.entry("dev/nexus/service/ChashProbePlanShapeTest.java", 1),
        // MANIFEST_LESS_CENSUS_SQL run in its literal-value form (static statement, values inlined by jOOQ)
        Map.entry("dev/nexus/service/vectors/ManifestLessCensusIntegrationTest.java", 1),
        // MANIFEST_LESS_CENSUS_SQL under the EXPLAIN counted in EXECUTED
        Map.entry("dev/nexus/service/vectors/ManifestLessCensusNotesGuardIndexPlanShapeTest.java", 1),
        // MANIFEST_LESS_CENSUS_SQL compared byte for byte with its changelog, never executed
        Map.entry("dev/nexus/service/vectors/ManifestLessCensusSqlIdentityTest.java", 1));

    private static final int PRODUCT_SQL_TOTAL = 4;

    private static final Pattern JDBC_ENTRY_CALL =
        Pattern.compile("\\b(?:createStatement|prepareStatement|prepareCall)\\s*+\\(");
    private static final Pattern PRODUCT_SQL_REF = Pattern.compile("\\b[A-Z]\\w*+\\.[A-Z][A-Z0-9_]*_SQL\\b");

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

    /** {@code src} with comments removed and every string, text block and char literal emptied. */
    static String codeOnly(String src) {
        String s = RawSqlGateTest.stripJavaComments(src);
        StringBuilder out = new StringBuilder(s.length());
        int i = 0;
        int n = s.length();
        while (i < n) {
            char c = s.charAt(i);
            if (s.startsWith("\"\"\"", i)) {
                int end = s.indexOf("\"\"\"", i + 3);
                i = end < 0 ? n : end + 3;
                out.append("\"\"");
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && s.charAt(j) != c) {
                    j += s.charAt(j) == '\\' ? 2 : 1;
                }
                i = j + 1;
                out.append(c).append(c);
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    static int occurrences(Pattern p, String code) {
        return (int) p.matcher(code).results().count();
    }

    /** The literal after leading whitespace and leading SQL comments ({@code --} to a newline, or a block). */
    static String afterLeadingComments(String literal) {
        int i = 0;
        int n = literal.length();
        while (true) {
            while (i < n && Character.isWhitespace(literal.charAt(i))) {
                i++;
            }
            if (literal.startsWith("\\n", i) || literal.startsWith("\\t", i)) {
                i += 2;
            } else if (literal.startsWith("--", i)) {
                int nl = literal.indexOf('\n', i);
                int esc = literal.indexOf("\\n", i);
                int end = nl < 0 ? esc : esc < 0 ? nl : Math.min(nl, esc);
                if (end < 0) {
                    return "";
                }
                i = end;
            } else if (literal.startsWith("/*", i)) {
                int end = literal.indexOf("*/", i + 2);
                if (end < 0) {
                    return "";
                }
                i = end + 2;
            } else {
                return literal.substring(i);
            }
        }
    }

    static boolean isStatement(String literal) {
        return STATEMENT.matcher(afterLeadingComments(literal)).lookingAt();
    }

    static long statementCount(String src) {
        return literals(src).stream().filter(SqlStringLiteralGateTest::isStatement).count();
    }

    @Test
    void sqlStringLiterals_andSqlSinks_inTheTestTree_matchTheirMapsExactly() throws IOException {
        Path root = Path.of("src", "test", "java");
        assertThat(root).exists();
        Map<String, Integer> literalCounts = new TreeMap<>();
        Map<String, Integer> jdbcCounts = new TreeMap<>();
        Map<String, Integer> productCounts = new TreeMap<>();
        int files = 0;
        long examined = 0;
        try (Stream<Path> walk = Files.walk(root)) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).toList()) {
                String src = Files.readString(p);
                String rel = root.relativize(p).toString().replace(java.io.File.separatorChar, '/');
                files++;
                List<String> lits = literals(src);
                examined += lits.size();
                putIfPositive(literalCounts, rel, (int) lits.stream().filter(SqlStringLiteralGateTest::isStatement).count());
                String code = codeOnly(src);
                putIfPositive(jdbcCounts, rel, occurrences(JDBC_ENTRY_CALL, code));
                putIfPositive(productCounts, rel, occurrences(PRODUCT_SQL_REF, code));
            }
        }
        assertThat(files).as("test files scanned").isGreaterThanOrEqualTo(MIN_FILES);
        assertThat(examined).as("string literals examined").isGreaterThanOrEqualTo(MIN_LITERALS);
        assertThat(EXECUTED.keySet()).as("a file sits in one literal map").doesNotContainAnyElementsOf(TEXT.keySet());

        Map<String, Integer> literalMaps = new TreeMap<>(EXECUTED);
        literalMaps.putAll(TEXT);
        List<String> violations = new ArrayList<>();
        diff("SQL statement literal", literalCounts, literalMaps,
            "write the statement as typed jOOQ, a validated PgContainerHelper helper or a test changelog", violations);
        diff("JDBC SQL-text call", jdbcCounts, JDBC_ENTRY,
            "send typed jOOQ, or replay a changeset through PgContainerHelper.replayChangeset", violations);
        diff("product *_SQL reference", productCounts, PRODUCT_SQL,
            "run the product method that owns the constant, or register the use with its reason", violations);
        assertThat(violations).as("SQL text in service/src/test/java (nexus-cbo4a)").isEmpty();
        assertThat(sum(EXECUTED)).isEqualTo(EXECUTED_TOTAL);
        assertThat(sum(TEXT)).isEqualTo(TEXT_TOTAL);
        assertThat(sum(JDBC_ENTRY)).isEqualTo(JDBC_ENTRY_TOTAL);
        assertThat(sum(PRODUCT_SQL)).isEqualTo(PRODUCT_SQL_TOTAL);
    }

    private static void putIfPositive(Map<String, Integer> counts, String file, int n) {
        if (n > 0) {
            counts.put(file, n);
        }
    }

    private static int sum(Map<String, Integer> map) {
        return map.values().stream().mapToInt(Integer::intValue).sum();
    }

    /** Both directions: a count above its entry is a regression, below it a stale entry to lower. */
    private static void diff(String what, Map<String, Integer> actual, Map<String, Integer> declared, String remedy,
            List<String> violations) {
        for (var e : actual.entrySet()) {
            int d = declared.getOrDefault(e.getKey(), 0);
            if (e.getValue() > d) {
                violations.add(e.getKey() + ": " + e.getValue() + " " + what + "(s), map declares " + d + " -- " + remedy);
            }
        }
        for (var e : declared.entrySet()) {
            int found = actual.getOrDefault(e.getKey(), 0);
            if (found < e.getValue()) {
                violations.add(e.getKey() + ": map declares " + e.getValue() + " " + what + "(s), found " + found
                    + " -- lower the entry (and its total) in the same edit, or remove it at zero");
            }
        }
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
                SELECT\s
                (SELECT o.orphaned_at
                  INSERT INTO nexus.catalog_owners (tenant_id) VALUES (?)
                MERGE INTO nexus.t USING s ON true
                VALUES (?, ?)
                UPDATE nexus.chunks SET collection = ?
                UPDATE ONLY "nexus"."chunks" AS c SET a = 1
                UPDATE\s
                DELETE FROM nexus.chunks c\s
                WITH batch AS (
                WITH RECURSIVE reach(tumbler, d) AS (
                WITH "x" AS MATERIALIZED (
                CREATE OR REPLACE FUNCTION nexus.f()
                CREATE UNLOGGED TABLE a (id int)
                CREATE EVENT TRIGGER nx_probe
                CREATE INDEX\s
                DROP TABLE\s
                ALTER TABLE nexus.databasechangelog SET SCHEMA public
                ALTER TABLE\s
                ALTER FUNCTION nexus.f(int) OWNER TO r
                ALTER SYSTEM SET work_mem = '4MB'
                ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO r
                DROP POLICY p ON nexus.chunks
                GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA nexus TO nexus_svc
                REVOKE ADMIN OPTION FOR svc FROM bob
                SET LOCAL statement_timeout = 0
                SET LOCAL\s
                SET ROLE nexus_svc
                SET TRANSACTION ISOLATION LEVEL SERIALIZABLE
                SET CONSTRAINTS ALL DEFERRED
                RESET work_mem
                EXPLAIN (ANALYZE, BUFFERS) SELECT 1
                EXPLAIN\s
                VACUUM (ANALYZE) nexus.chunks
                VACUUM t
                ANALYZE nexus.chunks
                ANALYZE t
                CLUSTER t USING i
                REINDEX INDEX nexus.i
                TRUNCATE t
                TRUNCATE TABLE nexus.t CASCADE
                DO $$ BEGIN END $$
                COPY nexus.t (a) FROM STDIN
                LOCK nexus.t IN ACCESS EXCLUSIVE MODE
                LOCK TABLE\s
                CALL nexus.p(1)
                REFRESH MATERIALIZED VIEW nexus.v
                COMMENT ON TABLE t IS 'x'
                COMMIT;
                BEGIN ISOLATION LEVEL SERIALIZABLE
                START TRANSACTION
                SAVEPOINT s1
                RELEASE SAVEPOINT s1
                ROLLBACK TO SAVEPOINT s1
                PREPARE q (int) AS SELECT $1
                SHOW work_mem
                DISCARD ALL
                NOTIFY nexus_events
                CHECKPOINT
                -- a leading comment\\nDROP INDEX i
                ALTER TABLE nexus.
                ALTER TABLE nexus.chunks\s
                ALTER ROLE r\s
                ALTER MATERIALIZED VIEW nexus.v OWNER TO r
                UPDATE nexus.
                COPY nexus.t\s
                COMMIT AND CHAIN
                ROLLBACK PREPARED 'x'
                END;
                ABORT;
                UNLISTEN nexus_events
                /* c */ ALTER TABLE a ADD COLUMN b int
                """))
            .hasSize(73)
            .allMatch(SqlStringLiteralGateTest::isStatement);
        assertThat(fixtureLines("""
                prose:
                WITH CHECK must reference the tenant GUC
                WITH X must hold
                DELETE trigger: tenant B's chunk is not tenant A's
                SET NOT NULL on doc_id must reject a NULL
                SET clause: CREATE FUNCTION ... SET hnsw.* is refused
                SET LOCAL: reverts with the transaction
                SET CONSTRAINTS ... DEFERRED is transaction-control syntax
                EXPLAIN cannot then see the index scan
                DO NOTHING: still exactly one row
                VACUUM is not given a network timeout
                VACUUM step will reject a table
                ANALYZE ran on %s
                TRUNCATE is refused here
                LOCK is held by the other session
                CREATE EXTENSION
                ALTER TABLE on chunks leaves during the heal
                ALTER ROLE ... SET search_path must fail loud
                ALTER TABLE ... {NO} FORCE ROW LEVEL SECURITY is DDL
                REVOKE of PUBLIC or a DROP that loses access breaks search
                ROLLBACK
                COMMIT
                SELECT
                SECURITY INVOKER, not STRICT
                select 1
                nexus.chunks
                """))
            .hasSize(25)
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
            + "    static final String B = " + tq + "\n        S" + "ELECT 1 FROM nexus.chunks\n        " + tq + ";\n"
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
        for (String verb : fixtureLines("""
                openings:
                GRANT
                WITH
                UPDATE
                ALTER TABLE
                VACUUM
                COPY
                LOCK
                """)) {
            String opening = verb + " ";
            assertThat(isStatement(opening + "a.".repeat(100_000) + "a" + " ".repeat(100_000)))
                .as("%s over a long run", verb).isIn(true, false);
            assertThat(isStatement(opening + "a ".repeat(100_000))).as("%s over a long word run", verb)
                .isIn(true, false);
        }
    }
}
