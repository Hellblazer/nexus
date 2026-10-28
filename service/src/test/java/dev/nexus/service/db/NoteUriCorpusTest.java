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
    void moveToRewritesBothNoteSpellingsAndNothingElse() {
        Field<String> uri = DSL.field(DSL.name("source_uri"), SQLDataType.CLOB);
        String sql = DSL.using(SQLDialect.POSTGRES).renderInlined(NoteUri.moveTo(uri, "knowledge__a", "knowledge__b"));
        assertThat(sql).contains("case")
            .contains("'nx-note://knowledge__a/'").contains("'nx-note://knowledge__b/'")
            .contains("'chroma://knowledge__a/'").contains("'chroma://knowledge__b/'")
            .contains("starts_with")
            .doesNotContain("like")
            .contains("else \"source_uri\"");
    }

    @Test
    void moveToFromTheRowsCollectionReadsTheColumn() {
        Field<String> uri = DSL.field(DSL.name("source_uri"), SQLDataType.CLOB);
        Field<String> coll = DSL.field(DSL.name("physical_collection"), SQLDataType.CLOB);
        String sql = DSL.using(SQLDialect.POSTGRES).renderInlined(NoteUri.moveTo(uri, coll, "knowledge__b"));
        assertThat(sql).contains("\"physical_collection\"")
            .contains("'nx-note://knowledge__b/'").contains("'chroma://knowledge__b/'")
            .contains("char_length(\"physical_collection\")")
            .doesNotContain("like")
            .contains("else \"source_uri\"");
    }
}
