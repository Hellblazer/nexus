// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.JSONB;

/**
 * Shared TEXT-to-jsonb write helper for the schema type-hygiene arc (epic
 * nexus-cefa1).
 *
 * <p>Originated in {@code TelemetryRepository} (nexus-cefa1.3, P2) for
 * {@code hook_failures.batch_doc_ids}. {@code AspectRepository}
 * (nexus-cefa1.4, P3) needed the identical helper for {@code
 * document_aspects.extras} / {@code .salient_sentences}, so it moved here
 * rather than being copy-pasted a second time — package-private, so every
 * jsonb-target write site in this package can reuse it without a public API
 * surface.
 */
final class JsonbSupport {

    private JsonbSupport() {
    }

    /**
     * Mirrors each type-hygiene changeset's own {@code USING NULLIF(col,
     * '')::jsonb}: null/blank input writes a real SQL NULL rather than an
     * invalid empty-string jsonb literal ({@code ''::jsonb} raises "invalid
     * input syntax for type json").
     */
    static JSONB jsonbOrNull(String v) {
        return (v == null || v.isBlank()) ? null : JSONB.valueOf(v);
    }

    /**
     * For NOT NULL jsonb columns (e.g. {@code plans.plan_json},
     * nexus-cefa1.5, P4): unlike {@link #jsonbOrNull}, a null/blank value is
     * never valid input here — the column's own changeset casts with a
     * plain {@code ::jsonb} (no {@code NULLIF}), so a null/blank write must
     * be rejected before it ever reaches the database rather than being
     * silently coerced to something the NOT NULL constraint would then
     * reject anyway with a less informative error. The primary gate is the
     * HTTP handler's own 400 ({@code PlanHandler}, {@code Jackson#readTree}
     * validation, mirroring {@code AspectHandler.rejectMalformedJson}); this
     * is the repository-layer backstop for any caller that reaches here
     * directly (e.g. a future write path that bypasses the handler).
     */
    static JSONB jsonbRequired(String v, String field) {
        if (v == null || v.isBlank()) {
            throw new IllegalArgumentException("field '" + field + "' must not be null or blank");
        }
        return JSONB.valueOf(v);
    }
}
