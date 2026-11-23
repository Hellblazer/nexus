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

    private static final Field<JSONB> ID_KEY = DSL.function("jsonb_extract_path", SQLDataType.JSONB, CHUNKS.METADATA, DSL.val(QuarantineOrigin.ORIGIN_ID_KEY));
    private static final Field<String> ID_KEY_TYPE = DSL.function("jsonb_typeof", SQLDataType.VARCHAR, ID_KEY);
    private static final Field<String> LABEL_TAG = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection");

    private RestoreOriginMarker() {
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
}
