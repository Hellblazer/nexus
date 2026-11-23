// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A verb that retires or revives a registry row (the catalog rename, the collection supersede) could not lock the row within its
 * bound, because writers still held it (RDR-229 P3.5a, linda's Q3 ruling). Raised by {@link CollectionTombstones#lockRows} before the
 * verb has written anything; the transaction rolls back and changes nothing.
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} answers 503 {@code rename_busy} with {@code Retry-After}. Retryable. Writers
 * get no timeout of their own.
 */
public final class RenameBusyException extends RuntimeException {

    /** Seconds a caller is told to wait before it retries. */
    public static final long RETRY_AFTER_SECONDS = 5;

    public RenameBusyException(String bound) {
        super("the collection is in use by writers: the rename or supersede could not lock its registry row within " + bound
            + "; nothing was changed, retry shortly");
    }
}
