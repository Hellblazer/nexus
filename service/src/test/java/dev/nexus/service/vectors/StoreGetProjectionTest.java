// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-f3yqd: the {@code include} vocabulary of {@code store-get} and the statement it selects. The SQL
 * assertion renders the very select list {@link PgVectorRepository#get} runs, so "metadatas only reads no
 * chunk_text" is a fact about the statement, not about the envelope.
 */
class StoreGetProjectionTest {

    private static final DimTables.ChunkTable CH = DimTables.CHUNKS.get(1024);

    private static String sql(StoreGetProjection p) {
        return DSL.using(SQLDialect.POSTGRES).renderInlined(DSL.select(p.selectFields(CH)).from(CH.table()));
    }

    @Test
    void absentOrNull_isFull() {
        assertThat(StoreGetProjection.parse(null)).isSameAs(StoreGetProjection.FULL);
        assertThat(StoreGetProjection.FULL.documents()).isTrue();
        assertThat(StoreGetProjection.FULL.metadatas()).isTrue();
    }

    @Test
    void theVocabulary_picksTheColumns() {
        assertThat(StoreGetProjection.parse(List.of("documents", "metadatas")))
            .isEqualTo(new StoreGetProjection(true, true));
        assertThat(StoreGetProjection.parse(List.of("metadatas"))).isEqualTo(new StoreGetProjection(false, true));
        assertThat(StoreGetProjection.parse(List.of("documents"))).isEqualTo(new StoreGetProjection(true, false));
        assertThat(StoreGetProjection.parse(List.of())).isEqualTo(new StoreGetProjection(false, false));
    }

    @Test
    void embeddings_isAcceptedAndNeverReturned() {
        // Chroma vocabulary; this route never returned embeddings, and a client may still name them.
        assertThat(StoreGetProjection.parse(List.of("documents", "embeddings")))
            .isEqualTo(new StoreGetProjection(true, false));
    }

    @Test
    void anythingElse_isRefused() {
        assertThatThrownBy(() -> StoreGetProjection.parse(List.of("metadatas", "bogus")))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("bogus");
        assertThatThrownBy(() -> StoreGetProjection.parse("metadatas"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("include");
        assertThatThrownBy(() -> StoreGetProjection.parse(List.of(1)))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> StoreGetProjection.parse(Arrays.asList("documents", null)))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void full_selectsTheChunkTextAndWholeMetadata() {
        String s = sql(StoreGetProjection.FULL);
        assertThat(s).contains("\"chunk_text\"").contains("\"metadata\"");
    }

    @Test
    void metadatasOnly_neverNamesTheChunkTextColumn() {
        assertThat(sql(new StoreGetProjection(false, true))).doesNotContain("\"chunk_text\"");
        assertThat(sql(new StoreGetProjection(false, false))).doesNotContain("\"chunk_text\"");
    }

    @Test
    void metadatasExcluded_readsOnlyTheSpanKeys_neverTheWholeMetadata() {
        String s = sql(new StoreGetProjection(true, false));
        assertThat(s).contains("\"chunk_text\"");
        assertThat(s).contains("chunk_text_hash").contains("line_start").contains("line_end");
        // the whole-column select is gone: every reference to metadata is an attribute extraction
        assertThat(s.replace("\"metadata\" -> ", "").replace("\"metadata\"->", ""))
            .as("no bare metadata column in the select list").doesNotContain("\"metadata\"");
    }
}
