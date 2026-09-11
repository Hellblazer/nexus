// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * Base of the nine typed RDR-205 tuple-space errors (bead nexus-em75s.4,
 * §Technical Design "Operations"). Each carries a {@link #code()} matching
 * its RDR-205 name verbatim (e.g. {@code "UnknownSubspace"}) and the HTTP
 * status {@link TupleHandlerErrorMapping} uses when rendering it — the
 * mapping lives on the exception itself so a new subtype cannot be added
 * without also declaring how it renders. {@code TupleHandler} catches this
 * base type ahead of the generic 500 ladder and renders {@link #code()},
 * {@link #httpStatus()} and {@link #getMessage()} uniformly.
 */
public abstract class TupleException extends RuntimeException {

    private final String code;
    private final int httpStatus;

    protected TupleException(String code, int httpStatus, String message) {
        super(message);
        this.code = code;
        this.httpStatus = httpStatus;
    }

    /** The RDR-205 typed-error name, verbatim (e.g. {@code "UnknownSubspace"}). */
    public final String code() {
        return code;
    }

    /** The HTTP status {@code TupleHandler} sends for this error. */
    public final int httpStatus() {
        return httpStatus;
    }
}
