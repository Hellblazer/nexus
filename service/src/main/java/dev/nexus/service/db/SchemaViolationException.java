// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error: a write or a claim pattern breaches the template's
 * schema — a missing/unknown key or dimension, a dimension value outside
 * its declared {@code values} set, a missing nonce on a {@code keys+nonce}
 * template, or an out-of-range {@code ttl_seconds}/{@code lease_s}/{@code
 * timeout_s}. Names the offending {@code field} and the {@code reason},
 * raised before any write (RDR-205 §Technical Design "Operations").
 */
public final class SchemaViolationException extends TupleException {

    private final String field;
    private final String reason;

    public SchemaViolationException(String field, String reason) {
        super("SchemaViolation", 400, "field '" + field + "': " + reason);
        this.field = field;
        this.reason = reason;
    }

    public String field() {
        return field;
    }

    public String reason() {
        return reason;
    }
}
