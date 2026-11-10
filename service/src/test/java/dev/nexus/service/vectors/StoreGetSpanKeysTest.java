// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.AbstractMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@link StoreGetProjection#SPAN_KEYS} is the list of metadata keys the by-id read selects when
 * {@code metadatas} is excluded, and {@link PgVectorRepository#spanFromMeta} is what turns them into a span. They
 * are two hand-kept lists of one fact, so this test couples them: it runs {@code spanFromMeta} over every span
 * shape with a map that records each key read, and requires the keys read to be exactly {@code SPAN_KEYS}. A
 * fourth key added to {@code spanFromMeta} fails here, before it can give a wrong span under
 * {@code include=["documents"]} (the statement would not have selected it). One shared constant was not
 * chosen: {@code spanFromMeta} reads each key at its own type with its own rule, and a loop over a key list would
 * hide those rules.
 */
class StoreGetSpanKeysTest {

    /** A map that answers from a backing map and records every key asked of it, however it is asked. */
    private static final class Recording extends AbstractMap<String, Object> {
        final Map<String, Object> backing;
        final Set<String> read = new LinkedHashSet<>();

        Recording(Map<String, Object> backing) { this.backing = backing; }

        @Override public Object get(Object key) { read.add(String.valueOf(key)); return backing.get(key); }
        @Override public boolean containsKey(Object key) { read.add(String.valueOf(key)); return backing.containsKey(key); }
        @Override public Object getOrDefault(Object key, Object d) { read.add(String.valueOf(key)); return backing.getOrDefault(key, d); }
        @Override public Set<Entry<String, Object>> entrySet() {
            throw new AssertionError("spanFromMeta iterated the whole map; the projection reads only named keys");
        }
    }

    private static final String HASH =
        "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899";

    @Test
    void theKeysSpanFromMetaReads_areExactlyTheProjectionsSpanKeys() {
        Set<String> read = new LinkedHashSet<>();
        // one map per span shape: chash form, line range, line range with line_end 0, bad hash falling through,
        // and no positional data at all (a read that reaches every branch reads every key)
        for (Map<String, Object> shape : java.util.List.<Map<String, Object>>of(
                Map.of("chunk_text_hash", HASH),
                Map.of("line_start", 5, "line_end", 15),
                Map.of("line_start", 5, "line_end", 0),
                Map.of("chunk_text_hash", "not-hex", "line_start", 1, "line_end", 2),
                Map.of())) {
            var rec = new Recording(shape);
            PgVectorRepository.spanFromMeta(rec);
            read.addAll(rec.read);
        }
        assertThat(read)
            .as("every key spanFromMeta reads must be a key the metadata-excluded statement selects, and none dead")
            .containsExactlyInAnyOrderElementsOf(StoreGetProjection.SPAN_KEYS);
    }

    @Test
    void theSpansAreTheSame_whetherTheWholeMetadataOrOnlyTheSpanKeysIsRead() {
        Map<String, Object> whole = Map.of("title", "T", "extra", 1, "chunk_text_hash", HASH, "line_start", 1,
                                           "line_end", 2);
        Map<String, Object> keysOnly = Map.of("chunk_text_hash", HASH, "line_start", 1, "line_end", 2);
        assertThat(PgVectorRepository.spanFromMeta(keysOnly)).isEqualTo(PgVectorRepository.spanFromMeta(whole))
            .isEqualTo("chash:" + HASH);
    }
}
