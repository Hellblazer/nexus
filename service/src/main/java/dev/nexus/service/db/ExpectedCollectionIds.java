// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.IdentityHashMap;
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
 *       {@link CollectionRenamedUnderRequestException}, a live row answers under its own label;</li>
 *   <li>{@link CollectionIds#idOf(String, String)} gives a subselect on the id as well as the label, so a statement carrying it
 *       writes NULL (the P3.1 CHECK refuses it) when the label is no longer the id's. It takes no lock and records no
 *       consumption: it is the stamp of a history row (a log, never a decision), and the write that must be live at commit reads
 *       the id through {@link CollectionRegistry#requireForWrite};</li>
 *   <li>the cache readers ({@link CollectionRegistry#cached}, {@code isKnown}) answer a miss, and the lifecycle readers read by
 *       id, so no fast path answers from the label.</li>
 * </ul>
 *
 * <p><b>Consumption.</b> A named id is <i>consumed</i> only by one of the in-transaction reads above on the write's own context,
 * never by an outside-a-transaction lookup. After the handler the filter reports a named id of a write route that was not
 * consumed ({@link #unconsumed()}): a write that went on by label. With the census on ({@link #setCensus}, a test seam) it also
 * reports, per transaction, a transaction that wrote and read none of the named ids ({@link #censusFindings()}): consumption by
 * the request is not consumption by every write transaction of it. A transaction is told by its jOOQ configuration, which every
 * context derived from the transaction's own shares. A history write (a log row, never a decision) does not read the id and
 * says so ({@link #historyWrite}); it is counted as exempt, not as examined.
 *
 * <p>A request by label has no carrier: the label read {@code FOR KEY SHARE} is the transaction's own
 * ({@link CollectionRegistry#requireForWrite}), memoised on the transaction's configuration, and there is nothing to carry.
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
        /** Census only: the labels each transaction (by its configuration) read, in the order the transactions ended. */
        final Map<Object, Set<String>> readByTransaction = new IdentityHashMap<>();
        /** Census only: the transactions that declared a history write. */
        final Set<Object> historyTransactions = Collections.newSetFromMap(new IdentityHashMap<>());
        /** Census only: the transactions that wrote, with what each read, as the findings and the counts are built from them. */
        final List<String> findings = new ArrayList<>();
        int writeTransactions;
        int exemptTransactions;
        int unknownTransactions;

        Carrier(Map<String, Long> ids, Runnable beforeTransactionRead, Runnable afterTransactionRead) {
            this.ids = Map.copyOf(ids);
            this.beforeTransactionRead = beforeTransactionRead;
            this.afterTransactionRead = afterTransactionRead;
        }
    }

    /** True while a test has the per-transaction census on; false in production, so no transaction pays for it. */
    private static volatile boolean census;

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

    /**
     * The id the current request named for {@code label}, if it named one (RDR-229 P3.5c, I7): for a statement or a resolver outside
     * this package that matches a collection by label, so it can match the REQUESTED id instead, never whichever row holds the label
     * now. Reads nothing and consumes nothing; {@link CollectionIds#idOf} is the way to build the predicate and consume the id.
     */
    public static OptionalLong requested(String label) {
        return of(label);
    }

    /** The id this request named for {@code label}, if it named one. */
    static OptionalLong of(String label) {
        Carrier c = CURRENT.get();
        Long id = c == null || label == null ? null : c.ids.get(label);
        return id == null ? OptionalLong.empty() : OptionalLong.of(id);
    }

    /**
     * Records that a write route had nothing to write under {@code label} (an empty batch), so no transaction read its id and none
     * needed to: there is nothing for the id to protect. The one other way an id is consumed. It is a claim nothing checks at run
     * time, so it is made only by an early return that comes BEFORE the first {@code withTenant} of its method, and never for a
     * document or row that a later transaction still deletes or stamps (an empty rows list still deletes the stored manifest rows):
     * {@code NothingToWriteSourceScanTest} reads the source and fails on a call that is not such an early return.
     */
    public static void nothingToWrite(String label) {
        Carrier c = CURRENT.get();
        if (c != null) {
            c.consumed.add(label);
        }
    }

    /**
     * Records that the write's own transaction {@code ctx} read {@code label}'s id. The census keeps which transaction read it.
     */
    static void consumed(DSLContext ctx, String label) {
        Carrier c = CURRENT.get();
        if (c != null) {
            c.consumed.add(label);
            if (census && c.ids.containsKey(label)) {
                c.readByTransaction.computeIfAbsent(ctx.configuration(), k -> new HashSet<>()).add(label);
            }
        }
    }

    /**
     * A history write (a relevance log row, a hook failure: a row about a collection that decides nothing) stamps the id without
     * locking it and is never refused (linda's ruling: history tables are exempt from {@code FOR KEY SHARE}). The census counts the
     * transaction as exempt, and the id as consumed for the request, so a route whose only collection write is a history write is not
     * reported.
     */
    static void historyWrite(DSLContext ctx, String label) {
        Carrier c = CURRENT.get();
        if (c != null) {
            c.consumed.add(label);
            if (census) {
                c.historyTransactions.add(ctx.configuration());
            }
        }
    }

    /** Turns the per-transaction census on or off. A test seam; {@code CollectionSelectorFilter#testHooks} sets it with its sink. */
    public static void setCensus(boolean on) {
        census = on;
    }

    /**
     * Run by {@link TenantScope} just before a transaction commits, and only with the census on and a request that named an id on
     * this thread: if the transaction wrote (it was assigned a transaction id), it must have read one of the named ids, or have
     * declared a history write. The finding is kept for {@link #censusFindings()}; nothing is thrown, so the census cannot change an
     * answer.
     */
    static void beforeCommit(DSLContext ctx) {
        Carrier c = census ? CURRENT.get() : null;
        if (c == null) {
            return;
        }
        Object tx = ctx.configuration();
        boolean wrote;
        try {
            wrote = Boolean.TRUE.equals(ctx.select(DSL.field(DSL.function("txid_current_if_assigned", SQLDataType.BIGINT).isNotNull()))
                .fetchOne(0, Boolean.class));
        } catch (DataAccessException e) {
            c.unknownTransactions++;
            return;
        }
        if (!wrote) {
            return;
        }
        if (c.historyTransactions.contains(tx)) {
            c.exemptTransactions++;
            return;
        }
        c.writeTransactions++;
        if (c.readByTransaction.getOrDefault(tx, Set.of()).isEmpty()) {
            c.findings.add("a write transaction read none of " + new TreeSet<>(c.ids.keySet()));
        }
    }

    /** The per-transaction findings of this request: a write transaction that read none of the named ids. Empty when none. */
    public static List<String> censusFindings() {
        Carrier c = CURRENT.get();
        return c == null ? List.of() : List.copyOf(c.findings);
    }

    /**
     * What the per-transaction census looked at in this request, for a test to print and to refuse a vacuous pass:
     * {examined write transactions, exempt (history) write transactions, transactions it could not read}.
     */
    public static int[] censusCounts() {
        Carrier c = CURRENT.get();
        return c == null ? new int[3] : new int[] {c.writeTransactions, c.exemptTransactions, c.unknownTransactions};
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
