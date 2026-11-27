// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A chunk write named a collection whose catalog row is retired ({@code superseded_by} set) and
 * nothing asked for the name back (nexus-mp8ys).
 *
 * <p>A rename or supersede leaves the old name as a tombstone that routing no longer lists
 * (nexus-4w07i). A client process that did not perform the rename can keep writing under the old
 * name from a stale registration cache, and those chunks strand under the tombstone. Only an
 * explicit registration ({@code POST /v1/catalog/collections/upsert}) revives a name; an implicit
 * write never does, so the write is refused and the successor is named.
 *
 * <p>Deliberately NOT {@link UnregisteredCollectionException}: that refusal is a 422 whose prose a
 * client's register-and-retry recognises, and a retry here would re-register, and so revive, the
 * very name this refusal protects.
 *
 * <p>Raised before any mutating statement and before the embedder runs, so nothing is written or
 * billed. {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} maps it to a typed 409.
 */
public final class CollectionSupersededException extends RuntimeException {

    private final String tenant;
    private final String collection;
    private final String supersededBy;

    /** The write is refused and {@code supersededBy}, the live successor's current label, is where it should go. */
    public CollectionSupersededException(String tenant, String collection, String supersededBy) {
        super("collection '" + collection + "' was superseded by '" + supersededBy
            + "', so a write to it is refused; write to '" + supersededBy + "' instead");
        this.tenant = tenant;
        this.collection = collection;
        this.supersededBy = supersededBy;
    }

    private CollectionSupersededException(String tenant, String collection) {
        super("collection '" + collection + "' is retired and names no live successor, so a write to it is refused");
        this.tenant = tenant;
        this.collection = collection;
        this.supersededBy = "";
    }

    /**
     * The refusal of a write to a retired collection whose successor was deleted (RDR-229 R7): there is nowhere to redirect it, and the text
     * the row's {@code superseded_by} still holds is its own dead label, which is no successor.
     */
    public static CollectionSupersededException noSuccessor(String tenant, String collection) {
        return new CollectionSupersededException(tenant, collection);
    }

    public String tenant() { return tenant; }
    public String collection() { return collection; }
    /** The successor the refused write should go to; empty when {@link #hasSuccessor()} is false. */
    public String supersededBy() { return supersededBy; }
    /** False when the retired collection names no live successor. */
    public boolean hasSuccessor() { return !supersededBy.isEmpty(); }
}
