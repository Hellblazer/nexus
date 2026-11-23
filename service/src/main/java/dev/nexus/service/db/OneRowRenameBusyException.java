// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * The one-row rename could not lock its registry rows within {@link CollectionOneRowRename#ROW_LOCK_TIMEOUT_MS}, because writers still held them (RDR-229
 * P3.7, linda's Q1 ruling on folio's r3 critique). Raised before the rename has written anything; the transaction rolls back and changes nothing.
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} answers 503 {@code rename_busy} with {@code Retry-After}. Retryable.
 *
 * <p>Matches slice C's {@code RenameBusyException} (P3.5a, not on this base); replace with it, and this body's lock with
 * {@code CollectionTombstones.lockRows}, at the replay.
 */
public final class OneRowRenameBusyException extends RuntimeException {

    /** Seconds a caller is told to wait before it retries (slice C's value). */
    public static final long RETRY_AFTER_SECONDS = 5;

    public OneRowRenameBusyException(String bound) {
        super("the collection is in use by writers: the rename could not lock its registry rows within " + bound
            + "; nothing was changed, retry shortly");
    }
}
