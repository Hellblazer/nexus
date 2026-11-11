// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * What a {@code POST /v1/vectors/store-get} caller reads of each chunk (nexus-f3yqd): the optional request key
 * {@code include}, an array drawn from {@code documents}, {@code metadatas} and {@code embeddings}.
 *
 * <p>Absent (or JSON null) is {@link #FULL}: the envelope as it was, byte for byte, for a client that sends no
 * {@code include}. Present, the statement selects only the columns the caller named, so a metadata-only read
 * never detoasts {@code chunk_text}, and the envelope omits the key of a column it did not select (the Python
 * stub fills an absent key with an empty list; every in-repo caller reads only the keys it included, T2
 * nexus/audit-store-get-include-2026-11-10). {@code embeddings} is in the vocabulary because Chroma clients
 * name it, but this route never returned embeddings and still does not. {@code ids}, {@code count},
 * {@code chashes}, {@code spans}, {@code sensitives} and {@code source_uris} are not part of the projection:
 * the client's truncation check reads {@code count} and the MCP store_get output reads {@code sensitives}.
 *
 * <p>{@code spans} derive from three metadata keys. With {@code metadatas} excluded the statement therefore
 * selects a small object holding only those keys (the ones {@code PgVectorRepository.spanFromMeta} reads)
 * instead of the whole metadata column, so spans stay correct and nothing else of the metadata is read.
 */
public record StoreGetProjection(boolean documents, boolean metadatas) {

    /** No {@code include} on the request: every column, the envelope unchanged. */
    public static final StoreGetProjection FULL = new StoreGetProjection(true, true);

    private static final Set<String> VOCABULARY = Set.of("documents", "metadatas", "embeddings");

    /** The metadata keys {@code PgVectorRepository.spanFromMeta} reads. */
    static final List<String> SPAN_KEYS = List.of("chunk_text_hash", "line_start", "line_end");

    /**
     * Reads the request's {@code include} value.
     *
     * @param raw the body's {@code include} (null when the key is absent or JSON null)
     * @throws IllegalArgumentException when it is not an array of strings from the vocabulary
     */
    public static StoreGetProjection parse(Object raw) {
        if (raw == null) return FULL;
        if (!(raw instanceof List<?> items)) {
            throw new IllegalArgumentException("field 'include' must be an array of "
                + "\"documents\" / \"metadatas\" / \"embeddings\"");
        }
        boolean documents = false;
        boolean metadatas = false;
        for (Object item : items) {
            if (!(item instanceof String s) || !VOCABULARY.contains(s)) {
                throw new IllegalArgumentException("field 'include' has an unsupported value: " + item
                    + " (supported: documents, metadatas, embeddings)");
            }
            if (s.equals("documents")) documents = true;
            if (s.equals("metadatas")) metadatas = true;
        }
        return new StoreGetProjection(documents, metadatas);
    }

    /**
     * The select list of the by-id read, in this order: chash, chunk text (a typed NULL when not projected),
     * metadata (the whole column, or only the span keys when metadata is not projected), then {@code extra}
     * (the caller's window count).
     */
    public List<Field<?>> selectFields(DimTables.ChunkTable ch, Field<?>... extra) {
        List<Field<?>> out = new ArrayList<>(3 + extra.length);
        out.add(ch.chash());
        out.add(documents ? ch.chunkText() : DSL.castNull(String.class).as("text_not_selected"));
        out.add(metadatas ? ch.metadata() : spanKeys(ch.metadata()).as("span_keys"));
        out.addAll(List.of(extra));
        return out;
    }

    private static Field<JSONB> spanKeys(Field<JSONB> metadata) {
        var entries = SPAN_KEYS.stream()
            .map(k -> DSL.jsonEntry(k, DSL.jsonbGetAttribute(metadata, k)))
            .toArray(org.jooq.JSONEntry[]::new);
        return DSL.jsonbObject(entries);
    }
}
