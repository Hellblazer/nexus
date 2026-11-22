// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17, {@code vectors-074-1}): {@code nexus.catalog_reconcile_commit} refuses a staged present
 * row whose {@code physical_collection} has no registry row, as it refuses a path outside the owner root, and commits the
 * other rows. Without the refusal the id lookup is NULL and the write fails
 * {@code catalog_documents_physical_collection_id_check} (SQLSTATE 23514), which fails the whole commit with write_failed.
 * The rest of the commit's behaviour is {@link CatalogReconcileCommitBehaviourTest}'s.
 */
class CatalogReconcileCommitUnregisteredCollectionTest extends CatalogReconcileTestBase {

    private static final String OWNER = "1.1";

    /** Every collection this class stages is registered for each fresh tenant; the test removes one between stage and commit. */
    @Override
    String newTenant() {
        String t = super.newTenant();
        try (Connection su = pg.createConnection("")) {
            for (String name : List.of("code__seed", "code__elsewhere")) {
                PgContainerHelper.insertCollection(org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES), t, name);
            }
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
        return t;
    }

    @BeforeAll
    void boot() throws Exception {
        migrate();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, "svc_rc074_unreg", "svc_rc074_unreg_pass");
        }
        connect("svc_rc074_unreg", "svc_rc074_unreg_pass");
    }

    @Test
    void aPresentRowOfAnUnregisteredCollection_isRefusedWithAReason_andTheOtherCollectionsRowsCommit() throws Exception {
        String t = newTenant();
        owner(t, OWNER, "/r");
        stage(t, OWNER, "r1", "partial",
            present(0, "reg1.py"), present(1, "reg2.py"),
            present(2, "gone1.py").set("physical_collection", "code__elsewhere"),
            present(3, "gone2.py").set("physical_collection", "code__elsewhere"));
        // the registry row goes between stage and commit
        try (Connection su = pg.createConnection("")) {
            CollectionMintSupport.deleteRow(org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES), t, "code__elsewhere");
        }

        Map<String, Object> out = repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT);

        for (String gone : List.of("gone1.py", "gone2.py")) {
            assertThat(actionOf(out, gone)).isEqualTo("refused");
            assertThat((String) outcomeRow(out, gone).get("reason")).startsWith("unregistered_collection")
                .contains("code__elsewhere");
            assertThat(outcomeRow(out, gone).get("tumbler")).as("a refused row has no tumbler").isNull();
            assertThat(liveDoc(t, OWNER, gone)).as("a refused row writes no document").isNull();
        }
        for (String ok : List.of("reg1.py", "reg2.py")) {
            assertThat(actionOf(out, ok)).isEqualTo("created");
            assertThat(liveDoc(t, OWNER, ok)).as("the registered collection's row was written").isNotNull();
        }
        assertThat(liveCount(t, OWNER)).as("only the registered collection's rows were written").isEqualTo(2);
    }
}
