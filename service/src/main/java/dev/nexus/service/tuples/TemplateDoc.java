// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import java.util.List;
import java.util.Map;

/**
 * The optional {@code doc} block of a tuple template (RDR-239 Technical Design item 1, bead
 * nexus-mdbds.2.10): what the subspace is for, who writes and reads it, what its fields and
 * body mean, and one example post. Documentation only: nothing here restricts {@code out()}.
 *
 * <p>Only {@code summary} is required. {@code writers}, {@code readers}, {@code body},
 * {@code example} and {@code seeAlso} are {@code null} when the block omits them; an empty
 * {@code writers} list is a different statement ("no writer today") and stays an empty list.
 *
 * @param fields keyed by a declared key or dimension name; each value is a {@code String}
 *               (one-line description) or a {@code Map<String,String>} (value to meaning),
 *               in file order
 * @param body   a {@code String} (one-line description) or a {@code Map<String,String>}
 *               (JSON field to meaning), or {@code null}
 */
public record TemplateDoc(
        String summary,
        List<String> writers,
        List<String> readers,
        Map<String, Object> fields,
        Object body,
        Example example,
        List<String> seeAlso) {

    /** Copies every collection immutably, file order kept (the registry snapshot is shared
     *  across requests; nothing downstream may mutate it). */
    public TemplateDoc {
        writers = writers == null ? null : List.copyOf(writers);
        readers = readers == null ? null : List.copyOf(readers);
        seeAlso = seeAlso == null ? null : List.copyOf(seeAlso);
        if (fields != null) {
            Map<String, Object> copy = new java.util.LinkedHashMap<>();
            fields.forEach((k, v) -> copy.put(k, immutableDescription(v)));
            fields = java.util.Collections.unmodifiableMap(copy);
        }
        body = body == null ? null : immutableDescription(body);
    }

    /** A description is a String or a value map; a map is copied in file order. */
    @SuppressWarnings("unchecked")
    private static Object immutableDescription(Object o) {
        return o instanceof Map
                ? java.util.Collections.unmodifiableMap(new java.util.LinkedHashMap<>((Map<String, String>) o))
                : o;
    }

    /** Bytes allowed in one description: a summary, a writers/readers/see_also string, a
     *  fields or body description (S4 Q3). */
    public static final int MAX_TEXT_BYTES = 300;

    /** Bytes allowed in a whole doc block as the parsed mapping reads: every key and scalar. */
    public static final int MAX_BLOCK_BYTES = 4096;

    /**
     * One tuple literal. {@code keys} and {@code dims} are strings even when the YAML reader
     * returned a number or a boolean; {@code nonce} and {@code body} are {@code null} when absent.
     */
    public record Example(String subspace, String nonce, Map<String, String> keys,
                          Map<String, String> dims, String body) {
        public Example {
            keys = keys == null ? Map.of() : java.util.Collections.unmodifiableMap(new java.util.LinkedHashMap<>(keys));
            dims = dims == null ? Map.of() : java.util.Collections.unmodifiableMap(new java.util.LinkedHashMap<>(dims));
        }
    }
}
