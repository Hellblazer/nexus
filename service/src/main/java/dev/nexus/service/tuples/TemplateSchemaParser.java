// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import dev.nexus.service.db.TupleLimits;

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
            "name", "keys", "dimensions", "id_from", "id_dims", "take", "retention_seconds",
            "max_body_bytes", "lock", "max_live_rows", "claim_log_ttl_seconds", "doc");

    /** Closed field set of the {@code doc} block (RDR-239 Technical Design item 1). */
    private static final Set<String> DOC_FIELDS =
            Set.of("summary", "writers", "readers", "fields", "body", "example", "see_also");

    /** Closed field set of {@code doc.example}. */
    private static final Set<String> EXAMPLE_FIELDS = Set.of("subspace", "nonce", "keys", "dims", "body");

    private static final Set<String> DIMENSION_FIELDS = Set.of("type", "values", "required");

    /** Fields allowed on a {@code keys} mapping-form entry's spec (nexus-em75s.36). */
    private static final Set<String> KEY_SPEC_FIELDS = Set.of("values");

    private static final Set<String> TAKE_FIELDS =
            Set.of("enabled", "default_lease_seconds", "max_lease_seconds", "max_attempts");

    /**
     * The whole template, {@code doc} block included: any breach, in the structure or in the doc,
     * throws. This is the classpath-template behaviour (RDR-239 S3); a registry source that may
     * drop a bad doc calls {@link #parseStructure} and {@link #parseDoc} separately.
     */
    static TemplateSchema parse(String source, Map<String, Object> doc) {
        TemplateSchema schema = parseStructure(source, doc);
        if (!doc.containsKey("doc")) {
            return schema;
        }
        return schema.withDoc(parseDoc(source, doc.get("doc"), schema));
    }

    /** Everything but the {@code doc} block, which is checked for its name only. The returned
     *  schema carries no doc. */
    static TemplateSchema parseStructure(String source, Map<String, Object> doc) {
        rejectUnknownKeys(source, "", doc.keySet(), TOP_LEVEL_FIELDS);

        String name = requireString(source, doc, "name");
        List<String> nameSegments = parseNameSegments(source, name);

        ParsedKeys parsedKeys = parseKeys(source, doc.get("keys"));
        List<String> keys = parsedKeys.names();
        if (keys.isEmpty()) {
            throw breach(source, "keys", "must be a non-empty list");
        }
        for (String k : keys) {
            if (k.isBlank()) {
                throw breach(source, "keys", "must not contain a blank entry");
            }
        }
        Map<String, List<String>> keyValues = parsedKeys.values();

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
        Long maxBodyBytes = parseMaxBodyBytes(source, doc);
        boolean lock = parseLock(source, doc);
        Long maxLiveRows = parseMaxLiveRows(source, doc);
        Long claimLogTtlSeconds = parseClaimLogTtlSeconds(source, doc);

        return new TemplateSchema(name, nameSegments, keys, keyValues, dimensions, idFrom, idDims, take,
                retentionSeconds, maxBodyBytes, lock, maxLiveRows, claimLogTtlSeconds, null);
    }

    // ── doc block (RDR-239 Technical Design item 1, bead nexus-mdbds.2.10) ──────────

    /**
     * Parses the {@code doc} mapping of {@code schema}'s template. Documentation only, so every
     * rule here is about the block itself and what it points at (declared keys, dimensions, the
     * template's body ceiling); the example's shape, nonce and size checks need the whole
     * registry and run in {@link TemplateRegistry}, through {@link TupleShapeValidator}.
     */
    @SuppressWarnings("unchecked")
    static TemplateDoc parseDoc(String source, Object raw, TemplateSchema schema) {
        if (!(raw instanceof Map)) {
            throw breach(source, "doc", "must be a mapping");
        }
        Map<String, Object> d = (Map<String, Object>) raw;
        rejectUnknownKeys(source, "doc.", d.keySet(), DOC_FIELDS);
        int weight = weigh(d);
        if (weight > TemplateDoc.MAX_BLOCK_BYTES) {
            throw breach(source, "doc", "is " + weight + " bytes as read, over the "
                    + TemplateDoc.MAX_BLOCK_BYTES + "-byte cap for a doc block");
        }
        String summary = describe(source, "doc.summary", d.get("summary"), "is required and must be a non-blank string");
        List<String> writers = docStringList(source, d, "writers");
        List<String> readers = docStringList(source, d, "readers");
        Map<String, Object> fields = parseDocFields(source, d.get("fields"), schema);
        Object body = parseDocBody(source, d.get("body"), schema);
        TemplateDoc.Example example = parseExample(source, d.get("example"), schema);
        List<String> seeAlso = docStringList(source, d, "see_also");
        return new TemplateDoc(summary, writers, readers, fields, body, example, seeAlso);
    }

    /** UTF-8 bytes of every mapping key and every scalar in {@code o}, nested: the size of a doc
     *  block "as the engine's reader sees it" (S4 Q3). */
    private static int weigh(Object o) {
        if (o instanceof Map<?, ?> m) {
            int w = 0;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                w += weigh(e.getKey()) + weigh(e.getValue());
            }
            return w;
        }
        if (o instanceof List<?> l) {
            int w = 0;
            for (Object x : l) {
                w += weigh(x);
            }
            return w;
        }
        return o == null ? 0 : TupleLimits.utf8Length(String.valueOf(o));
    }

    /** A scalar MiniYaml read, as text; {@code null} for a null, a list or a mapping. */
    private static String scalarText(Object o) {
        return o instanceof String || o instanceof Long || o instanceof Boolean ? String.valueOf(o) : null;
    }

    /** One description: a non-blank scalar of at most {@link TemplateDoc#MAX_TEXT_BYTES} bytes. */
    private static String describe(String source, String field, Object raw, String whenWrong) {
        String text = scalarText(raw);
        if (text == null || text.isBlank()) {
            throw breach(source, field, whenWrong);
        }
        int len = TupleLimits.utf8Length(text);
        if (len > TemplateDoc.MAX_TEXT_BYTES) {
            throw breach(source, field, "is " + len + " bytes, over the " + TemplateDoc.MAX_TEXT_BYTES
                    + "-byte cap for a description");
        }
        return text;
    }

    @SuppressWarnings("unchecked")
    private static List<String> docStringList(String source, Map<String, Object> d, String key) {
        Object raw = d.get(key);
        if (raw == null) {
            return null;
        }
        if (!(raw instanceof List<?> list)) {
            throw breach(source, "doc." + key, "must be a list of one-line strings");
        }
        List<String> out = new ArrayList<>();
        for (int i = 0; i < list.size(); i++) {
            out.add(describe(source, "doc." + key + "[" + i + "]", list.get(i), "must be a non-blank one-line string"));
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> parseDocFields(String source, Object raw, TemplateSchema schema) {
        if (raw == null) {
            return null;
        }
        if (!(raw instanceof Map)) {
            throw breach(source, "doc.fields", "must be a mapping from a key or dimension name to a description");
        }
        Map<String, Object> out = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : ((Map<String, Object>) raw).entrySet()) {
            String name = e.getKey();
            String field = "doc.fields." + name;
            TemplateSchema.Dimension dim = schema.dimensions().get(name);
            if (!schema.keys().contains(name) && dim == null) {
                throw breach(source, field, "is not a declared key or dimension of this template");
            }
            if (e.getValue() instanceof Map) {
                Map<String, String> valueMap = parseValueMap(source, field, (Map<String, Object>) e.getValue());
                List<String> declared = dim != null ? dim.values() : schema.keyValues().get(name);
                if (declared != null && !new TreeSet<>(declared).equals(new TreeSet<>(valueMap.keySet()))) {
                    throw breach(source, field, "value map keys " + new TreeSet<>(valueMap.keySet())
                            + " differ from the declared values " + new TreeSet<>(declared));
                }
                out.put(name, valueMap);
            } else {
                out.put(name, describe(source, field, e.getValue(), "must be a one-line description or a value map"));
            }
        }
        return out;
    }

    /** {@code value: meaning} (or {@code json field: meaning}) entries, each meaning a description. */
    private static Map<String, String> parseValueMap(String source, String field, Map<String, Object> raw) {
        if (raw.isEmpty()) {
            throw breach(source, field, "must not be an empty map");
        }
        Map<String, String> out = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : raw.entrySet()) {
            out.put(e.getKey(), describe(source, field + "." + e.getKey(), e.getValue(),
                    "must be a non-blank one-line description"));
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    private static Object parseDocBody(String source, Object raw, TemplateSchema schema) {
        if (raw == null) {
            return null;
        }
        if (schema.maxBodyBytes() != null && schema.maxBodyBytes() == 0L) {
            throw breach(source, "doc.body", "must be omitted: this template declares max_body_bytes 0, so it carries no body");
        }
        if (raw instanceof Map) {
            return parseValueMap(source, "doc.body", (Map<String, Object>) raw);
        }
        return describe(source, "doc.body", raw, "must be a one-line description or a map of json field to meaning");
    }

    @SuppressWarnings("unchecked")
    private static TemplateDoc.Example parseExample(String source, Object raw, TemplateSchema schema) {
        if (raw == null) {
            return null;
        }
        if (!(raw instanceof Map)) {
            throw breach(source, "doc.example", "must be a mapping");
        }
        Map<String, Object> ex = (Map<String, Object>) raw;
        rejectUnknownKeys(source, "doc.example.", ex.keySet(), EXAMPLE_FIELDS);
        String subspace = scalarText(ex.get("subspace"));
        if (subspace == null || subspace.isBlank()) {
            throw breach(source, "doc.example.subspace", "is required and must be a non-blank string");
        }
        String nonce = null;
        if (ex.get("nonce") != null) {
            nonce = scalarText(ex.get("nonce"));
            if (nonce == null) {
                throw breach(source, "doc.example.nonce", "must be a scalar");
            }
            if (schema.idFrom() != TemplateSchema.IdFrom.KEYS_NONCE) {
                throw breach(source, "doc.example.nonce", "is allowed only when id_from is keys+nonce (this template's is "
                        + schema.idFrom().wire() + ")");
            }
        }
        String body = null;
        if (ex.get("body") != null) {
            body = scalarText(ex.get("body"));
            if (body == null) {
                throw breach(source, "doc.example.body", "must be a scalar");
            }
        }
        return new TemplateDoc.Example(subspace, nonce,
                exampleStrings(source, "doc.example.keys", ex.get("keys")),
                exampleStrings(source, "doc.example.dims", ex.get("dims")), body);
    }

    /** An example's keys or dims: MiniYaml turns {@code 3889972} into a Long and {@code true}
     *  into a Boolean, and {@code out()} takes strings, so every value is read back as text. */
    @SuppressWarnings("unchecked")
    private static Map<String, String> exampleStrings(String source, String field, Object raw) {
        if (raw == null) {
            return Map.of();
        }
        if (!(raw instanceof Map)) {
            throw breach(source, field, "must be a mapping");
        }
        Map<String, String> out = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : ((Map<String, Object>) raw).entrySet()) {
            String text = scalarText(e.getValue());
            if (text == null) {
                throw breach(source, field + "." + e.getKey(), "must be a scalar");
            }
            out.put(e.getKey(), text);
        }
        return out;
    }

    /**
     * {@code lock} (RDR-211 Phase 1 Step 1, bead nexus-rplay.3): optional top-level
     * boolean, defaults {@code false}. See {@link TemplateSchema#lock()}.
     */
    private static boolean parseLock(String source, Map<String, Object> doc) {
        if (!doc.containsKey("lock") || doc.get("lock") == null) {
            return false;
        }
        Object raw = doc.get("lock");
        if (!(raw instanceof Boolean b)) {
            throw breach(source, "lock", "must be a boolean");
        }
        return b;
    }

    /**
     * {@code claim_log_ttl_seconds} (RDR-211 Scale and Limits item 6): optional, a
     * positive integer. This is ONLY the syntactic check — "must not exceed the
     * engine's own claim-log TTL default" and "must exceed this template's own
     * retention_seconds by more than one sweep interval" both need the registry's
     * resolved default, which does not exist yet at parse time ({@link
     * TemplateRegistry#load} runs both checks once every template is parsed; see
     * {@link TemplateSchema#claimLogTtlSeconds()}'s javadoc for why that split is
     * deliberate here and not, say, {@link #parseMaxBodyBytes}'s shape).
     */
    private static Long parseClaimLogTtlSeconds(String source, Map<String, Object> doc) {
        if (!doc.containsKey("claim_log_ttl_seconds") || doc.get("claim_log_ttl_seconds") == null) {
            return null;
        }
        Object raw = doc.get("claim_log_ttl_seconds");
        if (!(raw instanceof Long l) || l <= 0) {
            throw breach(source, "claim_log_ttl_seconds", "must be a positive integer");
        }
        return l;
    }

    /**
     * {@code max_live_rows} (RDR-211 Scale and Limits item 2): optional, a positive
     * integer, no upper ceiling of its own — unlike {@code max_body_bytes} this field
     * has no global cap to stay under, since it bounds row COUNT in a subspace, not a
     * byte size {@link TupleLimits} already governs elsewhere. Absent means unbounded,
     * matching {@link TemplateSchema#maxLiveRows()}'s existing-behaviour default.
     */
    private static Long parseMaxLiveRows(String source, Map<String, Object> doc) {
        if (!doc.containsKey("max_live_rows") || doc.get("max_live_rows") == null) {
            return null;
        }
        Object raw = doc.get("max_live_rows");
        if (!(raw instanceof Long l) || l <= 0) {
            throw breach(source, "max_live_rows", "must be a positive integer");
        }
        return l;
    }

    /**
     * {@code max_body_bytes} (bead nexus-r7xao): optional, {@code 0 <= n <=
     * TupleLimits.MAX_BODY_BYTES}. A template may only LOWER the global body
     * cap, never raise it — a value above the global cap or negative fails the
     * load loudly, the same way every other structural breach in this parser
     * does, rather than silently clamping to something the template author did
     * not ask for.
     */
    private static Long parseMaxBodyBytes(String source, Map<String, Object> doc) {
        if (!doc.containsKey("max_body_bytes") || doc.get("max_body_bytes") == null) {
            return null;
        }
        Object raw = doc.get("max_body_bytes");
        if (!(raw instanceof Long l) || l < 0) {
            throw breach(source, "max_body_bytes", "must be a non-negative integer");
        }
        if (l > TupleLimits.MAX_BODY_BYTES) {
            throw breach(source, "max_body_bytes",
                    "must not exceed the global cap of " + TupleLimits.MAX_BODY_BYTES + " bytes (got " + l + ")");
        }
        return l;
    }

    /** {@code keys} document-shape entries, split from the pinned name order (RDR-205's {@code
     *  keys} list) so a caller-supplied out() request can be validated against both in one pass. */
    private record ParsedKeys(List<String> names, Map<String, List<String>> values) {
    }

    /**
     * {@code keys} accepts two forms (nexus-em75s.36): the original flat list of key-name
     * strings (every key unconstrained), or a mapping from key name to an optional spec
     * carrying {@code values} — the same allowed-value-set shape {@link #parseDimensions}
     * already gives {@code dimensions} entries, applied here to a key instead of moving
     * the field to {@code dimensions} (RDR-205 §Technical Design "Registry" ~844, e.g.
     * the literal {@code aspects}' {@code queue} pinned to {@code {aspects}}). A
     * bare {@code key_name:} entry (no nested {@code values}) is unconstrained, same as
     * the list form.
     */
    @SuppressWarnings("unchecked")
    private static ParsedKeys parseKeys(String source, Object raw) {
        if (raw instanceof List<?> list) {
            List<String> names = new ArrayList<>();
            for (Object o : list) {
                if (!(o instanceof String s)) {
                    throw breach(source, "keys", "must contain only strings");
                }
                names.add(s);
            }
            return new ParsedKeys(names, Map.of());
        }
        if (raw instanceof Map) {
            Map<String, Object> map = (Map<String, Object>) raw;
            List<String> names = new ArrayList<>();
            Map<String, List<String>> values = new LinkedHashMap<>();
            for (Map.Entry<String, Object> entry : map.entrySet()) {
                String keyName = entry.getKey();
                names.add(keyName);
                Object spec = entry.getValue();
                if (spec == null) {
                    continue;
                }
                if (!(spec instanceof Map)) {
                    throw breach(source, "keys." + keyName, "must be a mapping (or empty)");
                }
                Map<String, Object> specDoc = (Map<String, Object>) spec;
                rejectUnknownKeys(source, "keys." + keyName + ".", specDoc.keySet(), KEY_SPEC_FIELDS);
                List<String> vals = parseValuesList(source, "keys." + keyName + ".values", specDoc);
                if (vals != null) {
                    values.put(keyName, vals);
                }
            }
            return new ParsedKeys(names, values);
        }
        throw breach(source, "keys", "is required and must be a list or a mapping");
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

            List<String> values = parseValuesList(source, "dimensions." + dimName + ".values", dimDoc);

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

    /**
     * The {@code values} sub-field shared by a {@code dimensions} entry and a {@code keys}
     * mapping-form entry: absent means unconstrained ({@code null}); present must be a
     * non-empty list of non-blank strings.
     */
    private static List<String> parseValuesList(String source, String field, Map<String, Object> containingDoc) {
        if (!containingDoc.containsKey("values")) {
            return null;
        }
        Object valuesRaw = containingDoc.get("values");
        if (!(valuesRaw instanceof List<?> list) || list.isEmpty()) {
            throw breach(source, field, "must be a non-empty list");
        }
        List<String> values = new ArrayList<>();
        for (Object v : list) {
            if (!(v instanceof String s) || s.isBlank()) {
                throw breach(source, field, "must contain only non-blank strings");
            }
            values.add(s);
        }
        return values;
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
