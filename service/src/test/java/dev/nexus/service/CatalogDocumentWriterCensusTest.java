// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-rfvyy.6.4 / .6.6, RDR-243 assumption A2: a source-scan census of who writes the document sensitivity
 * flag and its audit table. No database. It checks four things, and nothing about the rule's behaviour (the
 * write-rules test pins that):
 *
 * <ol>
 *   <li><b>Insert census.</b> The methods of {@code CatalogRepository.java} that INSERT into
 *       {@code nexus.catalog_documents} (the {@code insertInto(CATALOG_DOCUMENTS ...)} call and the raw
 *       {@code INSERT INTO} / {@code mergeInto} spellings) are exactly {@link #WRITERS}, in both directions: an
 *       unnamed writer fails, and so does a named method with no hit. The census checks METHOD NAMES; it does
 *       not read the inserted flag value, so a new writer is only caught by its name.</li>
 *   <li><b>Assignment census.</b> Every {@code .set(...SENSITIVE, value)} (and every raw {@code SET sensitive =})
 *       anywhere under {@code service/src/main/java} assigns a literal {@code true}, or {@code sensitive} inside
 *       {@code setSensitivity} (the audited route), and its enclosing method also calls
 *       {@code recordSensitivityChanges}. The sites are exactly {@code raiseSensitivity}, {@code raiseWrittenRows}
 *       and {@code setSensitivity}: no ON CONFLICT arm assigns the column (that is what keeps the flag one-way
 *       on upsert and import, the raise being a separate audited UPDATE), and each of the three insert writers
 *       that ON CONFLICT-update calls {@code raiseWrittenRows}.</li>
 *   <li><b>Append-only audit table.</b> Outside comments, every mention of {@code catalog_sensitivity_changes}
 *       in {@code service/src/main/java} sits in {@code recordSensitivityChanges}, which inserts and nothing
 *       else; no file under {@code service/src/main} (Java, changesets, SQL) issues an UPDATE, DELETE or
 *       TRUNCATE against it. This is a scan of source text: it does not see dynamic SQL built from a table name
 *       held in a variable, and it does not narrow the database grants (nexus_svc keeps DML on the table).</li>
 *   <li><b>Non-vacuity.</b> Each scan must examine at least the number of sites it is known to have, and a
 *       synthetic source with a violating site is flagged by the same scan.</li>
 * </ol>
 *
 * <p>A change that adds a writer, an assignment form, or a reader of the audit table edits this class in the
 * same commit, which is the review point.
 */
class CatalogDocumentWriterCensusTest {

    private static final Path SOURCE =
        Path.of("src", "main", "java", "dev", "nexus", "service", "db", "CatalogRepository.java");

    /** The methods of CatalogRepository.java that insert into catalog_documents (A2). */
    static final Set<String> WRITERS = Set.of(
        "upsertDocument",
        "registerDocumentWithOutcome",
        "registerDocumentManyWithOutcome",
        "importDocumentsBatch",
        "doImportDocument");

    private static final Pattern INSERT_HIT = Pattern.compile(
        "insertInto\\(\\s*(?:dev\\.nexus\\.service\\.jooq\\.nexus\\.Tables\\.)?CATALOG_DOCUMENTS\\b"
            + "|mergeInto\\(\\s*CATALOG_DOCUMENTS\\b"
            + "|(?i:insert\\s+into\\s+(?:nexus\\.)?catalog_documents\\b)"
            + "|insertInto\\(\\s*(?:DSL\\.)?(?:table|name)\\(\\s*\"(?:nexus\\.)?catalog_documents\"");

    /** A method declaration at class-member indent (four spaces), up to its name. */
    private static final Pattern METHOD_DECL = Pattern.compile(
        "(?m)^    (?=\\S)(?:(?:public|private|protected|static|final|synchronized)\\s+)*[\\w<>\\[\\], .?]+?\\s+(\\w+)\\s*\\(");

    /** The method declarations of {@code src}, in order: start offset and name. */
    private static List<Object[]> methodDecls(String src) {
        List<Object[]> decls = new ArrayList<>();
        Matcher d = METHOD_DECL.matcher(src);
        while (d.find()) {
            String n = d.group(1);
            if (Set.of("if", "for", "while", "switch", "catch", "return", "new", "else").contains(n)) continue;
            decls.add(new Object[] {d.start(), n});
        }
        return decls;
    }

    private static String ownerAt(List<Object[]> decls, int pos) {
        String owner = "<outside any method>";
        for (Object[] decl : decls) {
            if ((int) decl[0] <= pos) owner = (String) decl[1]; else break;
        }
        return owner;
    }

    /** Methods of {@code src} that contain a catalog_documents insert, with the hit count. */
    static List<String> writerHits(String src) {
        List<Object[]> decls = methodDecls(src);
        List<String> hits = new ArrayList<>();
        Matcher m = INSERT_HIT.matcher(src);
        while (m.find()) hits.add(ownerAt(decls, m.start()));
        return hits;
    }

    @Test
    void everyInsertIntoCatalogDocumentsIsInTheNamedWriterSet() throws IOException {
        List<String> hits = writerHits(Files.readString(SOURCE));
        assertThat(hits).as("non-vacuity: the scan examined the five known insert statements")
            .hasSizeGreaterThanOrEqualTo(WRITERS.size());
        assertThat(new TreeSet<>(hits))
            .as("methods of CatalogRepository.java that insert into catalog_documents (found %s); an unnamed "
                + "writer must OR the sensitive flag and join WRITERS in the same commit, a stale name must go",
                hits)
            .isEqualTo(new TreeSet<>(WRITERS));
    }

    @Test
    void theScanSeesASixthWriterAndEveryRawSpelling() {
        String fake = """
            class X {
                void upsertDocument() {
                    ctx.insertInto(CATALOG_DOCUMENTS, A).values(1).execute();
                }
                void sneaky() {
                    ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id) VALUES (?)", t);
                }
                void viaMerge() {
                    ctx.mergeInto(CATALOG_DOCUMENTS).using(x).execute();
                }
                void viaTable() {
                    ctx.insertInto(DSL.table("catalog_documents")).values(1).execute();
                }
            }
            """;
        assertThat(new TreeSet<>(writerHits(fake)))
            .containsExactlyInAnyOrder("upsertDocument", "sneaky", "viaMerge", "viaTable");
    }

    // ── the assignment census ───────────────────────────────────────────

    private static final Path MAIN_JAVA = Path.of("src", "main", "java");
    private static final Path MAIN = Path.of("src", "main");
    private static final Path REPOSITORY = SOURCE;

    /** {@code .set(<...>SENSITIVE, value)}: the column symbol is the first argument. */
    private static final Pattern JOOQ_SET = Pattern.compile("\\.set\\(\\s*(?:\\w+\\.)*SENSITIVE\\s*,");
    /** Raw SQL assigning the column: {@code SET ... sensitive =}. */
    private static final Pattern RAW_SET = Pattern.compile("(?is)\\bset\\b[^;\"]{0,200}?\\bsensitive\\s*=(?!=)");

    /**
     * Source with block and line comments blanked to spaces (offsets and newlines kept). String, char and text-block
     * literals are walked as literals and kept as they are: a {@code //} or {@code /*} inside one is not a comment
     * (a {@code "file:///x"} must not hide the code after it on the same line), and a quote inside a comment does
     * not open a literal.
     */
    static String stripJavaComments(String src) {
        StringBuilder out = new StringBuilder(src);
        int n = src.length();
        int i = 0;
        while (i < n) {
            char c = src.charAt(i);
            if (c == '/' && i + 1 < n && src.charAt(i + 1) == '/') {
                int end = src.indexOf('\n', i);
                if (end < 0) end = n;
                blank(out, i, end);
                i = end;
            } else if (c == '/' && i + 1 < n && src.charAt(i + 1) == '*') {
                int end = src.indexOf("*/", i + 2);
                end = end < 0 ? n : end + 2;
                blank(out, i, end);
                i = end;
            } else if (c == '"' && src.startsWith("\"\"\"", i)) {
                i += 3;
                while (i < n && !src.startsWith("\"\"\"", i)) i += src.charAt(i) == '\\' ? 2 : 1;
                i = Math.min(n, i + 3);
            } else if (c == '"' || c == '\'') {
                i++;
                while (i < n && src.charAt(i) != c && src.charAt(i) != '\n') i += src.charAt(i) == '\\' ? 2 : 1;
                i = Math.min(n, i + 1);
            } else {
                i++;
            }
        }
        return out.toString();
    }

    private static void blank(StringBuilder out, int from, int to) {
        for (int k = from; k < to; k++) if (out.charAt(k) != '\n') out.setCharAt(k, ' ');
    }

    /** XML/SQL text with changeset comments and SQL line comments blanked. */
    static String stripXmlSqlComments(String src) {
        StringBuilder out = new StringBuilder(src);
        Matcher m = Pattern.compile("(?s)<!--.*?-->|<comment>.*?</comment>|(?m)--[^\\n]*").matcher(src);
        while (m.find()) {
            for (int i = m.start(); i < m.end(); i++) if (out.charAt(i) != '\n') out.setCharAt(i, ' ');
        }
        return out.toString();
    }

    /** The text of the balanced-parenthesis argument list opening at {@code open} (the '(' offset). */
    private static String argsFrom(String src, int open) {
        int depth = 0;
        for (int i = open; i < src.length(); i++) {
            char c = src.charAt(i);
            if (c == '(') depth++;
            else if (c == ')' && --depth == 0) return src.substring(open + 1, i);
        }
        throw new IllegalStateException("unbalanced parenthesis at offset " + open);
    }

    /** A SENSITIVE assignment: the method it sits in, the value it assigns, and whether the method audits. */
    record Assignment(String method, String value, boolean methodAudits) {}

    /** Every assignment of the SENSITIVE column in {@code src} (jOOQ {@code .set} and raw {@code SET}). */
    static List<Assignment> assignments(String rawSrc) {
        String src = stripJavaComments(rawSrc);
        List<Object[]> decls = methodDecls(src);
        List<Assignment> out = new ArrayList<>();
        Matcher m = JOOQ_SET.matcher(src);
        while (m.find()) {
            String args = argsFrom(src, src.indexOf('(', m.start()));
            String value = args.substring(args.indexOf(',') + 1).strip().replaceAll("\\s+", " ");
            out.add(assignment(src, decls, m.start(), value));
        }
        Matcher r = RAW_SET.matcher(src);
        while (r.find()) out.add(assignment(src, decls, r.start(), "<raw SQL: " + r.group().strip() + ">"));
        return out;
    }

    private static Assignment assignment(String src, List<Object[]> decls, int pos, String value) {
        String method = ownerAt(decls, pos);
        int from = 0;
        for (Object[] decl : decls) if (decl[1].equals(method) && (int) decl[0] <= pos) from = (int) decl[0];
        int to = src.length();
        for (Object[] decl : decls) if ((int) decl[0] > pos) { to = (int) decl[0]; break; }
        return new Assignment(method, value, src.substring(from, to).contains("recordSensitivityChanges("));
    }

    /** Violations of the allowed assignment forms; empty when every site is permitted. */
    static List<String> assignmentViolations(List<Assignment> found) {
        List<String> bad = new ArrayList<>();
        for (Assignment a : found) {
            boolean allowedValue = a.value().equals("true")
                || (a.value().equals("sensitive") && a.method().equals("setSensitivity"));
            if (!allowedValue) bad.add(a.method() + " assigns " + a.value()
                + " (allowed: true, or `sensitive` in setSensitivity)");
            else if (!a.methodAudits()) bad.add(a.method() + " assigns " + a.value()
                + " but never calls recordSensitivityChanges");
        }
        return bad;
    }

    private static List<Path> javaFiles(Path root) throws IOException {
        try (var walk = Files.walk(root)) {
            return walk.filter(f -> f.toString().endsWith(".java")).sorted().toList();
        }
    }

    @Test
    void everyAssignmentOfTheSensitiveColumnIsATrueOrTheAuditedRoute_andNoOnConflictArmAssignsIt() throws IOException {
        List<Assignment> all = new ArrayList<>();
        int files = 0;
        for (Path f : javaFiles(MAIN_JAVA)) {
            files++;
            all.addAll(assignments(Files.readString(f)));
        }
        assertThat(files).as("non-vacuity: the scan walked the main source tree").isGreaterThan(50);
        assertThat(all).as("non-vacuity: the three known assignment sites; %s", all).hasSizeGreaterThanOrEqualTo(3);
        assertThat(assignmentViolations(all)).as("assignments of catalog_documents.sensitive: %s", all).isEmpty();
        Set<String> methods = new TreeSet<>();
        for (Assignment a : all) methods.add(a.method());
        assertThat(methods)
            .as("the only methods that assign the column: the raise helpers and the audited route, never an "
                + "ON CONFLICT writer (found %s)", all)
            .isEqualTo(Set.of("raiseSensitivity", "raiseWrittenRows", "setSensitivity"));

        String repo = stripJavaComments(Files.readString(REPOSITORY));
        assertThat(repo).as("the stored-OR-excluded constant is gone: no arm assigns the column")
            .doesNotContain("EX_DOC_SENSITIVE_OR");
        List<Object[]> decls = methodDecls(repo);
        for (String writer : List.of("upsertDocument", "importDocumentsBatch", "doImportDocument")) {
            assertThat(methodBody(repo, decls, writer))
                .as("%s raises a flagged request's rows through raiseWrittenRows, after its ON CONFLICT write", writer)
                .contains("raiseWrittenRows(");
        }
    }

    private static String methodBody(String src, List<Object[]> decls, String method) {
        for (int k = 0; k < decls.size(); k++) {
            if (decls.get(k)[1].equals(method)) {
                int to = k + 1 < decls.size() ? (int) decls.get(k + 1)[0] : src.length();
                return src.substring((int) decls.get(k)[0], to);
            }
        }
        throw new AssertionError("no method " + method);
    }

    @Test
    void theAssignmentScanSeesALoweringAssignmentInEverySpelling() {
        String fake = """
            class X {
                void raiseWrittenRows() {
                    ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, true).execute();
                    recordSensitivityChanges(ctx);
                }
                void lowers() {
                    ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, false).execute();
                }
                void viaExcluded() {
                    ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, DSL.excluded(CATALOG_DOCUMENTS.SENSITIVE)).execute();
                }
                void raw() {
                    ctx.execute("UPDATE nexus.catalog_documents SET sensitive = false WHERE tenant_id = ?", t);
                }
                void unaudited() {
                    ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, true).execute();
                }
                void commented() {
                    // .set(CATALOG_DOCUMENTS.SENSITIVE, false) in a comment is not code
                }
            }
            """;
        List<Assignment> found = assignments(fake);
        assertThat(found).hasSize(5);
        assertThat(assignmentViolations(found)).hasSize(4)
            .anyMatch(v -> v.startsWith("lowers assigns false"))
            .anyMatch(v -> v.startsWith("viaExcluded assigns DSL.excluded"))
            .anyMatch(v -> v.startsWith("raw assigns <raw SQL"))
            .anyMatch(v -> v.startsWith("unaudited assigns true but never calls recordSensitivityChanges"));
    }

    @Test
    void theCommentStripperIsStringAware_aLiteralWithSlashesDoesNotHideCodeAfterIt() {
        // the review's case: "file:///x" opens no comment, so the violating assignment on the same line is seen
        String sameLine = """
            class X {
                void sneaky() {
                    String u = "file:///x"; ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, false).execute();
                }
            }
            """;
        List<Assignment> found = assignments(sameLine);
        assertThat(found).as("the assignment after a \"file:///x\" literal on the same line is found").hasSize(1);
        assertThat(assignmentViolations(found)).hasSize(1).allMatch(v -> v.startsWith("sneaky assigns false"));

        // block-comment opener, escaped quote, char literal and text block are all literals too
        String literals = """
            class X {
                void a() { String s = "/* not a comment"; ctx.set(CATALOG_DOCUMENTS.SENSITIVE, false); }
                void b() { String s = "say \\"//\\" then"; ctx.set(CATALOG_DOCUMENTS.SENSITIVE, false); }
                void c() { char q = '"'; ctx.set(CATALOG_DOCUMENTS.SENSITIVE, false); }
                void d() { String s = \"\"\"
                    // inside a text block
                    \"\"\"; ctx.set(CATALOG_DOCUMENTS.SENSITIVE, false); }
            }
            """;
        assertThat(assignments(literals)).extracting(Assignment::method).containsExactly("a", "b", "c", "d");

        // and real comments, including one holding a quote, are still blanked
        String comments = """
            class X {
                void e() {
                    // it's "quoted" .set(CATALOG_DOCUMENTS.SENSITIVE, false)
                    /* .set(CATALOG_DOCUMENTS.SENSITIVE, false) */
                    int x = 1; // trailing .set(CATALOG_DOCUMENTS.SENSITIVE, false)
                }
            }
            """;
        assertThat(assignments(comments)).isEmpty();
        assertThat(stripJavaComments(comments)).hasSameSizeAs(comments);
    }

    // ── the audit table is append-only by construction of the source ────

    private static final Pattern AUDIT_NAME = Pattern.compile("CATALOG_SENSITIVITY_CHANGES|(?i:catalog_sensitivity_changes)");
    private static final Pattern AUDIT_UPDATE_OR_DELETE = Pattern.compile(
        "(?is)\\b(?:update|delete\\s+from|truncate(?:\\s+table)?)\\s+(?:only\\s+)?(?:\\w+\\.)?catalog_sensitivity_changes\\b"
            + "|\\b(?:update|deleteFrom|truncate)\\(\\s*(?:\\w+\\.)*CATALOG_SENSITIVITY_CHANGES\\b");

    /** Methods mentioning the audit table (outside comments and import lines), with every mention. */
    static List<String> auditTableMentions(String rawSrc) {
        String src = stripJavaComments(rawSrc).replaceAll("(?m)^import .*$", "");
        List<Object[]> decls = methodDecls(src);
        List<String> out = new ArrayList<>();
        Matcher m = AUDIT_NAME.matcher(src);
        while (m.find()) out.add(ownerAt(decls, m.start()));
        return out;
    }

    @Test
    void onlyRecordSensitivityChangesWritesTheAuditTable_andNothingUpdatesOrDeletesIt() throws IOException {
        List<String> mentions = new ArrayList<>();
        for (Path f : javaFiles(MAIN_JAVA)) {
            for (String owner : auditTableMentions(Files.readString(f))) mentions.add(f.getFileName() + "#" + owner);
        }
        assertThat(mentions).as("non-vacuity: the writer's insert statement was found").isNotEmpty();
        assertThat(mentions).as("every mention of the audit table in main Java sits in recordSensitivityChanges")
            .allMatch(x -> x.equals("CatalogRepository.java#recordSensitivityChanges"));

        String repo = stripJavaComments(Files.readString(REPOSITORY));
        int from = repo.indexOf("static void recordSensitivityChanges(");
        String body = repo.substring(from, repo.indexOf("\n    }\n", from));
        assertThat(body).as("the writer inserts").contains("insertInto(CATALOG_SENSITIVITY_CHANGES");
        assertThat(body).as("the writer does nothing else to the table")
            .doesNotContain(".update(").doesNotContain("deleteFrom(").doesNotContain(".delete(");

        int examined = 0;
        List<String> offenders = new ArrayList<>();
        try (var walk = Files.walk(MAIN)) {
            for (Path f : walk.filter(Files::isRegularFile).sorted().toList()) {
                String name = f.toString();
                boolean java = name.endsWith(".java");
                if (!(java || name.endsWith(".xml") || name.endsWith(".sql"))) continue;
                examined++;
                String text = java ? stripJavaComments(Files.readString(f)) : stripXmlSqlComments(Files.readString(f));
                if (AUDIT_UPDATE_OR_DELETE.matcher(text).find()) offenders.add(name);
            }
        }
        assertThat(examined).as("non-vacuity: the scan read the main Java, changesets and SQL").isGreaterThan(200);
        assertThat(offenders).as("files that UPDATE, DELETE or TRUNCATE catalog_sensitivity_changes").isEmpty();
    }

    /**
     * nexus-u5m5g.3 (RDR-193 Part A1): the reconcile commit is a writer of {@code catalog_documents} that is not Java, so
     * the scans above cannot see it. Pinned here on the changeset's own text: it inserts documents in exactly one
     * statement, whose {@code sensitive} column is the OR of the staged flag and a sensitive tombstoned predecessor
     * (never a bare literal), it assigns the column nowhere else (an existing document is never flagged by the commit;
     * {@code sensitivity_pending} hands those to the audited route), and it inserts into the audit table, in one
     * statement, one row for each created row that was born flagged.
     */
    @Test
    void theReconcileCommitInsertsDocumentsOnce_assignsNoSensitiveFlagElse_andAuditsItsFlaggedRows() throws IOException {
        Path changeset = Path.of("src", "main", "resources", "db", "changelog", "catalog-048-reconcile.xml");
        String xml = stripXmlSqlComments(Files.readString(changeset));
        assertThat(xml).as("non-vacuity: the changeset text was read").contains("catalog_reconcile_commit").hasSizeGreaterThan(5000);

        Pattern insertDocs = Pattern.compile(String.join("\\s+", "(?i)insert", "into", "nexus\\.catalog_documents\\b"));
        Matcher d = insertDocs.matcher(xml);
        int docInserts = 0;
        while (d.find()) docInserts++;
        assertThat(docInserts).as("the commit inserts documents in exactly one statement").isEqualTo(1);
        assertThat(xml).as("and that statement's flag is the staged OR the inherited one, computed before it")
            .contains("0, '', '', 0, '', '', '', NULL::timestamptz, w.row_sens");
        assertThat(xml).as("row_sens is assigned from the staged flag and the predecessor's, and nowhere else")
            .contains("row_sens = (w.group_sens OR w.inherited)");
        assertThat(RAW_SET.matcher(xml).find()).as("no statement of the changeset assigns catalog_documents.sensitive").isFalse();

        Matcher a = Pattern.compile(String.join("\\s+", "(?i)insert", "into", "nexus\\.catalog_sensitivity_changes\\b")).matcher(xml);
        int auditInserts = 0;
        while (a.find()) auditInserts++;
        assertThat(auditInserts).as("one audit insert").isEqualTo(1);
        assertThat(xml).as("for the created rows born flagged, register_inherit naming the predecessor else register_set")
            .contains("WHERE w.ins AND w.created AND w.row_sens")
            .contains("'register_inherit predecessor=' || w.pred_tumbler")
            .contains("'register_set'");
    }

    @Test
    void theAuditScanSeesAWriterOutsideTheNamedMethodAndEveryUpdateDeleteSpelling() {
        String fake = """
            class X {
                void recordSensitivityChanges() { ctx.insertInto(CATALOG_SENSITIVITY_CHANGES, A).execute(); }
                void sneaky() { ctx.execute("INSERT INTO nexus.catalog_sensitivity_changes (tenant_id) VALUES (?)", t); }
                // catalog_sensitivity_changes in a comment is not a mention
            }
            """;
        assertThat(auditTableMentions(fake)).containsExactly("recordSensitivityChanges", "sneaky");
        for (String bad : List.of(
                "UPDATE nexus.catalog_sensitivity_changes SET reason = 'x'",
                "delete from catalog_sensitivity_changes",
                "DELETE FROM nexus.catalog_sensitivity_changes WHERE id = 1",
                "TRUNCATE TABLE nexus.catalog_sensitivity_changes",
                "ctx.deleteFrom(CATALOG_SENSITIVITY_CHANGES).execute()",
                "ctx.update(CATALOG_SENSITIVITY_CHANGES).set(A, b).execute()")) {
            assertThat(AUDIT_UPDATE_OR_DELETE.matcher(bad).find()).as(bad).isTrue();
        }
        assertThat(AUDIT_UPDATE_OR_DELETE.matcher("INSERT INTO nexus.catalog_sensitivity_changes (x) VALUES (1)").find())
            .isFalse();
    }
}
