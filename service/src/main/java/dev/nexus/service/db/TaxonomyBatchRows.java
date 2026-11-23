// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

/**
 * The batch map-extraction helpers of {@link TaxonomyRepository} (they mirror TaxonomyHandler's per-row parse), moved out of it unchanged so
 * that class stays under its file-size ceiling (RDR-229 P3.5c assembly, nexus-p2m1x.25).
 */
final class TaxonomyBatchRows {

    private TaxonomyBatchRows() { /* static */ }

    static String reqS(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null || v.toString().isEmpty()) throw new IllegalArgumentException("Missing required field: " + k);
        return v.toString();
    }
    static String optS(Map<String, Object> r, String k) {
        Object v = r.get(k);
        return v == null ? null : v.toString();
    }
    static Double optD(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null) return null;
        if (v instanceof Number n) return n.doubleValue();
        try { return Double.parseDouble(v.toString()); } catch (NumberFormatException e) { return null; }
    }
    static Long optL(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null) return null;
        if (v instanceof Number n) return n.longValue();
        try { return Long.parseLong(v.toString()); } catch (NumberFormatException e) { return null; }
    }
    static long reqL(Map<String, Object> r, String k) {
        Long l = optL(r, k);
        if (l == null) throw new IllegalArgumentException("Missing required field: " + k);
        return l;
    }
    static int optI(Map<String, Object> r, String k, int def) {
        Object v = r.get(k);
        if (v instanceof Number n) return n.intValue();
        if (v != null) { try { return Integer.parseInt(v.toString()); } catch (NumberFormatException ignored) { } }
        return def;
    }
}
