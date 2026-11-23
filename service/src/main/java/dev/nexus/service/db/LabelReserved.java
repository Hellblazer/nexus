// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

import dev.nexus.service.datamove.ArbiterTwins;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda 2026-11-23, addenda 8 and 8a): after a one-row rename A to B, the rows of B in the name-keyed unique tables still
 * carry the name {@code A} (the one-row rename writes the registry row only) under B's {@code collection_id}. A collection registered later under the
 * label {@code A} would meet those rows on the name key of every table: {@code chunks} and {@code taxonomy_centroids} would answer the same
 * {@code (tenant_id, collection, chash, ...)} conflict again and again. Until P4.1 drops the name columns, a label is therefore RESERVED while any
 * row of such a table still carries it under a {@code collection_id} that is not the live holder of the label.
 *
 * <p>The tables are the name keys of {@link ArbiterTwins#ALL}; the check is one statement of {@code EXISTS}es, each on the key's leading columns
 * {@code (tenant_id, <name column>)}, so the table's existing unique index answers it and no index is added
 * ({@code LabelReservedPlanTest} shows the plans). {@code pdf_pipeline} is the one entry whose name key does not lead with the collection
 * ({@code (tenant_id, content_hash, collection, pdf_path)}): it is {@link #NOT_CHECKED} with its reason, and the test that pins this list fails when
 * {@link ArbiterTwins#ALL} grows an entry neither checked nor listed.
 *
 * <p>Applies wherever a label becomes live: a new or revived registration, a supersede's successor, the target of a rename. A refusal is
 * {@link LabelReservedException}, a typed 409 {@code label_reserved} naming P4.1.
 */
public final class LabelReserved {

    /** Name-keyed tables the check does not read, with why. Pinned against {@link ArbiterTwins#ALL} by a test. */
    public static final Map<String, String> NOT_CHECKED = Map.of(
        "pdf_pipeline", "its name key leads with content_hash: (tenant_id, content_hash, collection, pdf_path), so a test for the label cannot use the key; "
            + "its INSERT is a target-less ON CONFLICT DO NOTHING, which drops a conflicting row rather than raising");

    /** One table the check reads: its name column, its id column and the arbiter's partial predicate as a typed condition. */
    record Probe(String table, String nameColumn, String idColumn, Condition predicate) { }

    private LabelReserved() { /* static */ }

    /** The probes, one per entry of {@link ArbiterTwins#ALL} not in {@link #NOT_CHECKED}. */
    static List<Probe> probes() {
        List<Probe> out = new ArrayList<>();
        for (ArbiterTwins a : ArbiterTwins.ALL) {
            if (NOT_CHECKED.containsKey(a.table())) {
                continue;
            }
            String nameColumn = column(a.nameColumns(), 1);
            String idColumn = column(a.idColumns(), 1);
            if (!nameColumn.contains("collection")) {
                throw new IllegalStateException("ArbiterTwins entry " + a.table() + " does not lead its name key with a collection column: "
                    + a.nameColumns() + "; add it to LabelReserved.NOT_CHECKED with a reason");
            }
            out.add(new Probe(a.table(), nameColumn, idColumn, predicateOf(a)));
        }
        return out;
    }

    private static String column(String columns, int index) {
        return columns.split(",")[index].trim();
    }

    /** The arbiter's partial predicate as a typed condition; an unmapped one is a defect the pin test names. */
    private static Condition predicateOf(ArbiterTwins a) {
        String p = a.predicate();
        if (p == null) {
            return DSL.trueCondition();
        }
        if (ArbiterTwins.TOPICS_ROOT.equals(p)) {
            return DSL.field(DSL.name("parent_id")).isNull();
        }
        if (ArbiterTwins.REEMBED_ACTIVE.equals(p)) {
            return DSL.field(DSL.name("state"), String.class).in(ArbiterTwins.REEMBED_ACTIVE_STATES);
        }
        throw new IllegalStateException("ArbiterTwins entry " + a.table() + " has a partial predicate LabelReserved cannot express: " + p);
    }

    /**
     * The statement: whether any probed table holds a row named {@code label} for {@code tenant} under a {@code collection_id} that is not the live
     * holder of {@code label}. When the label has no live registry row the holder is {@code becomingHolder}: the id of the collection that a rename
     * is about to give the label to (it may take back a label it once had), or none for a registration.
     */
    static org.jooq.Select<org.jooq.Record1<Boolean>> query(DSLContext ctx, String tenant, String label, Long becomingHolder) {
        Field<Long> live = DSL.field(ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")));
        Field<Long> holder = DSL.coalesce(live, DSL.val(becomingHolder, Long.class));
        Condition any = DSL.falseCondition();
        for (Probe p : probes()) {
            Table<?> t = DSL.table(DSL.name("nexus", p.table()));
            Field<String> tenantCol = DSL.field(DSL.name("tenant_id"), String.class);
            Field<String> nameCol = DSL.field(DSL.name(p.nameColumn()), String.class);
            Field<Long> idCol = DSL.field(DSL.name(p.idColumn()), Long.class);
            any = any.or(DSL.exists(DSL.selectOne().from(t)
                .where(tenantCol.eq(tenant)).and(nameCol.eq(label)).and(p.predicate()).and(idCol.isDistinctFrom(holder))));
        }
        return ctx.select(DSL.field(any));
    }

    /** Whether {@code label} is reserved for {@code tenant} (see the class note); {@code becomingHolder} is null for a registration. */
    public static boolean isReserved(DSLContext ctx, String tenant, String label, Long becomingHolder) {
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
