// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ManifestFillableCount;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (linda 2026-11-23): the manifest-to-chunk join of the fillable count ({@code ManifestFillableCount.joined}, shared with the fill
 * batch's pick and UPDATE) matches the chunk row by {@code collection_id} from gate 3 and by name before it. After a one-row rename a chunk
 * keeps its old stored name, while a manifest row written under the collection's CURRENT label carries the new one (both with the one id): a name
 * join pairs them with nothing, and the row drops out of the count; by id it is a candidate. The same rows, counted with each gate.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cManifestFillableByIdTest extends ManifestFillableTestBase {

    @Test
    void aManifestRowWrittenUnderTheRenamedLabelIsPairedWithItsChunkByTheIdFromTheLatch() throws Exception {
        String d = doc("renamed owner");
        chunk(TENANT, h(41), both(OK_AT));
        manifest(d, h(41));
        for (String kind : KINDS) {
            assertThat(fillable(kind)).as("non-vacuity, before the rename (%s): the row is fillable", kind).isEqualTo(1);
        }

        String renamed = COLL.replace("__v1", "") + "-renamed__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(ctx);   // a one-row rename cascades nowhere
            PgContainerHelper.dropCatalogChunksFk(su);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, renamed)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL)).execute();
            // a manifest row written after the rename names the collection by its current label (and its id); the chunk keeps the old name
            int n = ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, renamed)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(d)).execute();
            assertThat(n).as("setup: the manifest row names the new label").isEqualTo(1);
        }

        var scope = new TenantScope(svcDs);
        var byName = new ManifestFillableCount(new CatalogRepository(scope, () -> false, RenameGuard.open()));
        var byId = new ManifestFillableCount(new CatalogRepository(scope, () -> true, RenameGuard.open()));
        for (var kind : ManifestFillableCount.Kind.values()) {
            assertThat(byName.count(TENANT, kind, 1000).fillable())
                .as("before gate 3 (%s): the name join pairs the manifest row with no chunk", kind).isZero();
            assertThat(byId.count(TENANT, kind, 1000).fillable())
                .as("from gate 3 (%s): the id join finds the chunk and the row is fillable", kind).isEqualTo(1);
        }
    }
}
