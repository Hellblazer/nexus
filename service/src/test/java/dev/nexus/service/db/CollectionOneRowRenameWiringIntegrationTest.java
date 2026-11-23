// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), review S2b: the owner rule at its call site, {@code CollectionOneRowRename.apply}, on a migrated schema.
 * Each case registers a collection, calls {@code apply} directly and reads the registry row's owner. The name path ends on the documents'
 * owner when one exists and is free; the one-row path never re-derives (the documents' name column keeps the old label), so what it
 * keeps is decided by {@link CollectionOneRowRename#ownerTheRenameMayChange}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionOneRowRenameWiringIntegrationTest {

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

    private static String name(String type, String owner, int version) {
        return type + "__" + owner + "__" + MODEL + "__v" + version;
    }

    private String ownerOf(String tenant, String name) {
        return ctx.select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne(0, String.class);
    }

    private void document(String tenant, String tumbler, String collection) {
        long id = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(CATALOG_COLLECTIONS.NAME.eq(collection)).fetchOne(0, Long.class);
        ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "d " + tumbler).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, id).execute();
    }

    private void rename(String tenant, String oldName, String newName, String contentType, String owner) {
        CollectionOneRowRename.apply(ctx, tenant, oldName, newName, null, contentType, owner, locked -> false, new String[5]);
    }

    @Test
    void aSettledTumblerOwnerOfACodeCollectionSurvivesAClientSlug() {
        String t = "wire-settled";
        String old = name("code", "p37ws", 1);
        PgContainerHelper.insertCollection(ctx, t, old);
        document(t, "1.2.3", old);
        assertThat(ownerOf(t, old)).as("non-vacuity: the document's trigger settled the owner").isEqualTo("1-2");

        rename(t, old, name("code", "p37wt", 1), null, "p37wt");

        assertThat(ownerOf(t, name("code", "p37wt", 1))).as("the name path ends on the documents' owner; so does the one-row path").isEqualTo("1-2");
    }

    /**
     * A slug-owned code collection whose documents name tumbler owner 1-2, held back because another collection holds that tuple (the
     * catalog-051 guard kept the slug). Intended result: the client's slug, which is where the name path ends too (it re-derives, finds
     * 1-2 held and keeps the owner it was given).
     */
    @Test
    void aSlugOwnedCodeCollectionWhoseDocumentsNameAHeldTumblerOwnerTakesTheClientsSlug() {
        String t = "wire-held";
        PgContainerHelper.insertCollection(ctx, t, name("code", "1-2", 1));   // holds the (code, 1-2, model, v1) tuple
        String q = name("code", "p37wq", 1);
        PgContainerHelper.insertCollection(ctx, t, q);
        document(t, "1.2.9", q);
        assertThat(ownerOf(t, q)).as("non-vacuity: the derivation was held back, the slug stays").isEqualTo("p37wq");

        rename(t, q, name("code", "p37wr", 1), null, "p37wr");

        assertThat(ownerOf(t, name("code", "p37wr", 1))).isEqualTo("p37wr");
    }

    @Test
    void anEmptySlugOwnedCollectionTakesTheClientsOwner_slugOrTumblerShaped() {
        String t = "wire-empty";
        String a = name("code", "p37wa", 1);
        String b = name("code", "p37wb", 1);
        PgContainerHelper.insertCollection(ctx, t, a);
        PgContainerHelper.insertCollection(ctx, t, b);

        rename(t, a, name("code", "p37wc", 1), null, "p37wc");
        rename(t, b, name("code", "1-7", 1), null, "1-7");

        assertThat(ownerOf(t, name("code", "p37wc", 1))).isEqualTo("p37wc");
        assertThat(ownerOf(t, name("code", "1-7", 1))).as("an explicit tumbler-shaped owner is applied, as the name path applies it").isEqualTo("1-7");
    }

    @Test
    void aKnowledgeCollectionWithAnAllDigitOwnerTakesTheClientsOwner() {
        String t = "wire-knowledge";
        String old = name("knowledge", "123", 1);
        PgContainerHelper.insertCollection(ctx, t, old);
        assertThat(ownerOf(t, old)).as("non-vacuity: the owner is all digits, which only code, docs and rdr treat as settled").isEqualTo("123");

        rename(t, old, name("knowledge", "abc", 1), null, "abc");

        assertThat(ownerOf(t, name("knowledge", "abc", 1))).isEqualTo("abc");
    }
}
