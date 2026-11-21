// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

/**
 * RDR-229 (Sam 2026-11-21, "Owner is the source"; bead nexus-p2m1x.14): catalog-051-1 amends catalog-044. A collection
 * registered under a CURATOR owner keeps it: neither catalog-044's AFTER trigger on {@code catalog_documents} (a document
 * lands) nor its BEFORE trigger on {@code catalog_collections} (the owner_id is written) re-derives it from the documents. An
 * owner_id that is not tumbler-shaped (the nexus-6pbwx legacy slug) is still derived from the documents.
 *
 * <p>Falsifier, by hand: remove the catalog-051 include from db.changelog-master.xml and the curator cases below read the
 * repo owner of the documents (1-33), which is catalog-044's behaviour; the slug case reads the same either way.
 */
class Catalog051CuratorOwnerNotReplaceableTest {

    private static final String TENANT = "catalog049-tenant";

    private static final String CURATOR_DOCS = "docs__corpus-cafe0001__voyage-context-3__v1";
    private static final String CURATOR_RESET = "docs__reset-cafe0002__voyage-context-3__v1";
    private static final String CURATOR_LATE = "docs__late-cafe0003__voyage-context-3__v1";
    private static final String SLUG_CODE = "code__arcaneum-2ad2825c__voyage-code-3__v1";

    @Test
    void aCuratorOwnedCollectionKeepsItsOwner_aSlugOwnedOneIsStillDerivedFromItsDocuments() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            owner(ctx, "1.32", "knowledge", "curator");
            owner(ctx, "1.33", "some-repo", "repo");

            // (1) the AFTER trigger: a repo owner's documents land in a curator-owned collection
            collection(ctx, CURATOR_DOCS, "docs", "1-32");
            document(ctx, "1.33.1", CURATOR_DOCS);
            document(ctx, "1.33.2", CURATOR_DOCS);
            assertThat(ownerOf(ctx, CURATOR_DOCS)).as("documents landing do not re-own a curator's collection")
                .isEqualTo("1-32");

            // (2) the BEFORE trigger: the owner_id is written again while documents exist
            collection(ctx, CURATOR_RESET, "docs", "1-3");
            document(ctx, "1.33.9", CURATOR_RESET);
            assertThat(ownerOf(ctx, CURATOR_RESET)).as("a tumbler-shaped owner stands").isEqualTo("1-3");
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.OWNER_ID, "1-32")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(CURATOR_RESET)).execute();
            assertThat(ownerOf(ctx, CURATOR_RESET)).as("re-setting the owner_id to a curator's segment is kept")
                .isEqualTo("1-32");

            // (3) the BEFORE trigger on INSERT: the documents already exist when the collection registers
            document(ctx, "1.33.7", CURATOR_LATE);
            collection(ctx, CURATOR_LATE, "docs", "1-32");
            assertThat(ownerOf(ctx, CURATOR_LATE)).as("a registration over existing documents keeps its owner")
                .isEqualTo("1-32");

            // (4) the nexus-6pbwx legacy case is unchanged: a slug is derived from the documents
            collection(ctx, SLUG_CODE, "code", "arcaneum-2ad2825c");
            document(ctx, "1.15.1", SLUG_CODE);
            assertThat(ownerOf(ctx, SLUG_CODE)).as("a slug owner is still replaceable").isEqualTo("1-15");
        } finally {
            pg.stop();
        }
    }

    private static void owner(DSLContext ctx, String prefix, String name, String type) {
        ctx.insertInto(CATALOG_OWNERS, CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME,
                CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.NEXT_SEQ)
            .values(TENANT, prefix, name, type, "", 0L).execute();
    }

    private static void collection(DSLContext ctx, String name, String contentType, String owner) {
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(TENANT, name, contentType, owner,
                contentType.contains("code") ? "voyage-code-3" : "voyage-context-3", "v1", "live")
            .execute();
    }

    private static void document(DSLContext ctx, String tumbler, String collection) {
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
            .values(TENANT, tumbler, "doc " + tumbler, collection).execute();
    }

    private static String ownerOf(DSLContext ctx, String name) {
        String owner = ctx.select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.OWNER_ID);
        assertThat(owner).as("row %s exists", name).isNotNull();
        return owner;
    }
}
