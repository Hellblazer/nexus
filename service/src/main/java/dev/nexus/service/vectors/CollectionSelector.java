// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.TenantScope;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), item 6 "Readers during the stamp": the collections a Java reader selects, as the one
 * value that says whether the reader reads the id or the name.
 *
 * <p>The decision is made once, when the selector is built: {@link Gate3#holds()} read at that moment (never cached by
 * this class, so the next selector built after the latch reads the id with no restart). On the id path every requested
 * label is resolved to the registry's {@code collection_id} in the caller's transaction; a label that resolves to no
 * id selects nothing. <b>A NULL id is no collection</b> (linda 2026-11-20): an empty or null label, or one the registry
 * does not hold, is dropped and never falls back to a name predicate, so the rows P3.1 left unresolved stay unresolved.
 * On the name path nothing is read and every predicate is the name column's, as before P3.5.
 *
 * <p>One selector serves one statement or one operation. {@link #in} and {@link #eq} give the predicate for a table's
 * name column and its id twin; {@link #ids()} gives the ids a SQL function twin ({@code <name>_by_id}) takes.
 */
public final class CollectionSelector {

    private final List<String> names;
    /** Label to id for each label that resolved; {@code null} on the name path. */
    private final Map<String, Long> ids;

    private CollectionSelector(List<String> names, Map<String, Long> ids) {
        this.names = names;
        this.ids = ids;
    }

    /** The name path: every predicate is the name column's. For a caller whose gate has not latched and for tests. */
    public static CollectionSelector byName(Collection<String> names) {
        return new CollectionSelector(distinct(names), null);
    }

    /**
     * Reads {@code gate3} now and selects {@code names} by id when it holds, else by name. The registry read of the id
     * path runs in {@code ctx}.
     */
    public static CollectionSelector of(Gate3 gate3, DSLContext ctx, String tenant, Collection<String> names) {
        Objects.requireNonNull(gate3, "gate3");
        List<String> distinct = distinct(names);
        if (!gate3.holds()) {
            return new CollectionSelector(distinct, null);
        }
        return new CollectionSelector(distinct, resolve(ctx, tenant, distinct));
    }

    /** {@link #of(Gate3, DSLContext, String, Collection)} for one label. */
    public static CollectionSelector of(Gate3 gate3, DSLContext ctx, String tenant, String name) {
        return of(gate3, ctx, tenant, name == null ? List.<String>of() : List.of(name));
    }

    /** As {@link #of(Gate3, DSLContext, String, Collection)}, in its own tenant transaction (and none on the name path). */
    public static CollectionSelector of(Gate3 gate3, TenantScope scope, String tenant, Collection<String> names) {
        Objects.requireNonNull(gate3, "gate3");
        List<String> distinct = distinct(names);
        if (!gate3.holds()) {
            return new CollectionSelector(distinct, null);
        }
        return new CollectionSelector(distinct, scope.withTenant(tenant, ctx -> resolve(ctx, tenant, distinct)));
    }

    /** {@link #of(Gate3, TenantScope, String, Collection)} for one label. */
    public static CollectionSelector of(Gate3 gate3, TenantScope scope, String tenant, String name) {
        return of(gate3, scope, tenant, name == null ? List.<String>of() : List.of(name));
    }

    /**
     * The registry's id for each label, the label the caller gave (any lifecycle: a superseded or quarantine row has an id
     * too, and the reader's own checks decide what to do with it). Typed read of {@code catalog_collections}: the user's
     * label resolving to its key, which is why the registry-name reads here are {@code label-kept}.
     */
    private static Map<String, Long> resolve(DSLContext ctx, String tenant, List<String> names) {
        List<String> nonEmpty = names.stream().filter(n -> n != null && !n.isEmpty()).toList();
        Map<String, Long> out = new LinkedHashMap<>();
        if (nonEmpty.isEmpty()) {
            return out;
        }
        // live first, then the newest tombstone (linda Q3): the first row seen per label in that order is its id
        ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID)
           .from(CATALOG_COLLECTIONS)
           .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
           .and(CATALOG_COLLECTIONS.NAME.in(nonEmpty))
           .orderBy(liveFirst(), CATALOG_COLLECTIONS.COLLECTION_ID.desc())
           .fetch()
           .forEach(r -> {
               if (r.value2() != null) {
                   out.putIfAbsent(r.value1(), r.value2());
               }
           });
        // a label the request named by id is that id, when the row of that id still carries the label; else it resolves to nothing, never
        // to the row that holds the label now (I7). A plain read: the by-id read FOR KEY SHARE is the write's own, taken before this.
        for (String n : nonEmpty) {
            var requested = dev.nexus.service.db.ExpectedCollectionIds.requested(n);
            if (requested.isPresent()) {
                Long held = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(requested.getAsLong()))
                    .and(CATALOG_COLLECTIONS.NAME.eq(n)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
                if (held == null) {
                    out.remove(n);
                } else {
                    out.put(n, held);
                }
            }
        }
        // keep the caller's order, so the ids line up with the labels that resolved
        Map<String, Long> ordered = new LinkedHashMap<>();
        for (String n : nonEmpty) {
            Long id = out.get(n);
            if (id != null) {
                ordered.put(n, id);
            }
        }
        return ordered;
    }

    private static List<String> distinct(Collection<String> names) {
        return names == null ? List.of() : List.copyOf(new LinkedHashSet<>(names));
    }

    // ---- statement-level predicates: no id read in Java, the registry resolves the label inside the statement ----------

    /**
     * {@code nameCol = name} before gate 3; {@code idCol = (SELECT collection_id FROM catalog_collections WHERE tenant_id = ? AND
     * name = ?)} from it. The scalar subselect is resolved by the same statement, in its own snapshot and under the same tenant
     * scope, so a reader pays no extra round trip. It is NULL for an empty label and for one the registry does not hold, and
     * {@code idCol = NULL} selects no row: <b>a NULL id is no collection</b>, never a fall back to the name (linda 2026-11-20).
     * {@code gate3} is read when the predicate is built, so build it inside the statement's transaction.
     */
    public static Condition collectionIs(Gate3 gate3, String tenant, Field<String> nameCol, Field<Long> idCol, String name) {
        Objects.requireNonNull(gate3, "gate3");
        if (!gate3.holds()) {
            return nameCol.eq(name);
        }
        if (name == null || name.isEmpty()) {
            return DSL.falseCondition();   // the empty label is no collection
        }
        // CollectionIds.idOf: for a label the request named by id, THAT id and the label together; else live first, then the newest
        // tombstone (linda Q3). A write under an id-addressed request never lands on whichever row holds the label now (I7).
        return idCol.eq(dev.nexus.service.db.CollectionIds.idOf(tenant, name));
    }

    /** {@code ORDER BY} key that puts the row holding its label first: linda's live-first, then the newest tombstone (collection_id DESC). */
    public static org.jooq.SortField<Integer> liveFirst() {
        return DSL.when(live(), 1).otherwise(0).desc();
    }

    /**
     * The registry row that holds its label now: not a tombstone ({@code superseded_by} empty and {@code superseded_by_id} NULL, the
     * predicate of the live-label unique index). A label resolves live first, then to the newest tombstone (linda Q3, {@link
     * #liveFirst}): a collection retired with data in it still has readers and writers by its label until P4.1.
     */
    static Condition live() {
        return DSL.coalesce(CATALOG_COLLECTIONS.SUPERSEDED_BY, "").eq("").and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNull());
    }

    /** {@link #collectionIs} for several labels: {@code idCol IN (SELECT collection_id ... WHERE name IN (...))}; none selects nothing. */
    public static Condition collectionIn(Gate3 gate3, String tenant, Field<String> nameCol, Field<Long> idCol,
                                         Collection<String> names) {
        Objects.requireNonNull(gate3, "gate3");
        if (!gate3.holds()) {
            return nameCol.in(names);
        }
        List<String> nonEmpty = names.stream().filter(n -> n != null && !n.isEmpty()).distinct().toList();
        if (nonEmpty.isEmpty()) {
            return DSL.falseCondition();
        }
        // a label the request named by id is that id (with the label), never the row that holds the label now (I7)
        List<String> named = nonEmpty.stream().filter(n -> dev.nexus.service.db.ExpectedCollectionIds.requested(n).isPresent()).toList();
        List<String> free = nonEmpty.stream().filter(n -> !named.contains(n)).toList();
        Condition out = DSL.falseCondition();
        for (String n : named) {
            out = out.or(idCol.eq(dev.nexus.service.db.CollectionIds.idOf(tenant, n)));
        }
        if (free.isEmpty()) {
            return out;
        }
        // one id per label: live first, then the newest tombstone (DISTINCT ON the label)
        return out.or(idCol.in(DSL.select(CATALOG_COLLECTIONS.COLLECTION_ID).distinctOn(CATALOG_COLLECTIONS.NAME)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(free))
            .orderBy(CATALOG_COLLECTIONS.NAME, liveFirst(), CATALOG_COLLECTIONS.COLLECTION_ID.desc())));
    }

    /**
     * The registry's current label of a row's collection id, as a scalar subselect over {@code tenantCol} and {@code idCol}: what
     * a reader that returns a collection returns from gate 3, never the row's own name (which a one-row rename leaves stale).
     */
    public static Field<String> labelOf(Field<String> tenantCol, Field<Long> idCol) {
        var c = CATALOG_COLLECTIONS.as("lbl");
        return DSL.field(DSL.select(c.NAME).from(c).where(c.TENANT_ID.eq(tenantCol)).and(c.COLLECTION_ID.eq(idCol)));
    }

    /** True when this selector reads the id (gate 3 held when it was built). */
    public boolean byId() {
        return ids != null;
    }

    /** The labels as given, distinct, in order. */
    public List<String> names() {
        return names;
    }

    /** The labels as an array: the name functions' argument and the name path's predicate. */
    public String[] nameArray() {
        return names.toArray(String[]::new);
    }

    /** The id the label resolved to, or {@code null} (name path, or a label that is no collection). */
    public Long idOf(String name) {
        return ids == null ? null : ids.get(name);
    }

    /**
     * The resolved ids, distinct, in label order; empty when no label resolved. Only on the id path
     * ({@link IllegalStateException} on the name path, so a caller cannot read the wrong shape).
     */
    public List<Long> ids() {
        if (ids == null) {
            throw new IllegalStateException("a name-path selector has no ids");
        }
        return new ArrayList<>(new LinkedHashSet<>(ids.values()));
    }

    /** {@link #ids()} as the array a SQL function twin takes. */
    public Long[] idArray() {
        return ids().toArray(Long[]::new);
    }

    /** True when the id path resolved no label: the selector selects no collection. */
    public boolean selectsNothing() {
        return ids != null && ids.isEmpty();
    }

    /** {@code idCol IN (ids)} on the id path ({@code false} when none resolved), {@code nameCol IN (names)} before. */
    public Condition in(Field<String> nameCol, Field<Long> idCol) {
        if (ids == null) {
            return nameCol.in(names);
        }
        List<Long> resolved = ids();
        return resolved.isEmpty() ? DSL.falseCondition() : idCol.in(resolved);
    }

    /** {@link #in} for the one label of a single-label selector. */
    public Condition eq(Field<String> nameCol, Field<Long> idCol) {
        if (names.size() > 1) {
            throw new IllegalStateException("eq on a selector of " + names.size() + " labels");
        }
        return in(nameCol, idCol);
    }

    /** The same selection narrowed to {@code labels} (a subset of the labels it holds; the name path keeps them as given). */
    public CollectionSelector restrictedTo(Collection<String> labels) {
        List<String> distinct = distinct(labels);
        if (ids == null) {
            return new CollectionSelector(distinct, null);
        }
        Map<String, Long> some = new LinkedHashMap<>();
        for (String n : distinct) {
            Long id = ids.get(n);
            if (id != null) {
                some.put(n, id);
            }
        }
        return new CollectionSelector(distinct, some);
    }

    /**
     * The same selection narrowed to the one label {@code name} (a fan-out arm): the id path keeps its resolved id, the name
     * path its label. A label the selector does not hold selects nothing on the id path.
     */
    public CollectionSelector only(String name) {
        if (ids == null) {
            return new CollectionSelector(List.of(name), null);
        }
        Map<String, Long> one = new LinkedHashMap<>();
        Long id = ids.get(name);
        if (id != null) {
            one.put(name, id);
        }
        return new CollectionSelector(List.of(name), one);
    }
}
