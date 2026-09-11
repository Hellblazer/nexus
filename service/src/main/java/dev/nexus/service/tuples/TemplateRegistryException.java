// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

/**
 * A tuple-template registry load or boot-check failure (RDR-205 §Technical
 * Design "Registry", bead nexus-em75s.3). Every instance names {@code source}
 * (the offending file/descriptor, or the template name for a boot-check
 * refusal) and, where applicable, {@code field} — so the engine's boot log
 * always names the file and the field on a structural breach.
 */
public final class TemplateRegistryException extends RuntimeException {

    private final String source;
    private final String field;

    public TemplateRegistryException(String source, String field, String detail) {
        super(format(source, field, detail));
        this.source = source;
        this.field = field;
    }

    private static String format(String source, String field, String detail) {
        StringBuilder sb = new StringBuilder("tuple template registry: source=").append(source);
        if (field != null && !field.isBlank()) {
            sb.append(" field=").append(field);
        }
        return sb.append(": ").append(detail).toString();
    }

    public String source() {
        return source;
    }

    public String field() {
        return field;
    }
}
