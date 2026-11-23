// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A verb that retires or revives a registry row (the catalog rename, the collection supersede) could not lock the row within its
 * bound, because writers still held it (RDR-229 P3.5a, linda's Q3 ruling). Raised by {@link CollectionTombstones#lockRows} before the
 * verb has written anything; the transaction rolls back and changes nothing.
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} answers 503 {@code rename_busy} with {@code Retry-After}. Retryable. A WRITER
 * has no timeout on a registry row (it waits for a verb's lock, and a statement bound of its route may still end the wait); two of its waits
 * are bounded: the exclusive sweep gate the gc routes take in Java carries the 2 s bound of the SQL functions it stands in for (SQLSTATE
 * 55P03, the sweep gate's own message), and a write that follows a rename gives up after {@code RenameFollow}'s bounded number of follows with
 * this exception ({@link #renamedRepeatedly}): both retryable.
 */
public final class RenameBusyException extends RuntimeException {

    /** Seconds a caller is told to wait before it retries. */
    public static final long RETRY_AFTER_SECONDS = 5;

    public RenameBusyException(String bound) {
        super("the collection is in use by writers: the rename or supersede could not lock its registry row within " + bound
            + "; nothing was changed, retry shortly");
    }

    private RenameBusyException(String message, boolean verbatim) {
        super(message);
    }

    /** A write that follows a rename gave up after the collection was renamed {@code times} times while it ran (RenameFollow). */
    static RenameBusyException renamedRepeatedly(int times) {
        return new RenameBusyException("the collection was renamed " + times + " times while the write ran; nothing was written, retry shortly", true);
    }
}
