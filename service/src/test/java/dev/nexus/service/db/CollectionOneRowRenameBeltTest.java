// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27, folio's r3 confirmation review I1): the delete belt of {@code CollectionOneRowRename.deleteRetiredTarget}, tested ON ITS OWN.
 * The caller's target checks run under the same locks just before the belt and refuse the same cases first, so a route-level test cannot tell the belt
 * from them. These tests call {@code CollectionOneRowRename.apply} directly with a target check that answers "a retired row of the new label exists, no
 * refusal" ({@code ctx -> true}), so the only thing between the rename and the delete of the row at the new label is the belt. Each case must be refused
 * with the belt's own message, and every row must be where it was. Each goes red when the belt's check for its case is removed (the mutant runs are on the
 * bead).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionOneRowRenameBeltTest {

    private static final String MODEL = "minilm-l6-v2-384";

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private DSLContext ctx;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection c = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(c);
        }
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
    }

    @AfterAll
    void stop() throws Exception {
        if (su != null) {
            su.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    private static String name(String owner) {
        return "knowledge__" + owner + "__" + MODEL + "__v1";
    }

    private long idOf(String tenant, String name) {
        return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne(0, Long.class);
    }

    /** The registry rows of {@code tenant} as {@code name|id|superseded_by|xmin}, sorted: a rewritten row changes its xmin. */
    private List<String> registry(String tenant) {
        return ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.SUPERSEDED_BY, DSL.field(DSL.name("xmin"), String.class))
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .fetch(r -> r.value1() + "|" + r.value2() + "|" + r.value3() + "|" + r.value4()).stream().sorted().toList();
    }

    /** Registers {@code source} and {@code target}, the target a tombstone of the source (empty, as a rename before gate 3 leaves it). */
    private void sourceAndTombstone(String tenant, String source, String target) {
        PgContainerHelper.insertCollection(ctx, tenant, source);   // the source first: its id is below the tombstone's
        PgContainerHelper.insertCollection(ctx, tenant, target);
        ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, source).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, idOf(tenant, source))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(target)).execute();
        assertThat(ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(target)).fetchOne(0, String.class))
            .as("non-vacuity: the target is a tombstone").isEqualTo(source);
    }

    private void renameWithNoRefusalFromTheCallersChecks(String tenant, String source, String target) {
        CollectionOneRowRename.apply(ctx, tenant, source, target, null, null, locked -> true, new String[5]);
    }

    /** A LIVE row at the new label: the caller's checks said nothing, so only the belt stands between the rename and a delete of a live collection. */
    @Test
    void aLiveRowAtTheNewLabelIsRefused_andNoRowChanges() {
        String t = "belt-live";
        String x = name("beltlivex");
        String y = name("beltlivey");
        PgContainerHelper.insertCollection(ctx, t, x);
        PgContainerHelper.insertCollection(ctx, t, y);
        List<String> before = registry(t);
        assertThat(before).as("non-vacuity: two live rows").hasSize(2);

        assertThatThrownBy(() -> renameWithNoRefusalFromTheCallersChecks(t, x, y))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .hasMessageContaining("became live while this rename ran");

        assertThat(registry(t)).as("both registry rows are the same rows (name, id, xmin)").isEqualTo(before);
    }

    /** A tombstone at the new label that holds a catalog document under its id (no chunk): the belt's child-table walk is what refuses. */
    @Test
    void aTombstoneHoldingADocumentUnderItsIdIsRefused_andTheDocumentAndRowsStay() {
        String t = "belt-doc";
        String x = name("beltdocx");
        String y = name("beltdocy");
        sourceAndTombstone(t, x, y);
        long yId = idOf(t, y);
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
            .values(t, "belt.doc.1", "a document of the tombstone", y, yId).execute();
        List<String> before = registry(t);

        assertThatThrownBy(() -> renameWithNoRefusalFromTheCallersChecks(t, x, y))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .hasMessageContaining("holds rows in catalog_documents");

        assertThat(registry(t)).as("both registry rows are the same rows (name, id, xmin)").isEqualTo(before);
        assertThat(ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID.eq(yId))))
            .as("the document is still the tombstone's").isEqualTo(1);
    }

    /** A tombstone at the new label with a queued aspect task naming the label: the belt's task check is what refuses. */
    @Test
    void aTombstoneWithAQueuedAspectTaskNamingTheLabelIsRefused_andTheTaskAndRowsStay() {
        String t = "belt-task";
        String x = name("belttaskx");
        String y = name("belttasky");
        sourceAndTombstone(t, x, y);
        String body = "{\"collection\":\"" + y + "\",\"source_path\":\"p/belt.md\",\"doc_id\":\"belt.doc\",\"content_hash\":\"h-belt\"}";
        ctx.insertInto(TUPLES, TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE, TUPLES.KEYS, TUPLES.BODY, TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
            .values("belt-task-1".getBytes(StandardCharsets.UTF_8), t, "aspects", "aspects", JSONB.valueOf("{\"queue\":\"aspects\"}"), body,
                OffsetDateTime.now().plusDays(1), OffsetDateTime.now())
            .execute();
        assertThat(AspectTaskRefs.countNaming(ctx, t, y)).as("non-vacuity: a queued task names the tombstone's label").isEqualTo(1);
        List<String> before = registry(t);

        assertThatThrownBy(() -> renameWithNoRefusalFromTheCallersChecks(t, x, y))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .hasMessageContaining("queued aspect tasks name the retired target");

        assertThat(registry(t)).as("both registry rows are the same rows (name, id, xmin)").isEqualTo(before);
        assertThat(AspectTaskRefs.countNaming(ctx, t, y)).as("the task still names the tombstone's label").isEqualTo(1);
    }
}
