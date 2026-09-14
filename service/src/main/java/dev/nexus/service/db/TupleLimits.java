// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.nio.charset.StandardCharsets;

/**
 * RDR-205 tuple-space size limits (bead nexus-r7xao; Sam's decision
 * 2026-09-13): the tuple space is a METADATA store, not a value store.
 *
 * <p>These are fixed constants, not engine settings — unlike {@code
 * NX_TUPLE_READ_MAX} and its siblings on {@link TupleRepository}, Sam's
 * decision fixed the numbers themselves, so there is no env override and
 * no default-vs-configured split. This class is the single place they
 * live on the engine side; the Python client
 * ({@code nexus.db.t2.http_tuple_store}) and the two stdlib hooks
 * ({@code tuple_ledger_project.py}, {@code mailbox_drain.py}) mirror the
 * same numbers, and {@code test_tuple_size_limits_parity.py} pins all
 * three sides equal by reading this file's own source text.
 *
 * <p>A template MAY declare a {@code max_body_bytes} lower than {@link
 * #MAX_BODY_BYTES} ({@link TemplateSchema#maxBodyBytes()}); every other
 * limit here is global with no per-template override.
 */
public final class TupleLimits {

    private TupleLimits() {
    }

    /** Global ceiling on a tuple's {@code body}, UTF-8 bytes. A template may
     *  declare a LOWER {@code max_body_bytes}; never a higher one. */
    public static final int MAX_BODY_BYTES = 4096;

    /** Ceiling on one {@code keys}/{@code dims}/{@code keys_pattern} value, UTF-8 bytes. */
    public static final int MAX_FIELD_VALUE_BYTES = 256;

    /** Ceiling on {@code subspace}, UTF-8 bytes. */
    public static final int MAX_SUBSPACE_BYTES = 256;

    /** Ceiling on {@code nonce}, UTF-8 bytes. */
    public static final int MAX_NONCE_BYTES = 128;

    /** Ceiling on {@code claimant}, UTF-8 bytes. */
    public static final int MAX_CLAIMANT_BYTES = 128;

    /** Ceiling on {@code claim_id}, UTF-8 bytes. */
    public static final int MAX_CLAIM_ID_BYTES = 128;

    /** Ceiling on the whole serialised HTTP request body on every {@code
     *  /v1/tuples} route, refused before JSON parsing. */
    public static final int MAX_REQUEST_BODY_BYTES = 8192;

    /** UTF-8 byte length of {@code s}, or 0 for {@code null} — never negative,
     *  so a limit of 0 (a template's {@code max_body_bytes: 0}) is satisfied
     *  by both {@code null} and {@code ""} with no special-casing. */
    public static int utf8Length(String s) {
        if (s == null) {
            return 0;
        }
        return s.getBytes(StandardCharsets.UTF_8).length;
    }
}
