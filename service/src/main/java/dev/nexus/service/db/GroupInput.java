// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.JSONB;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;

/**
 * Validation of the fields of a group write (RDR-252), before any row is read: types, the name grammar of a
 * {@code serves} entry, the 5 to 240 minute bound, a UTC instant. Every refusal is a 400 with a code the client
 * can act on.
 */
final class GroupInput {

    static final int MIN_DEADLINE = 5;
    static final int MAX_DEADLINE = 240;
    static final int DEFAULT_DEADLINE = 20;
    static final int MIN_STALL_FACTOR = 1;
    static final int MAX_STALL_FACTOR = 10;
    static final int DEFAULT_STALL_FACTOR = 3;

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<List<String>> STRINGS = new TypeReference<>() { };

    private GroupInput() {
    }

    /** {@code serves} as stored: a JSON array of base topic names. */
    static JSONB servesJson(List<String> serves) {
        try {
            return JSONB.valueOf(MAPPER.writeValueAsString(serves));
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The stored {@code serves}, in the order it was written. */
    static List<String> servesOf(JSONB stored) {
        try {
            return stored == null ? List.of() : MAPPER.readValue(stored.data(), STRINGS);
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }

    static String text(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) {
            return null;
        }
        if (!(v instanceof String s)) {
            throw new IdentityException(400, "bad_field", "'" + key + "' must be a string");
        }
        return s;
    }

    /** A whole number of minutes from 5 to 240. Anything else (a string, a fraction, 4, 241) is refused. */
    static int deadline(Object v, String what) {
        if (v instanceof Integer || v instanceof Long || v instanceof Short) {
            long n = ((Number) v).longValue();
            if (n >= MIN_DEADLINE && n <= MAX_DEADLINE) {
                return (int) n;
            }
        }
        throw new IdentityException(400, "bad_deadline",
            what + " must be a whole number of minutes from " + MIN_DEADLINE + " to " + MAX_DEADLINE);
    }

    /** A whole number of deadlines from 1 to 10 (RDR-252 audit S1c). Anything else is refused. */
    static int stallFactor(Object v) {
        if (v instanceof Integer || v instanceof Long || v instanceof Short) {
            long n = ((Number) v).longValue();
            if (n >= MIN_STALL_FACTOR && n <= MAX_STALL_FACTOR) {
                return (int) n;
            }
        }
        throw new IdentityException(400, "bad_stall_factor",
            "stall_factor must be a whole number from " + MIN_STALL_FACTOR + " to " + MAX_STALL_FACTOR);
    }

    /** A list of base topic names: the group-name grammar, no {@code --}, each once. */
    static List<String> serves(Object v) {
        if (!(v instanceof List<?> list)) {
            throw new IdentityException(400, "bad_serves", "'serves' must be a list of base topic names");
        }
        LinkedHashSet<String> out = new LinkedHashSet<>();
        for (Object o : list) {
            if (!(o instanceof String t) || !IdentityMemberships.GROUP.matcher(t).matches() || t.contains("--")) {
                throw new IdentityException(400, "bad_serves",
                    "a 'serves' entry is a base topic name: a letter or digit, then letters, digits, '.', '_' and '-' "
                        + "(at most 64), with no '--'; got " + o);
            }
            out.add(t);
        }
        return new ArrayList<>(out);
    }

    /** Topic base name to minutes. Whether each key is in {@code serves} is the caller's check. */
    static Map<String, Integer> byTopic(Object v) {
        if (!(v instanceof Map<?, ?> m)) {
            throw new IdentityException(400, "bad_deadline", "'ack_deadline_by_topic' must be an object of minutes by topic");
        }
        Map<String, Integer> out = new LinkedHashMap<>();
        for (Map.Entry<?, ?> e : m.entrySet()) {
            if (!(e.getKey() instanceof String k)) {
                throw new IdentityException(400, "bad_deadline", "'ack_deadline_by_topic' keys are topic base names");
            }
            out.put(k, deadline(e.getValue(), "ack_deadline_by_topic[" + k + "]"));
        }
        return out;
    }

    /** A UTC instant ({@code 2026-12-01T00:00:00Z}); null, "open" and "" are open. */
    static OffsetDateTime instant(Object v, String what) {
        if (v == null || "".equals(v) || "open".equals(v)) {
            return null;
        }
        if (v instanceof String s) {
            try {
                return OffsetDateTime.ofInstant(Instant.parse(s), ZoneOffset.UTC);
            } catch (DateTimeParseException e) {
                // fall through to the refusal
            }
        }
        throw new IdentityException(400, "bad_" + what, "'" + what + "' must be a UTC instant like 2026-12-01T00:00:00Z"
            + (what.equals("ends") ? " or open" : ""));
    }
}
