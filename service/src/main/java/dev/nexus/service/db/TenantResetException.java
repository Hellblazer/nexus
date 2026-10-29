// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A parked tuple wait ({@code rd}, {@code in}, {@code wait}) was woken because its tenant was reset by the test-only
 * {@code POST /v1/_test/tenant-reset} route (bead nexus-0bl77, rtg20 phase A). The wait returns this instead of
 * re-querying: after a reset the tenant's rows belong to the next test, and a waiter left over from the previous one
 * must not claim them. {@code TupleHandler} renders it as 409
 * {@code TenantReset}. Only reachable when the route is enabled ({@code NX_TEST_TENANT_RESET_ROUTE=1}); no
 * production request path can raise it.
 *
 * <p>Deliberately NOT a {@link TupleException}: those fourteen are the typed errors a released client maps to a
 * {@code TupleError} class and the tuple-space doc lists ({@code test_tuple_error_table_pin.py} pins both). This one
 * reaches only the abandoned request of a reset test, so it stays out of the client's error vocabulary; a client
 * that did see it would read an unknown error body.
 */
public final class TenantResetException extends RuntimeException {

    public static final String CODE = "TenantReset";
    public static final int HTTP_STATUS = 409;

    public TenantResetException(String tenant) {
        super("tenant '" + tenant + "' was reset while this wait was parked; the wait is aborted");
    }

    public String code() {
        return CODE;
    }

    public int httpStatus() {
        return HTTP_STATUS;
    }
}
