// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

import dev.nexus.service.datamove.ArbiterTwins;
import dev.nexus.service.datamove.CompletionCheck;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda 2026-11-23, addenda 8 and 8a, and her ruling after folio's review of 19d19081dc): after a one-row rename A to B, B's rows in
 * the name-keyed unique tables still carry the name {@code A} (the one-row rename writes the registry row only) under B's {@code collection_id}. A collection
 * registered later under the label {@code A} would meet those rows on the name key of every table. Until P4.1 drops the name columns, a label is RESERVED
 * when, and only when:
 * <ol>
 * <li>the gate 3 latch row exists, read in the same transaction (before the latch every rename is the name path and no row carries a stale name); and</li>
 * <li>a probed row carries the label under a {@code collection_id} that resolves to a registry row now labelled something OTHER than the label. A row whose
 *     id resolves to the label's own row (a tombstone's rows, so a revive is not refused), a row whose id resolves to no registry row (a dangling id), and rows
 *     under the id of the collection that is being renamed onto the label (taking its own label back) do not reserve it.</li>
 * </ol>
 *
 * <p>The probed tables are the name-keyed unique tables of {@link ArbiterTwins#ALL} that arbitrate a write, {@code pdf_pipeline} included (its primary key leads
 * with {@code tenant_id}, and a registration is rare). The history tables ({@link #HISTORY_NOT_PROBED}: {@code chash_remap}, {@code reembed_jobs}) are never
 * probed: their name does not arbitrate a write, so a leftover row there blocks nothing. The check is one statement of {@code EXISTS}es, each joined to the
 * registry by id; the tables' existing unique indexes answer the name probe and no index is added ({@code LabelReservedPlanTest} shows the plans).
 * {@code pdf_pipeline} has no index led by {@code (tenant_id, collection)}: its probe is bounded to one tenant's rows by a tenant-led index, accepted
 * because a registration is rare (linda 2026-11-23). If registration latency shows it, the index goes in P3.4's runtime index pass, not a changeset.
 *
 * <p><b>Import and restore paths</b> (folio's review SB2, linda): the per-collection import ({@code POST /v1/catalog/import/collection},
 * {@code CatalogRepository#importCollectionsBatchReport} and {@code #importCollection}) checks every fresh row: a refused row of the batch report (rule
 * {@code label_reserved}) or, in the single form, a 409. The whole-tenant restore ({@code TenantImportLoad}, the tenant import routes) is EXEMPT because
 * it only ever loads into an EMPTY tenant: it re-checks emptiness inside the load and refuses a tenant that holds any row ("tenant not empty", 409
 * {@code tenant_not_empty}), and an empty tenant has no stale row to meet ({@code Rdr229P37RenameAcceptanceTest} shows the refusal).
 *
 * <p>Applies wherever a label becomes live: a new or revived registration (and again after the INSERT, in the same transaction, because a rename-away can land
 * between the read and the INSERT), a supersede's successor, the target of a rename. A refusal is {@link LabelReservedException}, a typed 409
 * {@code label_reserved} naming P4.1.
 */
public final class LabelReserved {

    /** Name-keyed tables of {@link ArbiterTwins#ALL} the check never reads, with why: their name does not arbitrate a write. Pinned against the list by a test. */
    public static final Map<String, String> HISTORY_NOT_PROBED = Map.of(
        "chash_remap", "a history table: its name column records a past remap and arbitrates no write, so a leftover row blocks nothing",
        "reembed_jobs", "a history table: a finished or failed job names a collection it no longer holds, and no write of a new collection meets it");

    /** One table the check reads: its name column, its id column and the arbiter's partial predicate as a typed condition. */
    record Probe(String table, String nameColumn, String idColumn, Condition predicate) { }

    private LabelReserved() { /* static */ }

    /** The probes, one per entry of {@link ArbiterTwins#ALL} not in {@link #HISTORY_NOT_PROBED}. */
    static List<Probe> probes() {
        List<Probe> out = new ArrayList<>();
        for (ArbiterTwins a : ArbiterTwins.ALL) {
            if (HISTORY_NOT_PROBED.containsKey(a.table())) {
                continue;
            }
            out.add(new Probe(a.table(), collectionColumn(a.nameColumns(), a.table()), collectionColumn(a.idColumns(), a.table()), predicateOf(a)));
        }
        return out;
    }

    /** The first column of a key's column list that names the collection (its name column, or its id column in the twin). */
    private static String collectionColumn(String columns, String table) {
        for (String c : columns.split(",")) {
            if (c.trim().contains("collection")) {
                return c.trim();
            }
        }
        throw new IllegalStateException("ArbiterTwins entry " + table + " has no collection column in " + columns);
    }

    /** The arbiter's partial predicate as a typed condition; an unmapped one is a defect the pin test names. */
    private static Condition predicateOf(ArbiterTwins a) {
        String p = a.predicate();
        if (p == null) {
            return DSL.trueCondition();
        }
        if (ArbiterTwins.TOPICS_ROOT.equals(p)) {
            return DSL.field(DSL.name("x", "parent_id")).isNull();
        }
        throw new IllegalStateException("ArbiterTwins entry " + a.table() + " has a partial predicate LabelReserved cannot express: " + p);
    }

    /**
     * The statement: whether any probed table holds a row named {@code label} for {@code tenant} whose {@code collection_id} resolves to a registry row now
     * labelled something other than {@code label}, leaving out rows under {@code becomingHolder} (the id of the collection a rename is about to give the label
     * to; null for a registration). Does not read the latch: {@link #isReserved} does.
     */
    static org.jooq.Select<org.jooq.Record1<Boolean>> query(DSLContext ctx, String tenant, String label, Long becomingHolder) {
        var c = CATALOG_COLLECTIONS.as("c");
        Condition any = DSL.falseCondition();
        for (Probe p : probes()) {
            Table<?> t = DSL.table(DSL.name("nexus", p.table())).as("x");
            Field<String> tenantCol = DSL.field(DSL.name("x", "tenant_id"), String.class);
            Field<String> nameCol = DSL.field(DSL.name("x", p.nameColumn()), String.class);
            Field<Long> idCol = DSL.field(DSL.name("x", p.idColumn()), Long.class);
            Condition probe = tenantCol.eq(tenant).and(nameCol.eq(label)).and(p.predicate());
            if (becomingHolder != null) {
                probe = probe.and(idCol.ne(becomingHolder));
            }
            any = any.or(DSL.exists(DSL.selectOne().from(t).join(c).on(c.TENANT_ID.eq(tenantCol).and(c.COLLECTION_ID.eq(idCol)))
                .where(probe).and(c.NAME.ne(label))));
        }
        return ctx.select(DSL.field(any));
    }

    /**
     * Whether {@code label} is reserved for {@code tenant} (see the class note): the latch row exists, read in {@code ctx}'s transaction, and a probed row
     * carries the label under another collection. {@code becomingHolder} is null for a registration.
     */
    public static boolean isReserved(DSLContext ctx, String tenant, String label, Long becomingHolder) {
        if (CompletionCheck.readLatch(ctx, CompletionCheck.GATE3).isEmpty()) {
            return false;
        }
        return Boolean.TRUE.equals(query(ctx, tenant, label, becomingHolder).fetchOne(0, Boolean.class));
    }

    /**
     * @throws LabelReservedException when {@code label} is reserved; nothing was written
     */
    public static void requireFree(DSLContext ctx, String tenant, String label) {
        requireFree(ctx, tenant, label, null);
    }

    /** As above for a rename: the collection with {@code becomingHolder} as its id may take the label (back). */
    public static void requireFree(DSLContext ctx, String tenant, String label, Long becomingHolder) {
        if (isReserved(ctx, tenant, label, becomingHolder)) {
            throw new LabelReservedException(label);
        }
    }
}
