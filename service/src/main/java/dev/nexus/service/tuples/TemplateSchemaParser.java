// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * Structural and field-level validation of a parsed template document
 * (RDR-205 §Technical Design "Registry", bead nexus-em75s.3). Every
 * failure names the source descriptor and the offending field.
 */
final class TemplateSchemaParser {

    private TemplateSchemaParser() {
    }

    private static final Set<String> TOP_LEVEL_FIELDS = Set.of(
            "name", "keys", "dimensions", "id_from", "id_dims", "take", "retention_seconds");

    private static final Set<String> DIMENSION_FIELDS = Set.of("type", "values", "required");

    private static final Set<String> TAKE_FIELDS =
            Set.of("enabled", "default_lease_seconds", "max_lease_seconds", "max_attempts");

    static TemplateSchema parse(String source, Map<String, Object> doc) {
        rejectUnknownKeys(source, "", doc.keySet(), TOP_LEVEL_FIELDS);

        String name = requireString(source, doc, "name");
        List<String> nameSegments = parseNameSegments(source, name);

        List<String> keys = requireStringList(source, doc, "keys");
        if (keys.isEmpty()) {
            throw breach(source, "keys", "must be a non-empty list");
        }
        for (String k : keys) {
            if (k.isBlank()) {
                throw breach(source, "keys", "must not contain a blank entry");
            }
        }

        Map<String, TemplateSchema.Dimension> dimensions = parseDimensions(source, doc.get("dimensions"));

        String idFromWire = requireString(source, doc, "id_from");
        TemplateSchema.IdFrom idFrom = TemplateSchema.IdFrom.fromWire(idFromWire);
        if (idFrom == null) {
            throw breach(source, "id_from",
                    "must be one of keys, keys+nonce, keys+body (got '" + idFromWire + "')");
        }

        List<String> idDims = optionalStringList(source, doc, "id_dims");
        for (String dim : idDims) {
            TemplateSchema.Dimension d = dimensions.get(dim);
            if (d == null) {
                throw breach(source, "id_dims", "names undefined dimension '" + dim + "'");
            }
            if (!d.required()) {
                throw breach(source, "id_dims",
                        "dimension '" + dim + "' enters the id and must be declared required: true");
            }
        }

        TemplateSchema.Take take = parseTake(source, doc.get("take"));
        long retentionSeconds = requirePositiveLong(source, doc, "retention_seconds");

        return new TemplateSchema(name, nameSegments, keys, dimensions, idFrom, idDims, take, retentionSeconds);
    }

    private static List<String> parseNameSegments(String source, String name) {
        String[] raw = name.split("/", -1);
        List<String> segments = new ArrayList<>();
        for (String seg : raw) {
            if (seg.isBlank()) {
                throw breach(source, "name", "must not contain an empty path segment ('" + name + "')");
            }
            boolean opensParam = seg.startsWith("<");
            boolean closesParam = seg.endsWith(">");
            if (opensParam || closesParam) {
                if (!(opensParam && closesParam && seg.length() > 2)) {
                    throw breach(source, "name", "malformed parameter segment '" + seg + "' in '" + name + "'");
                }
            }
            segments.add(seg);
        }
        return segments;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, TemplateSchema.Dimension> parseDimensions(String source, Object raw) {
        if (raw == null) {
            return Map.of();
        }
        if (!(raw instanceof Map)) {
            throw breach(source, "dimensions", "must be a mapping");
        }
        Map<String, Object> map = (Map<String, Object>) raw;
        Map<String, TemplateSchema.Dimension> out = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : map.entrySet()) {
            String dimName = entry.getKey();
            Object value = entry.getValue();
            if (!(value instanceof Map)) {
                throw breach(source, "dimensions." + dimName, "must be a mapping");
            }
            Map<String, Object> dimDoc = (Map<String, Object>) value;
            rejectUnknownKeys(source, "dimensions." + dimName + ".", dimDoc.keySet(), DIMENSION_FIELDS);

            Object typeRaw = dimDoc.get("type");
            if (!(typeRaw instanceof String) || ((String) typeRaw).isBlank()) {
                throw breach(source, "dimensions." + dimName + ".type", "is required and must be a non-blank string");
            }

            List<String> values = null;
            if (dimDoc.containsKey("values")) {
                Object valuesRaw = dimDoc.get("values");
                if (!(valuesRaw instanceof List<?> list) || list.isEmpty()) {
                    throw breach(source, "dimensions." + dimName + ".values", "must be a non-empty list");
                }
                values = new ArrayList<>();
                for (Object v : list) {
                    if (!(v instanceof String s) || s.isBlank()) {
                        throw breach(source, "dimensions." + dimName + ".values",
                                "must contain only non-blank strings");
                    }
                    values.add(s);
                }
            }

            boolean required = false;
            if (dimDoc.containsKey("required")) {
                Object reqRaw = dimDoc.get("required");
                if (!(reqRaw instanceof Boolean bool)) {
                    throw breach(source, "dimensions." + dimName + ".required", "must be a boolean");
                } else {
                    required = bool;
                }
            }

            out.put(dimName, new TemplateSchema.Dimension((String) typeRaw, values, required));
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    private static TemplateSchema.Take parseTake(String source, Object raw) {
        if (!(raw instanceof Map)) {
            throw breach(source, "take", "is required and must be a mapping");
        }
        Map<String, Object> doc = (Map<String, Object>) raw;
        rejectUnknownKeys(source, "take.", doc.keySet(), TAKE_FIELDS);

        Object enabledRaw = doc.get("enabled");
        if (!(enabledRaw instanceof Boolean enabled)) {
            throw breach(source, "take.enabled", "is required and must be a boolean");
        } else {
            Long defaultLease = optionalPositiveLong(source, doc, "take.default_lease_seconds", "default_lease_seconds");
            Long maxLease = optionalPositiveLong(source, doc, "take.max_lease_seconds", "max_lease_seconds");
            Long maxAttempts = optionalPositiveLong(source, doc, "take.max_attempts", "max_attempts");
            return new TemplateSchema.Take(enabled, defaultLease, maxLease, maxAttempts);
        }
    }

    private static Long optionalPositiveLong(String source, Map<String, Object> doc, String field, String key) {
        if (!doc.containsKey(key)) {
            return null;
        }
        Object raw = doc.get(key);
        if (!(raw instanceof Long l) || l <= 0) {
            throw breach(source, field, "must be a positive integer");
        }
        return l;
    }

    private static void rejectUnknownKeys(String source, String prefix, Set<String> present, Set<String> allowed) {
        Set<String> unknown = new TreeSet<>(present);
        unknown.removeAll(allowed);
        if (!unknown.isEmpty()) {
            throw breach(source, prefix + unknown.iterator().next(),
                    "unknown field (not part of the RDR-205 v1 document shape)");
        }
    }

    private static String requireString(String source, Map<String, Object> doc, String field) {
        Object raw = doc.get(field);
        if (!(raw instanceof String s) || s.isBlank()) {
            throw breach(source, field, "is required and must be a non-blank string");
        }
        return s;
    }

    @SuppressWarnings("unchecked")
    private static List<String> requireStringList(String source, Map<String, Object> doc, String field) {
        Object raw = doc.get(field);
        if (!(raw instanceof List<?> list)) {
            throw breach(source, field, "is required and must be a list");
        }
        List<String> out = new ArrayList<>();
        for (Object o : list) {
            if (!(o instanceof String s)) {
                throw breach(source, field, "must contain only strings");
            }
            out.add(s);
        }
        return out;
    }

    private static List<String> optionalStringList(String source, Map<String, Object> doc, String field) {
        if (!doc.containsKey(field) || doc.get(field) == null) {
            return List.of();
        }
        return requireStringList(source, doc, field);
    }

    private static long requirePositiveLong(String source, Map<String, Object> doc, String field) {
        Object raw = doc.get(field);
        if (!(raw instanceof Long l) || l <= 0) {
            throw breach(source, field, "is required and must be a positive integer");
        }
        return l;
    }

    private static TemplateRegistryException breach(String source, String field, String detail) {
        return new TemplateRegistryException(source, field, detail);
    }
}
