/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-231 (nexus-bivz3.1): {@link NoteUri} answers the shared corpus
 * {@code note-uri-corpus.json}, the file the client and SQL predicate tests read too. The
 * prefix list must equal the corpus's, and every listed prefix must have a residue row, so a
 * new file-routed prefix cannot join one side without the corpus noticing.
 */
class NoteUriCorpusTest {

    private static JsonNode corpus() throws IOException {
        try (InputStream in = NoteUriCorpusTest.class.getResourceAsStream("/note-uri-corpus.json")) {
            assertThat(in).as("resource note-uri-corpus.json").isNotNull();
            return new ObjectMapper().readTree(in);
        }
    }

    @Test
    void prefixListEqualsTheCorpusList() throws IOException {
        List<String> fromFile = new ArrayList<>();
        corpus().get("file_routed_prefixes").forEach(n -> fromFile.add(n.asText()));
        assertThat(NoteUri.FILE_ROUTED_PREFIXES).containsExactlyElementsOf(fromFile);
    }

    @Test
    void everyPrefixHasAChromaResidueRow() throws IOException {
        JsonNode c = corpus();
        for (JsonNode p : c.get("file_routed_prefixes")) {
            boolean found = false;
            for (JsonNode row : c.get("rows")) {
                if (row.get("uri").asText().startsWith(NoteUri.LEGACY + p.asText())
                        && !row.get("is_note").asBoolean()) {
                    found = true;
                }
            }
            assertThat(found).as("residue row for prefix " + p.asText()).isTrue();
        }
    }

    @Test
    void isNoteAndCanonicalMatchEveryRow() throws IOException {
        JsonNode rows = corpus().get("rows");
        assertThat(rows.size()).as("corpus rows").isGreaterThanOrEqualTo(15);
        for (JsonNode row : rows) {
            String uri = row.get("uri").asText();
            assertThat(NoteUri.isNote(uri)).as("isNote(" + uri + ")").isEqualTo(row.get("is_note").asBoolean());
            assertThat(NoteUri.canonical(uri)).as("canonical(" + uri + ")").isEqualTo(row.get("canonical").asText());
        }
    }

    @Test
    void nullIsNotANoteAndCanonicalPassesItThrough() {
        assertThat(NoteUri.isNote(null)).isFalse();
        assertThat(NoteUri.canonical(null)).isNull();
    }

    @Test
    void moveToRewritesOnlyTheNoteSpellingOfANoteCollection() {
        Field<String> uri = DSL.field(DSL.name("source_uri"), SQLDataType.CLOB);
        String sql = DSL.using(SQLDialect.POSTGRES).renderInlined(NoteUri.moveTo(uri, "knowledge__a", "knowledge__b"));
        // P2.10: the chroma:// arm is gone; residue and legacy-spelled values come back unchanged.
        assertThat(sql).contains("case")
            .contains("'nx-note://knowledge__a/'").contains("'nx-note://knowledge__b/'")
            .doesNotContain("chroma://")
            .contains("starts_with")
            .doesNotContain("like")
            .contains("else \"source_uri\"");
    }

    @Test
    void moveToOutOfAFileRoutedCollectionIsTheIdentity() {
        Field<String> uri = DSL.field(DSL.name("source_uri"), SQLDataType.CLOB);
        for (String prefix : NoteUri.FILE_ROUTED_PREFIXES) {
            String coll = prefix + "x__minilm-l6-v2-384__v1";
            String sql = DSL.using(SQLDialect.POSTGRES).renderInlined(NoteUri.moveTo(uri, coll, "plainlabel"));
            assertThat(sql).as(coll).isEqualTo("\"source_uri\"");
            assertThat(NoteUri.moveTo("nx-note://" + coll + "/a.md", coll, "plainlabel")).as(coll)
                .isEqualTo("nx-note://" + coll + "/a.md");
            assertThat(NoteUri.moveTo("chroma://" + coll + "/a.md", coll, "plainlabel")).as(coll)
                .isEqualTo("chroma://" + coll + "/a.md");
        }
    }

    @Test
    void moveToFromTheRowsCollectionReadsTheColumnAndGuardsEveryFileRoutedPrefix() {
        Field<String> uri = DSL.field(DSL.name("source_uri"), SQLDataType.CLOB);
        Field<String> coll = DSL.field(DSL.name("physical_collection"), SQLDataType.CLOB);
        String sql = DSL.using(SQLDialect.POSTGRES).renderInlined(NoteUri.moveTo(uri, coll, "knowledge__b"));
        assertThat(sql).contains("\"physical_collection\"")
            .contains("'nx-note://knowledge__b/'")
            .doesNotContain("chroma://")
            .contains("char_length(\"physical_collection\")")
            .doesNotContain("like")
            .contains("else \"source_uri\"");
        for (String prefix : NoteUri.FILE_ROUTED_PREFIXES) {
            assertThat(sql).as("guard for " + prefix)
                .contains("starts_with(\"physical_collection\", '" + prefix + "')");
        }
    }

    @Test
    void moveToOnOneValueRewritesOnlyANoteOfANoteCollection() {
        assertThat(NoteUri.moveTo("nx-note://knowledge__a/t", "knowledge__a", "knowledge__b"))
            .isEqualTo("nx-note://knowledge__b/t");
        assertThat(NoteUri.moveTo("chroma://knowledge__a/t", "knowledge__a", "knowledge__b"))
            .as("the legacy spelling is not moved").isEqualTo("chroma://knowledge__a/t");
        assertThat(NoteUri.moveTo("file:///a/t", "knowledge__a", "knowledge__b")).isEqualTo("file:///a/t");
        assertThat(NoteUri.moveTo((String) null, "knowledge__a", "knowledge__b")).isNull();
    }

    @Test
    void namesCollectionSelectsExactlyWhatMoveToRewrites() {
        Field<String> uri = DSL.field(DSL.name("source_uri"), SQLDataType.CLOB);
        var dsl = DSL.using(SQLDialect.POSTGRES);
        String note = dsl.renderInlined(NoteUri.namesCollection(uri, "knowledge__a"));
        assertThat(note).contains("starts_with").contains("'nx-note://knowledge__a/'").doesNotContain("chroma://");
        for (String prefix : NoteUri.FILE_ROUTED_PREFIXES) {
            String coll = prefix + "x__minilm-l6-v2-384__v1";
            assertThat(dsl.renderInlined(NoteUri.namesCollection(uri, coll))).as(coll).isEqualTo("false");
        }
    }

    /**
     * RDR-229 P2.9 retires {@code isFileRoutedCollection} in the same engine cut: {@code moveTo} and {@code namesCollection}
     * classify the old collection as the URI's segment (what {@code isNote} classifies), and only the stale-note repair's
     * live-collection test still calls it.
     */
    @Test
    void noLiveCodeTestsACollectionNameForAFileRoutedPrefix() throws IOException {
        // RDR-229 P2.9 retired NoteUri.isFileRoutedCollection (the stale-note repair reads the registry row's content
        // type); P2.10's URI rules classify a URI SEGMENT (isNote, namesCollection, moveTo), never a collection's name.
        java.nio.file.Path main = java.nio.file.Path.of("src", "main", "java");
        List<String> callers = new ArrayList<>();
        int scanned = 0;
        try (var files = java.nio.file.Files.walk(main)) {
            for (java.nio.file.Path f : (Iterable<java.nio.file.Path>) files.filter(x -> x.toString().endsWith(".java"))::iterator) {
                scanned++;
                int n = 0;
                for (String line : java.nio.file.Files.readAllLines(f)) {
                    String t = line.strip();
                    boolean prose = t.startsWith("*") || t.startsWith("//") || t.startsWith("/*");
                    if (!prose && line.contains("isFileRoutedCollection(")) n++;
                }
                if (n > 0) callers.add(f.getFileName() + "=" + n);
            }
        }
        assertThat(scanned).as("non-vacuity: the scan read the main sources").isGreaterThan(100);
        assertThat(callers).as("a name-prefix file-routed test in live code").isEmpty();
    }
}
