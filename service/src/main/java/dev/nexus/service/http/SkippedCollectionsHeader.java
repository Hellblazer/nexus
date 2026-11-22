// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * The value of {@link VectorHandler#SKIPPED_COLLECTIONS_HEADER} (nexus-5clu8). Since RDR-229 P3.6 a label may hold a
 * space, non-ASCII letters and (should the comma ruling be relaxed) a comma, none of which a raw header value carries
 * safely, so each name is form-encoded.
 */
public final class SkippedCollectionsHeader {

    private SkippedCollectionsHeader() {
    }

    /**
     * Each name form-encoded as UTF-8 ({@link URLEncoder}: a space is {@code +}, every byte outside letters, digits and
     * {@code .-*_} is {@code %XX}, so a comma inside a name is {@code %2C}), the names joined with a comma. A legacy
     * name of letters, digits, {@code _}, {@code -} and {@code .} encodes to itself. The client decodes each with
     * {@code urllib.parse.unquote_plus}.
     */
    public static String encode(List<String> skipped) {
        StringBuilder sb = new StringBuilder();
        for (String name : skipped) {
            if (sb.length() > 0) {
                sb.append(',');
            }
            sb.append(URLEncoder.encode(name, StandardCharsets.UTF_8));
        }
        return sb.toString();
    }
}
