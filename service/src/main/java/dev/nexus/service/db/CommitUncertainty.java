// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.exception.DataAccessException;

import java.sql.SQLException;

/**
 * Whether an exception a verb threw can follow a COMMIT that PostgreSQL applied (RDR-229 row 15 addendum, review code 9): a database
 * failure ({@link DataAccessException}, {@link SQLException}, at the commit or under it) can; a typed refusal the verb raised before
 * any write ({@link UnknownCollectionIdException}, {@link CollectionRetiredIdException}, {@link CollectionIdMismatchException},
 * {@link RenameBusyException}, a refused guard) rolled back and changed nothing, so there is nothing to evict. A caller that evicts
 * only when this answers true keeps a client that repeats a stale-id request from emptying its tenant's cache each time.
 */
final class CommitUncertainty {

    private CommitUncertainty() {
    }

    /** True when {@code failure}, or a cause of it, is a database failure; false for a typed refusal. */
    static boolean mayFollowACommit(Throwable failure) {
        for (Throwable t = failure; t != null; t = t.getCause()) {
            if (t instanceof DataAccessException || t instanceof SQLException) {
                return true;
            }
            if (t.getCause() == t) {
                break;
            }
        }
        return false;
    }
}
