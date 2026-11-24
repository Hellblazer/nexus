// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.impl.DSL;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * The rows of a ledger table (pdf_pipeline, chash_remap) that a label with NO registry row names by the name they recorded: before gate 3
 * that name; from gate 3 that name and an id no LIVE registry row holds (or none).
 *
 * <p>There is deliberately no {@code id = idOf(label)} arm, which {@link CollectionKey#eqLedger} and {@link CollectionKey#eqHistory}
 * have. A write that read the registry for the label and found no row took no lock, so the label can be registered by another request
 * between that tolerant read and the delete, and an id arm would then reach the NEW collection's rows without the key share a writer
 * takes (RDR-229 row 21, linda). A delete whose label HAS a row keeps the rule that reads the row it locked.
 */
final class DanglingLedger {

    private DanglingLedger() {
    }

    static Condition of(Gate3 gate3, String tenant, Field<String> name, Field<Long> id, String label) {
        Condition byName = name.eq(label);
        if (!gate3.holds()) {
            return byName;
        }
        var reg = CATALOG_COLLECTIONS.as("dlgr");
        return byName.and(id.isNull().or(DSL.notExists(DSL.selectOne().from(reg)
            .where(reg.TENANT_ID.eq(tenant)).and(reg.COLLECTION_ID.eq(id)).and(CollectionIds.live(reg)))));
    }
}
