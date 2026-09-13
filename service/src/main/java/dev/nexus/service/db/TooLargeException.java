// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error (bead nexus-r7xao): a field, or the whole request
 * body, exceeds its {@link TupleLimits} ceiling. Rendered at HTTP 413 for
 * both a per-field breach (the field name is {@code "body"}, {@code
 * "subspace"}, {@code "nonce"}, {@code "claimant"}, {@code "claim_id"}, or
 * {@code "keys.<name>"} / {@code "dims.<name>"} / {@code
 * "keys_pattern.<name>"}) and the whole-request cap (field name {@code
 * "request body"}, raised by {@code TupleHandler} before any JSON parse).
 *
 * <p>NEVER echoes the oversized value itself — only its byte length and the
 * limit. Every size check in {@link TupleRepository} runs before the
 * existing schema validation, which is what lets it fire ahead of the sites
 * that DO echo a value ({@code SchemaViolationException}'s "value '...' not
 * in [...]" messages).
 */
public final class TooLargeException extends TupleException {

    private final String field;
    private final long actualBytes;
    private final long limitBytes;

    public TooLargeException(String field, long actualBytes, long limitBytes) {
        super("TooLarge", 413,
                "field '" + field + "' is " + actualBytes + " bytes, exceeding the limit of "
                        + limitBytes + " bytes");
        this.field = field;
        this.actualBytes = actualBytes;
        this.limitBytes = limitBytes;
    }

    public String field() {
        return field;
    }

    public long actualBytes() {
        return actualBytes;
    }

    public long limitBytes() {
        return limitBytes;
    }
}
