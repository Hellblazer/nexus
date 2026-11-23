// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.OptionalLong;
import java.util.Set;
import java.util.TreeSet;

/**
 * The collection ids a request named (RDR-229 P3.5a, bead nexus-p2m1x.23; linda's ruling on the route boundary's gap), carried
 * from the route boundary to the handler on the request's own thread. The boundary resolves a {@code collection_id} to a label
 * and the handler then works by that label, so between the two the id could be freed and its label taken by another row, and a
 * write by the id would land in that row. While an id is named here, the registry resolves THAT id and never the label:
 * <ul>
 *   <li>{@link CollectionRegistry#resolveLabel} and {@link CollectionIds#require}, inside the handler's transaction, read the row
 *       of the id {@code FOR KEY SHARE} (a concurrent delete of the row waits for the write): no row is
 *       {@link UnknownCollectionIdException}, a retired row {@link CollectionRetiredIdException}, a row of another label
 *       {@link CollectionIdMismatchException}, a live row answers under its own label;</li>
 *   <li>{@link CollectionIds#idOf(String, String)} gives a subselect on the id as well as the label, so a statement carrying it
 *       writes NULL (the P3.1 CHECK refuses it) when the label is no longer the id's;</li>
 *   <li>the cache readers ({@link CollectionRegistry#cached}, {@code isKnown}) answer a miss, and the lifecycle readers read by
 *       id, so no fast path answers from the label.</li>
 * </ul>
 *
 * <p><b>Consumption.</b> A named id is <i>consumed</i> only by one of the in-transaction reads above on the write's own context,
 * never by an outside-a-transaction lookup. After the handler the filter reports a named id of a write route that was not
 * consumed ({@link #unconsumed()}): a write that went on by label.
 *
 * <p>A {@link ThreadLocal}, not an exchange attribute: the HTTP server keeps exchange attributes on the shared context, where
 * two requests would see each other's (see {@code RequestContext}). The boundary filter sets it just before the handler runs on
 * the same thread and clears it in a {@code finally}; the server runs one virtual thread per request. A handler that moves the
 * work to another thread is not covered, and none of the collection routes does.
 */
public final class ExpectedCollectionIds {

    /** One request's named ids, what has consumed them, and the test hook of the write transaction's first read. */
    private static final class Carrier {
        final Map<String, Long> ids;
        final Set<String> consumed = new HashSet<>();
        Runnable beforeTransactionRead;
        Runnable afterTransactionRead;
        /** The rows the by-id read returned in the transaction {@link #memoCtx} (its lock is held, so a second read adds nothing). */
        final Map<String, CollectionRow> memo = new HashMap<>();
        Object memoCtx;

        Carrier(Map<String, Long> ids, Runnable beforeTransactionRead, Runnable afterTransactionRead) {
            this.ids = Map.copyOf(ids);
            this.beforeTransactionRead = beforeTransactionRead;
            this.afterTransactionRead = afterTransactionRead;
        }
    }

    private static final ThreadLocal<Carrier> CURRENT = new ThreadLocal<>();

    private ExpectedCollectionIds() {
    }

    /**
     * Names the ids of this request, by the label the boundary resolved each to. An empty map names none.
     *
     * @param beforeTransactionRead a test seam, run once, just before the first by-id read a write transaction makes (after
     *        every outside-a-transaction lookup has passed); null in production
     * @param afterTransactionRead a test seam, run once, just after that read, with the transaction (and its lock) open; null in
     *        production
     */
    public static void set(Map<String, Long> idsByLabel, Runnable beforeTransactionRead, Runnable afterTransactionRead) {
        if (idsByLabel.isEmpty()) {
            CURRENT.remove();
        } else {
            CURRENT.set(new Carrier(idsByLabel, beforeTransactionRead, afterTransactionRead));
        }
    }

    public static void clear() {
        CURRENT.remove();
    }

    /** The labels this request named by id that no in-transaction read has consumed, sorted; empty when none is left. */
    public static List<String> unconsumed() {
        Carrier c = CURRENT.get();
        if (c == null) {
            return List.of();
        }
        Set<String> left = new TreeSet<>(c.ids.keySet());
        left.removeAll(c.consumed);
        return List.copyOf(left);
    }

    /** The id this request named for {@code label}, if it named one. */
    static OptionalLong of(String label) {
        Carrier c = CURRENT.get();
        Long id = c == null || label == null ? null : c.ids.get(label);
        return id == null ? OptionalLong.empty() : OptionalLong.of(id);
    }

    /**
     * Records that a write route had nothing to write under {@code label} (an empty batch, a document with no rows), so no
     * transaction read its id and none needed to: there is nothing for the id to protect. The one other way an id is consumed.
     */
    static void nothingToWrite(String label) {
        consumed(label);
    }

    /** Records that the write's own transaction read {@code label}'s id. */
    static void consumed(String label) {
        Carrier c = CURRENT.get();
        if (c != null) {
            c.consumed.add(label);
        }
    }

    /** The row the by-id read of {@code label} already returned in this transaction ({@code ctx}), or null. */
    static CollectionRow memoized(Object ctx, String label) {
        Carrier c = CURRENT.get();
        return c == null || c.memoCtx != ctx ? null : c.memo.get(label);
    }

    /** Keeps the row of a by-id read for the rest of the transaction {@code ctx}; a different transaction starts a fresh memo. */
    static void memoize(Object ctx, String label, CollectionRow row) {
        Carrier c = CURRENT.get();
        if (c != null) {
            if (c.memoCtx != ctx) {
                c.memo.clear();
                c.memoCtx = ctx;
            }
            c.memo.put(label, row);
        }
    }

    /** Runs the test seam, once, before a write transaction's by-id read. */
    static void beforeTransactionRead() {
        Carrier c = CURRENT.get();
        if (c != null && c.beforeTransactionRead != null) {
            Runnable hook = c.beforeTransactionRead;
            c.beforeTransactionRead = null;
            hook.run();
        }
    }

    /** Runs the test seam, once, after a write transaction's by-id read, while its lock is held. */
    static void afterTransactionRead() {
        Carrier c = CURRENT.get();
        if (c != null && c.afterTransactionRead != null) {
            Runnable hook = c.afterTransactionRead;
            c.afterTransactionRead = null;
            hook.run();
        }
    }
}
