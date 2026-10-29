// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.ArrayList;
import java.util.List;

/**
 * Which rows of one document's manifest {@code POST /v1/catalog/manifest/get_many} returns when the request
 * names {@code max_rows_per_doc} (nexus-jypmj.6).
 *
 * <p>A document within the limit is returned whole. A longer one is returned as an EVEN sample of exactly
 * {@code limit} rows that includes its first and last row (for {@code limit >= 2}): index {@code i} of the
 * sample is row {@code floor(i * (count - 1) / (limit - 1))}. That is the sample the client's water-fill
 * wants (a spread over the document, never a prefix, so a long document's evidence is not its opening
 * pages), and with {@code count > limit} the step exceeds one, so the indexes are strictly increasing and
 * distinct. A limit of one is the first row. Integer arithmetic throughout: no rounding mode to disagree on.
 */
public final class ManifestRowSample {

    private ManifestRowSample() {}

    /** Zero-based row indexes (positions in the ordered manifest) to return for a document of {@code count} rows. */
    public static List<Integer> indexes(int count, int limit) {
        if (limit < 1) throw new IllegalArgumentException("limit must be positive");
        List<Integer> out = new ArrayList<>(Math.min(count, limit));
        if (count <= limit) {
            for (int i = 0; i < count; i++) out.add(i);
            return out;
        }
        if (limit == 1) {
            out.add(0);
            return out;
        }
        for (int i = 0; i < limit; i++) {
            out.add((int) (((long) i * (count - 1)) / (limit - 1)));
        }
        return out;
    }

    /** The rows of one ordered manifest at {@link #indexes}. */
    public static <T> List<T> of(List<T> rows, int limit) {
        if (rows.size() <= limit) return rows;
        List<T> out = new ArrayList<>(limit);
        for (int i : indexes(rows.size(), limit)) out.add(rows.get(i));
        return out;
    }
}
