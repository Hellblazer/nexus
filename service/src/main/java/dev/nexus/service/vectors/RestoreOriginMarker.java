// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.QuarantineOrigin;
import dev.nexus.service.db.RestoreOriginUnregisteredException;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda 2026-11-23): the restore functions match a quarantined chunk to its origin by the origin's registry id from gate 3
 * (vectors-083), and a chunk whose id key is the JSON null marker (an origin that had no registry row when it was quarantined, or no longer has one) never
 * matches. The functions would then answer a zero, or {@code missing}, with no word of why. This is the check the repository runs first, in the restore's
 * own transaction or a read of its own: after the latch, a quarantined chunk of the named siblings whose label tag is the origin's and whose id key is the
 * null marker is a {@link RestoreOriginUnregisteredException}. Nothing has moved when it is thrown. Before the latch nothing is checked (the label match
 * is the rule and the marker is not read).
 */
final class RestoreOriginMarker {

    /** The value of {@link QuarantineOrigin#ORIGIN_ID_KEY}, as a literal so the census reads the key at the accessor; the restore tests fail if they differ. */
    private static final String ORIGIN_ID = "origin_collection_id";
    private static final Field<JSONB> ID_KEY = DSL.jsonbGetAttribute(CHUNKS.METADATA, ORIGIN_ID);
    private static final Field<String> ID_KEY_TYPE = DSL.function("jsonb_typeof", SQLDataType.VARCHAR, ID_KEY);
    private static final Field<String> LABEL_TAG = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection");

    private RestoreOriginMarker() {
    }

    /** The row's {@code origin_collection_id} is the JSON null marker (an origin that had no registry row when it was quarantined); false when the key is absent or a number. */
    static Condition isNullMarker() {
        return ID_KEY_TYPE.eq("null");
    }

    /**
     * Refuses a restore into {@code origin} that would skip marker rows. {@code chashes} (hex) limits the check to the chunks a caller named; null (the gc
     * restores, which take every chunk the origin's manifest owns) limits it to the marker rows the origin's manifest owns, the rows the function would
     * have restored by label. {@code labels} are the origin's label as the request named it and as the registry holds it now.
     */
    static void requireNone(DSLContext ctx, Gate3 gate3, String tenant, List<String> siblings, List<String> labels, List<String> chashes) {
        if (!gate3.holds() || siblings.isEmpty()) {
            return;
        }
        Condition cond = CHUNKS.TENANT_ID.eq(tenant)
            .and(CollectionSelector.collectionIn(gate3, tenant, CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID, siblings))
            .and(ID_KEY_TYPE.eq("null"))
            .and(LABEL_TAG.in(labels));
        if (chashes != null) {
            cond = cond.and(CHUNKS.CHASH.in(chashes.stream().map(h -> dev.nexus.service.db.Chash.fromHex(h).toBytes()).toList()));
        } else {
            cond = cond.and(DSL.exists(DSL.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant))
                .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(CollectionIds.idOf(tenant, labels.get(labels.size() - 1))))
                .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(CHUNKS.CHASH))));
        }
        long rows = ctx.fetchCount(DSL.selectDistinct(CHUNKS.CHASH).from(CHUNKS).where(cond));
        if (rows > 0) {
            throw new RestoreOriginUnregisteredException(labels.get(0), rows);
        }
    }

    /** The origin's label as the request named it and as the registry holds it now (one entry when they are the same). */
    static List<String> originLabels(String requested, String current) {
        return requested.equals(current) ? List.of(requested) : List.of(requested, current);
    }

    /**
     * The date-range restore's origin condition. RDR-229 P3.7 (linda 2026-11-23): from gate 3 a chunk is the origin's by the origin's id (QuarantineOrigin's one definition, the label tag only for a
     * row that carries no id key), so a window named by a one-row-renamed collection's NEW label finds the chunks tagged with its old one; an untagged
     * chunk is still taken for this origin's. A marker row is selected through its label and refused by restoreAcross, as in every form.
     */
    static Condition windowOrigin(Gate3 gate3, String tenant, String originCollection, Field<String> originStamp) {
        return gate3.holds()
            ? QuarantineOrigin.taggedWith(true, tenant, originCollection).or(originStamp.isNull())
                .or(isNullMarker().and(originStamp.eq(originCollection)))
            : originStamp.isNull().or(originStamp.eq(originCollection));
    }

    /**
     * RDR-229 P3.7 (linda 2026-11-23): from gate 3 a requested chunk in the siblings whose label tag is the origin's but whose id key is the null marker
     * is not restorable by id; the restore would read it {@code missing}. The whole call is refused before any sibling is touched (409
     * {@code restore_origin_unregistered}); the chunks stay quarantined. A read of its own: nothing is locked and nothing is written.
     */
    static void refuseRequested(dev.nexus.service.db.TenantScope scope, Gate3 gate3, String tenant, String originCollection, List<String> siblings,
                                String[] hex) {
        if (!gate3.holds() || siblings.isEmpty()) {
            return;
        }
        scope.withTenant(tenant, ctx -> {
            requireNone(ctx, gate3, tenant, siblings, List.of(originCollection), List.of(hex));
            return null;
        });
    }
}
