// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

/** The optional-field readers of a request map, moved out of CatalogRepository (RDR-229 P3.5c, to keep it under its size ceiling). */
final class CatalogOptFields {

    private CatalogOptFields() {
    }

    /** An optional string field: absent or null is null, a non-string is refused. */
    static String optString(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v == null) return null;
        if (v instanceof String sv) return sv;
        throw new IllegalArgumentException("'" + k + "' must be a string");
    }

    /** An optional whole-number field: absent or null is null, anything but an integral number is refused. */
    static Integer optInt(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v == null) return null;
        if (v instanceof Integer || v instanceof Long || v instanceof Short || v instanceof Byte) return ((Number) v).intValue();
        throw new IllegalArgumentException("'" + k + "' must be a whole number");
    }

    static Double optDouble(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v == null) return null;
        if (v instanceof Number n) return n.doubleValue();
        throw new IllegalArgumentException("'" + k + "' must be a number");
    }

    static Boolean optBool(Map<String, Object> m, String k) {
        Object v = m.get(k);
        if (v == null) return null;
        if (v instanceof Boolean b) return b;
        throw new IllegalArgumentException("'" + k + "' must be true or false");
    }
}
