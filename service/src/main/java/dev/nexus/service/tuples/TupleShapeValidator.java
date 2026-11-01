// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.tuples;

import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TooLargeException;
import dev.nexus.service.db.TupleLimits;

import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/**
 * The checks {@code out} runs on a post's size, shape and nonce, held apart from the
 * repository so the boot check can run the very same ones (bead nexus-mdbds.2.9, RDR-239
 * S2). Stateless: every method is static and takes the resolved {@link TemplateSchema}
 * plus the post's fields.
 *
 * <p>Errors keep the types and messages {@code TupleRepository} raised before the move:
 * {@link TooLargeException} for a size refusal, {@link SchemaViolationException} for a
 * shape or nonce refusal, each naming the failing field.
 *
 * <p>ORDER matters and is pinned by review history. {@link #validateOut} runs sizes, then
 * shape, then nonce. Sizes go first (nexus-r7xao) so an oversized value raises
 * {@code TooLarge} ahead of the shape check's value-echoing "value '...' not in [...]"
 * message instead of echoing it into a log line or response body. The nonce check sits
 * between shape and the caller's ttl checks (nexus-h61dl.3): moving it later changed which
 * exception {@code out} raised for a call that violated both at once.
 */
public final class TupleShapeValidator {

    private static final String NONCE_FIELD = "nonce";

    /** Bead nexus-jz3kh: what a refusal says about where the nonce goes. {@code nonce} is a
     *  parameter of the write itself (the request's own {@code nonce} field, {@code tuple_out}'s
     *  {@code nonce} argument); a caller who sees "nonce: required" and tries it as a key or a dim
     *  is refused again, so every nonce-related refusal names the parameter. */
    private static final String NONCE_OWN_FIELD = "its own field of the write (tuple_out's `nonce` parameter)";

    private TupleShapeValidator() {
    }

    /**
     * Sizes, then shape, then nonce: everything {@code out} validates about a post once its
     * template is resolved. The caller runs {@link #checkSubspaceSize} BEFORE resolving the
     * template, and its own ttl and delay checks AFTER this returns.
     *
     * @param nonceDeferred {@code true} on {@code ackWithReply}, whose reply nonce does not
     *                      exist until the request has been consumed: skips both the nonce
     *                      size check and the nonce presence check
     */
    public static void validateOut(TemplateSchema t, Map<String, String> keys, Map<String, String> dims,
                                   String body, String nonce, boolean nonceDeferred) {
        checkSizes(t, keys, dims, body, nonce, nonceDeferred);
        checkShape(t, keys, dims);
        if (!nonceDeferred) {
            checkNonce(t, nonce);
        }
    }

    /** One tuple field's UTF-8 byte length against *limitBytes*; a {@code null} value is
     *  0 bytes and always passes. Never echoes *value* itself (bead nexus-r7xao). */
    public static void checkFieldSize(String field, String value, int limitBytes) {
        int len = TupleLimits.utf8Length(value);
        if (len > limitBytes) {
            throw new TooLargeException(field, len, limitBytes);
        }
    }

    /** {@code subspace} against {@link TupleLimits#MAX_SUBSPACE_BYTES}. Needs no template, so
     *  it runs ahead of the template lookup. */
    public static void checkSubspaceSize(String subspace) {
        checkFieldSize("subspace", subspace, TupleLimits.MAX_SUBSPACE_BYTES);
    }

    /**
     * Keys, dims, nonce (unless deferred) and body against their size limits, in that order.
     * The body is held to the template's own {@code max_body_bytes} when it declares one,
     * else the global {@link TupleLimits#MAX_BODY_BYTES}. A template ceiling of 0 is
     * satisfied by both {@code null} and {@code ""} (both are 0 bytes) and refuses anything
     * else, with no special-casing needed here.
     */
    public static void checkSizes(TemplateSchema t, Map<String, String> keys, Map<String, String> dims,
                                  String body, String nonce, boolean nonceDeferred) {
        Map<String, String> keysSafe = keys == null ? Map.of() : keys;
        Map<String, String> dimsSafe = dims == null ? Map.of() : dims;
        for (var e : keysSafe.entrySet()) {
            checkFieldSize("keys." + e.getKey(), e.getValue(), TupleLimits.MAX_FIELD_VALUE_BYTES);
        }
        for (var e : dimsSafe.entrySet()) {
            checkFieldSize("dims." + e.getKey(), e.getValue(), TupleLimits.MAX_FIELD_VALUE_BYTES);
        }
        if (!nonceDeferred) {
            checkFieldSize(NONCE_FIELD, nonce, TupleLimits.MAX_NONCE_BYTES);
        }
        int len = TupleLimits.utf8Length(body);
        long limit = t.maxBodyBytes() != null ? t.maxBodyBytes() : TupleLimits.MAX_BODY_BYTES;
        if (len > limit) {
            throw new TooLargeException("body", len, limit);
        }
    }

    /**
     * The shape half of {@code out}'s validation: keys, dims and their allowed values. Split
     * from the nonce check (RDR-206 Phase 1 Step 2) because {@code ackWithReply} must
     * validate a reply's shape BEFORE consuming the request, while the reply's nonce does
     * not exist until the request has been consumed and handed back its id.
     */
    public static void checkShape(TemplateSchema t, Map<String, String> keys, Map<String, String> dims) {
        Map<String, String> keysSafe = keys == null ? Map.of() : keys;
        Map<String, String> dimsSafe = dims == null ? Map.of() : dims;
        TreeSet<String> unknownKeys = new TreeSet<>(keysSafe.keySet());
        unknownKeys.removeAll(t.keys());
        if (!unknownKeys.isEmpty()) {
            String first = unknownKeys.first();
            throw new SchemaViolationException(first, NONCE_FIELD.equals(first)
                    ? "not a key: the nonce is " + NONCE_OWN_FIELD
                    : "not a declared key for this template");
        }
        for (String k : t.keys()) {
            String v = keysSafe.get(k);
            if (v == null || v.isBlank()) {
                throw new SchemaViolationException(k, "required key missing or blank");
            }
            List<String> allowed = t.keyValues().get(k);
            if (allowed != null && !allowed.contains(v)) {
                throw new SchemaViolationException(k, "value '" + v + "' not in " + allowed);
            }
        }
        TreeSet<String> unknownDims = new TreeSet<>(dimsSafe.keySet());
        unknownDims.removeAll(t.dimensions().keySet());
        if (!unknownDims.isEmpty()) {
            String first = unknownDims.first();
            throw new SchemaViolationException(first, NONCE_FIELD.equals(first)
                    ? "not a dimension: the nonce is " + NONCE_OWN_FIELD
                    : "not a declared dimension for this template");
        }
        for (var e : t.dimensions().entrySet()) {
            String name = e.getKey();
            TemplateSchema.Dimension d = e.getValue();
            String v = dimsSafe.get(name);
            if (d.required() && (v == null || v.isBlank())) {
                throw new SchemaViolationException(name, "required dimension missing or blank");
            }
            if (v != null && d.values() != null && !d.values().contains(v)) {
                throw new SchemaViolationException(name, "value '" + v + "' not in " + d.values());
            }
        }
    }

    /** The nonce half of {@code out}'s validation: a blank or absent nonce is refused only
     *  when the template forms its id from {@code keys+nonce}. See {@link #checkShape}. */
    public static void checkNonce(TemplateSchema t, String nonce) {
        if (t.idFrom() == TemplateSchema.IdFrom.KEYS_NONCE && (nonce == null || nonce.isBlank())) {
            throw new SchemaViolationException(NONCE_FIELD, "required for id_from=keys+nonce: the nonce is "
                    + NONCE_OWN_FIELD + ", not a key or a dimension");
        }
    }
}
