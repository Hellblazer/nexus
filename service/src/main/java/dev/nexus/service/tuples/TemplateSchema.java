// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * A validated RDR-205 tuple template (bead nexus-em75s.3). The document shape
 * mirrors RDR-205 §Technical Design "Registry" — the May format with the
 * substrate keys dropped: {@code tier}, {@code tiers}, {@code content_type},
 * {@code embed_from}, {@code floor}, {@code margin}, the read defaults,
 * {@code match_text}, and retention zero meaning never are not part of this
 * shape and are rejected as unknown fields by {@link TemplateSchemaParser}.
 *
 * @param name            the template's full name, e.g. {@code "ledger/<session_id>"}
 * @param nameSegments     {@code name} split on {@code "/"}; a {@code "<param>"} segment
 *                          matches exactly one path segment, everything else must match
 *                          literally
 * @param keys             the pinned key set ({@code in}/{@code inp} match every key by
 *                          equality); required and non-empty
 * @param keyValues         per-key allowed value sets (nexus-em75s.36; RDR:844 —
 *                           {@code ledger/<session_id>}'s {@code kind} pinned to {@code
 *                           {start, report}}). A key absent from this map is unconstrained
 *                           (any non-blank string); a key present here must, when out()
 *                           supplies it, be one of the listed values. Declared in the
 *                           document shape via {@code keys}'s mapping form, e.g.
 *                           {@code keys: {kind: {values: [start, report]}}} — the smaller
 *                           change against moving a values-constrained field to {@link
 *                           #dimensions}, which {@link TemplateSchemaParser} also still
 *                           accepts as the flat {@code keys: [a, b]} list form for keys
 *                           needing no constraint. Every name in this map's key set is
 *                           also present in {@link #keys}.
 * @param dimensions        dimension name to schema; may be empty
 * @param idFrom            how the tuple id is formed
 * @param idDims            dimensions that also enter the id; every one named here must
 *                           be declared {@code required: true} in {@link #dimensions}
 * @param take              claim policy
 * @param retentionSeconds  {@code out}'s default TTL and its ceiling; required, positive
 */
public record TemplateSchema(
        String name,
        List<String> nameSegments,
        List<String> keys,
        Map<String, List<String>> keyValues,
        Map<String, Dimension> dimensions,
        IdFrom idFrom,
        List<String> idDims,
        Take take,
        long retentionSeconds) {

    public TemplateSchema {
        Objects.requireNonNull(name, "name");
        Objects.requireNonNull(idFrom, "idFrom");
        Objects.requireNonNull(take, "take");
        nameSegments = List.copyOf(nameSegments);
        keys = List.copyOf(keys);
        Map<String, List<String>> copiedKeyValues = new LinkedHashMap<>();
        for (var e : keyValues.entrySet()) {
            copiedKeyValues.put(e.getKey(), List.copyOf(e.getValue()));
        }
        keyValues = Map.copyOf(copiedKeyValues);
        dimensions = Map.copyOf(dimensions);
        idDims = List.copyOf(idDims);
    }

    /** True when {@link #name} carries no {@code <param>} segment (a literal name). */
    public boolean isLiteral() {
        return nameSegments.stream().noneMatch(TemplateSchema::isParamSegment);
    }

    static boolean isParamSegment(String segment) {
        return segment.startsWith("<") && segment.endsWith(">");
    }

    /** {@code dimensions} entry: name to type, allowed values, and whether required. */
    public record Dimension(String type, List<String> values, boolean required) {
        public Dimension {
            if (values != null) {
                values = List.copyOf(values);
            }
        }
    }

    /** Claim policy. Only {@code enabled} is required; the rest are nullable ceilings. */
    public record Take(boolean enabled, Long defaultLeaseSeconds, Long maxLeaseSeconds, Long maxAttempts) {
    }

    /** How the tuple id is formed. Wire values are the RDR-205 document-shape strings. */
    public enum IdFrom {
        KEYS("keys"),
        KEYS_NONCE("keys+nonce"),
        KEYS_BODY("keys+body");

        private final String wire;

        IdFrom(String wire) {
            this.wire = wire;
        }

        public String wire() {
            return wire;
        }

        static IdFrom fromWire(String wire) {
            for (IdFrom v : values()) {
                if (v.wire.equals(wire)) {
                    return v;
                }
            }
            return null;
        }
    }
}
