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
 *       anywhere under {@code service/src/main/java} assigns the one-way OR constant, a literal {@code true}, or
 *       {@code sensitive} inside {@code setSensitivity} (the audited route), and its enclosing method also calls
 *       {@code recordSensitivityChanges}. The OR constant's own definition is pinned to
 *       {@code stored OR excluded}.</li>
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

    /** The OR constant's declaration must read {@code stored OR excluded}. */
    private static final Pattern OR_CONSTANT_DEF = Pattern.compile(
        "EX_DOC_SENSITIVE_OR\\s*=\\s*DSL\\.field\\(DSL\\.condition\\(CATALOG_DOCUMENTS\\.SENSITIVE\\)"
            + "\\.or\\(DSL\\.condition\\(DSL\\.excluded\\(CATALOG_DOCUMENTS\\.SENSITIVE\\)\\)\\)\\)");

    /** Source with block and line comments blanked to spaces (offsets and newlines kept). */
    static String stripJavaComments(String src) {
        StringBuilder out = new StringBuilder(src);
        Matcher m = Pattern.compile("(?s)/\\*.*?\\*/|(?m)//[^\\n]*").matcher(src);
        while (m.find()) {
            for (int i = m.start(); i < m.end(); i++) if (out.charAt(i) != '\n') out.setCharAt(i, ' ');
        }
        return out.toString();
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
            boolean allowedValue = a.value().equals("EX_DOC_SENSITIVE_OR")
                || a.value().equals("true")
                || (a.value().equals("sensitive") && a.method().equals("setSensitivity"));
            if (!allowedValue) bad.add(a.method() + " assigns " + a.value()
                + " (allowed: EX_DOC_SENSITIVE_OR, true, or `sensitive` in setSensitivity)");
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
    void everyAssignmentOfTheSensitiveColumnIsTheOrConstantATrueOrTheAuditedRoute() throws IOException {
        List<Assignment> all = new ArrayList<>();
        int files = 0;
        for (Path f : javaFiles(MAIN_JAVA)) {
            files++;
            all.addAll(assignments(Files.readString(f)));
        }
        assertThat(files).as("non-vacuity: the scan walked the main source tree").isGreaterThan(50);
        assertThat(all).as("non-vacuity: the five known assignment sites (3 OR, raiseSensitivity, setSensitivity); %s", all)
            .hasSizeGreaterThanOrEqualTo(5);
        assertThat(assignmentViolations(all)).as("assignments of catalog_documents.sensitive: %s", all).isEmpty();
        Set<String> orMethods = new TreeSet<>();
        for (Assignment a : all) if (a.value().equals("EX_DOC_SENSITIVE_OR")) orMethods.add(a.method());
        assertThat(orMethods)
            .as("the OR constant is assigned by the three ON CONFLICT writers")
            .containsExactlyInAnyOrder("upsertDocument", "importDocumentsBatch", "doImportDocument");
    }

    @Test
    void theOrConstantIsStoredOrExcluded() throws IOException {
        String src = stripJavaComments(Files.readString(REPOSITORY));
        assertThat(OR_CONSTANT_DEF.matcher(src).find())
            .as("EX_DOC_SENSITIVE_OR must be DSL.field(condition(SENSITIVE).or(condition(excluded(SENSITIVE))))")
            .isTrue();
    }

    @Test
    void theAssignmentScanSeesALoweringAssignmentInEverySpelling() {
        String fake = """
            class X {
                void upsertDocument() {
                    ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, EX_DOC_SENSITIVE_OR).execute();
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
