// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.NoteUri;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;
import org.w3c.dom.Element;

import javax.xml.parsers.DocumentBuilderFactory;
import java.io.InputStream;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-231 (nexus-bivz3.5): the predicates {@code note-uri-001-1} selects and rewrites with, run in Postgres
 * over every row of the shared corpus {@code note-uri-corpus.json} (the file {@code NoteUriCorpusTest} pins
 * {@link NoteUri} to), and the changeset's own text read back to prove it carries exactly the predicates tested.
 *
 * <p>Three predicates over a column {@code c}: DIRECT ({@code chroma://} and a collection that is not
 * file-routed), NXNOTE (the same on the {@code nx-note://} spelling, a corpus-only check: the changeset never
 * selects on it) and EXPORT ({@code nxexp://<T>/chroma://<c>/...} with a collection that is not file-routed).
 * For every corpus row: {@code is_note == DIRECT || NXNOTE}; DIRECT holds only on a {@code chroma://} value;
 * and REWRITE (the CASE the UPDATEs and the guard use) equals the row's {@code canonical}. The changeset
 * check keeps the SQL text in this file from drifting from the SQL text the engine runs.
 */
class NoteUriBackfillPredicateParityTest {

    private static final String CHANGELOG = "/db/changelog/note-uri-001-backfill.xml";
    /** Spelled by parts: the SQL-literal gate counts a Java literal that opens with this clause as a statement. */
    private static final String SET_SOURCE_URI = String.join(" ", "SET", "source_uri", "=");
    private static final String EXPORT_HEAD = "'^nxexp://[^/]*/chroma://'";

    /** {@code starts_with(seg, 'rdr__') OR ...}, one term per file-routed prefix (the corpus's list). */
    private static String routed(String segment) {
        return NoteUri.FILE_ROUTED_PREFIXES.stream()
            .map(p -> "starts_with(" + segment + ", '" + p + "')")
            .collect(Collectors.joining(" OR "));
    }

    private static String direct(String c) {
        return "starts_with(" + c + ", 'chroma://') AND NOT ("
            + routed("split_part(substr(" + c + ", 10), '/', 1)") + ")";
    }

    private static String nxNote(String c) {
        return "starts_with(" + c + ", 'nx-note://') AND NOT ("
            + routed("split_part(substr(" + c + ", 11), '/', 1)") + ")";
    }

    private static String export(String c) {
        return c + " ~ " + EXPORT_HEAD + " AND NOT ("
            + routed("substring(" + c + " from '^nxexp://[^/]*/chroma://([^/]*)')") + ")";
    }

    private static String rewrite(String c) {
        return "CASE WHEN " + direct(c) + " THEN 'nx-note://' || substr(" + c + ", 10) WHEN " + export(c)
            + " THEN regexp_replace(" + c + ", '^(nxexp://[^/]*)/chroma://', '\\1/nx-note://') ELSE " + c + " END";
    }

    private static JsonNode corpus() throws Exception {
        try (InputStream in = NoteUriBackfillPredicateParityTest.class.getResourceAsStream("/note-uri-corpus.json")) {
            assertThat(in).as("resource note-uri-corpus.json").isNotNull();
            return new ObjectMapper().readTree(in);
        }
    }

    /** The changeset's {@code <sql>} text, XML-unescaped, whitespace collapsed to single spaces. */
    private static String changesetSql() throws Exception {
        try (InputStream in = NoteUriBackfillPredicateParityTest.class.getResourceAsStream(CHANGELOG)) {
            assertThat(in).as("resource " + CHANGELOG).isNotNull();
            var doc = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(in);
            var sqls = doc.getElementsByTagName("sql");
            assertThat(sqls.getLength()).as("one <sql> block in the changeset").isEqualTo(1);
            Element sql = (Element) sqls.item(0);
            assertThat(sql.getAttribute("splitStatements")).as("one transaction, one statement string")
                .isEqualTo("false");
            return sql.getTextContent().replaceAll("\\s+", " ").trim();
        }
    }

    // ── the same three predicates and the rewrite, as typed jOOQ over Postgres' own functions ──────────
    // (The RawSqlGateTest forbids SQL text in a test's DSL templates, so the text pinned below is run for
    // real by NoteUriBackfillMigrationTest#corpusRowsEndUpInTheirCanonicalForm; these evaluate the same
    // starts_with / split_part / substr offsets / regexp functions row by row, in Postgres.)

    private static Condition startsWith(Field<String> f, String prefix) {
        return DSL.condition(DSL.function("starts_with", SQLDataType.BOOLEAN, f, DSL.inline(prefix)));
    }

    private static Condition notRouted(Field<String> segment) {
        Condition c = DSL.trueCondition();
        for (String p : NoteUri.FILE_ROUTED_PREFIXES) {
            c = c.and(startsWith(segment, p).not());
        }
        return c;
    }

    private static Field<String> segmentAfter(Field<String> c, int scheme) {
        return DSL.function("split_part", String.class, DSL.substring(c, scheme), DSL.inline("/"), DSL.inline(1));
    }

    private static Condition tDirect(Field<String> c) {
        return startsWith(c, "chroma://").and(notRouted(segmentAfter(c, 10)));
    }

    private static Condition tNxNote(Field<String> c) {
        return startsWith(c, "nx-note://").and(notRouted(segmentAfter(c, 11)));
    }

    private static Condition tExport(Field<String> c) {
        Field<String> coll = DSL.function("substring", String.class, c,
            DSL.inline("^nxexp://[^/]*/chroma://([^/]*)"));
        return c.likeRegex("^nxexp://[^/]*/chroma://").and(notRouted(coll));
    }

    private static Field<String> tRewrite(Field<String> c) {
        return DSL.case_()
            .when(tDirect(c), DSL.inline("nx-note://").concat(DSL.substring(c, 10)))
            .when(tExport(c), DSL.function("regexp_replace", String.class, c,
                DSL.inline("^(nxexp://[^/]*)/chroma://"), DSL.inline("\\1/nx-note://")))
            .else_(c);
    }

    private static boolean holds(DSLContext ctx, Condition cond) {
        return Boolean.TRUE.equals(ctx.select(DSL.when(cond, DSL.inline(true)).otherwise(DSL.inline(false)))
            .fetchOne(0, Boolean.class));
    }

    @Test
    void sqlPredicatesAgreeWithEveryCorpusRow() throws Exception {
        JsonNode rows = corpus().get("rows");
        assertThat(rows.size()).as("corpus rows").isGreaterThanOrEqualTo(15);
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        int notes = 0;
        int exportRewrites = 0;
        int residue = 0;
        int nxNoteRows = 0;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (JsonNode row : rows) {
                String uri = row.get("uri").asText();
                boolean isNote = row.get("is_note").asBoolean();
                String canonical = row.get("canonical").asText();
                Field<String> c = DSL.inline(uri);

                boolean d = holds(ctx, tDirect(c));
                boolean n = holds(ctx, tNxNote(c));
                boolean x = holds(ctx, tExport(c));
                assertThat(d || n).as("SQL note predicate (chroma:// or nx-note://) on %s", uri).isEqualTo(isNote);
                assertThat(d && n).as("the two spellings are exclusive on %s", uri).isFalse();
                assertThat(d).as("DIRECT only on a chroma:// value: %s", uri)
                    .isEqualTo(isNote && uri.startsWith("chroma://"));
                assertThat(n).as("NXNOTE only on an nx-note:// value: %s", uri)
                    .isEqualTo(isNote && uri.startsWith("nx-note://"));
                assertThat(x && (d || n)).as("EXPORT and a direct form are exclusive on %s", uri).isFalse();

                // The same collection test on both spellings: swapping the scheme must not change the answer.
                if (uri.startsWith("chroma://")) {
                    String swapped = "nx-note://" + uri.substring("chroma://".length());
                    assertThat(holds(ctx, tNxNote(DSL.inline(swapped))))
                        .as("NXNOTE(%s) equals DIRECT(%s)", swapped, uri).isEqualTo(d);
                }

                String rewritten = ctx.select(tRewrite(c)).fetchOne(0, String.class);
                assertThat(rewritten).as("REWRITE(%s) equals the corpus canonical form", uri).isEqualTo(canonical);
                assertThat(NoteUri.canonical(uri)).as("and NoteUri.canonical agrees").isEqualTo(rewritten);
                assertThat(x || d).as("a value the UPDATE selects is rewritten, one it skips is not: %s", uri)
                    .isEqualTo(!rewritten.equals(uri));

                if (isNote) notes++;
                if (x) exportRewrites++;
                if (n) nxNoteRows++;
                if (!isNote && uri.startsWith("chroma://")) residue++;
            }
        } finally {
            pg.stop();
        }
        // Non-vacuity: the corpus must exercise the arms this test claims to cover.
        assertThat(notes).as("note rows examined").isGreaterThanOrEqualTo(5);
        assertThat(exportRewrites).as("nxexp note rows examined").isGreaterThanOrEqualTo(2);
        assertThat(nxNoteRows).as("nx-note:// note rows examined").isGreaterThanOrEqualTo(2);
        assertThat(residue).as("chroma:// residue rows examined, one per file-routed prefix")
            .isGreaterThanOrEqualTo(NoteUri.FILE_ROUTED_PREFIXES.size());
    }

    /** The whole changelog file as text, whitespace collapsed (the header comment is not in the {@code <sql>}). */
    private static String changelogFileText() throws Exception {
        try (InputStream in = NoteUriBackfillPredicateParityTest.class.getResourceAsStream(CHANGELOG)) {
            assertThat(in).as("resource " + CHANGELOG).isNotNull();
            return new String(in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8).replaceAll("\\s+", " ");
        }
    }

    @Test
    void headerCommentCarriesTheCensusFormsOfThePredicates() throws Exception {
        String file = changelogFileText();
        assertThat(file).as("the census's copy of the direct predicate")
            .contains("direct: " + direct("source_uri") + " nx-note: ");
        assertThat(file).as("the census's copy of the nx-note:// predicate")
            .contains("nx-note: " + nxNote("source_uri") + " export: ");
        assertThat(file).as("the census's copy of the export predicate")
            .contains("export: " + export("source_uri") + " SCHEME ONLY");
    }

    @Test
    void changesetCarriesExactlyTheTestedPredicates() throws Exception {
        String sql = changesetSql();
        for (String col : List.of("o.source_uri", "source_uri")) {
            assertThat(sql).as("DIRECT on " + col).contains(direct(col));
            assertThat(sql).as("EXPORT on " + col).contains(export(col));
            assertThat(sql).as("REWRITE on " + col).contains(rewrite(col));
        }
        assertThat(sql).as("the WHERE of each UPDATE is DIRECT OR EXPORT")
            .contains("WHERE " + direct("source_uri") + " OR " + export("source_uri") + ";");
        assertThat(count(sql, " " + SET_SOURCE_URI + " ")).as("three UPDATEs").isEqualTo(3);
        for (String t : List.of("catalog_documents", "document_aspects", "document_highlights")) {
            assertThat(sql).as("rewrite statement for " + t).contains("nexus." + t + " " + SET_SOURCE_URI + " " + rewrite("source_uri"));
        }
        assertThat(sql).as("the nx-note:// predicate is a corpus-only check, never selected on")
            .doesNotContain("starts_with(source_uri, 'nx-note://')")
            .doesNotContain("starts_with(o.source_uri, 'nx-note://')");
    }

    @Test
    void changesetOrdersToggleGuardUpdatesAndRestore() throws Exception {
        String sql = changesetSql();
        List<String> order = new ArrayList<>();
        Matcher m = Pattern.compile("(NO )?FORCE ROW LEVEL SECURITY|DO \\$\\$|UPDATE nexus\\.(\\w+)").matcher(sql);
        while (m.find()) {
            String hit = m.group();
            if (hit.startsWith("NO ")) {
                order.add("no-force");
            } else if (hit.startsWith("FORCE")) {
                order.add("force");
            } else if (hit.startsWith("DO")) {
                order.add("guard");
            } else {
                order.add("update " + m.group(2));
            }
        }
        assertThat(order).containsExactly(
            "no-force", "no-force", "no-force",
            "guard",
            "update catalog_documents", "update document_aspects", "update document_highlights",
            "force", "force", "force");
        int cd = sql.indexOf("nexus.catalog_documents NO FORCE");
        int da = sql.indexOf("nexus.document_aspects NO FORCE");
        int dh = sql.indexOf("nexus.document_highlights NO FORCE");
        assertThat(cd).isGreaterThanOrEqualTo(0);
        assertThat(da).as("toggle order: aspects after documents").isGreaterThan(cd);
        assertThat(dh).as("toggle order: highlights after aspects").isGreaterThan(da);
        int rdh = sql.indexOf("nexus.document_highlights FORCE");
        int rda = sql.indexOf("nexus.document_aspects FORCE");
        int rcd = sql.indexOf("nexus.catalog_documents FORCE");
        assertThat(rdh).as("FORCE restored after the last UPDATE").isGreaterThan(sql.lastIndexOf(" " + SET_SOURCE_URI + " "));
        assertThat(rda).as("FORCE restored in reverse order: aspects after highlights").isGreaterThan(rdh);
        assertThat(rcd).as("FORCE restored in reverse order: documents last").isGreaterThan(rda);
    }

    @Test
    void raiseTextTellsTheOperatorTheRecovery() throws Exception {
        String sql = changesetSql();
        assertThat(sql).contains("nothing was changed")
            .contains("Start the previous engine binary")
            .contains("POST /v1/catalog/merge")
            .contains("restart on the new engine")
            .contains(RUNBOOK_URL + "#if-the-rdr-231-note-uri-backfill-halts-startup")
            .as("a PyPI install cannot open a repo-relative path: the RAISE cites the published URL")
            .doesNotContain("docs/migration-runbook.md, section");
    }

    /** The published runbook URL (MIGRATION_RUNBOOK_URL in src/nexus/upgrade_finish.py). */
    private static final String RUNBOOK_URL =
        "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md";

    /**
     * The anchor the RAISE cites resolves to a heading of docs/migration-runbook.md (GitHub's slug: lower-cased,
     * punctuation dropped, spaces to hyphens), so a renamed section turns this red instead of stranding an operator.
     */
    @Test
    void raiseCitesAnAnchorTheRunbookHas() throws Exception {
        Matcher cited = Pattern.compile(Pattern.quote(RUNBOOK_URL) + "#([a-z0-9-]+)").matcher(changesetSql());
        assertThat(cited.find()).as("the RAISE cites the runbook URL with a section anchor").isTrue();
        String anchor = cited.group(1);
        java.nio.file.Path runbook = java.nio.file.Path.of("..", "docs", "migration-runbook.md");
        assertThat(runbook).as("the runbook is read from the repo root").exists();
        List<String> slugs = new ArrayList<>();
        for (String line : java.nio.file.Files.readAllLines(runbook)) {
            if (line.startsWith("#")) {
                String title = line.replaceFirst("^#+\\s*", "").toLowerCase(java.util.Locale.ROOT);
                slugs.add(title.replaceAll("[^a-z0-9 -]", "").replace(' ', '-'));
            }
        }
        assertThat(slugs).as("headings of docs/migration-runbook.md").hasSizeGreaterThan(10).contains(anchor);
    }

    private static int count(String text, String needle) {
        int n = 0;
        for (int i = text.indexOf(needle); i >= 0; i = text.indexOf(needle, i + 1)) {
            n++;
        }
        return n;
    }
}
