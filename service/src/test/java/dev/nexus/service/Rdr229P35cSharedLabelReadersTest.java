// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.IndexRunCensusReads;
import dev.nexus.service.db.LegacyChunkSurvey;
import dev.nexus.service.db.PdfStubMetadataReads;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), review fix S1: the readers carry the collection's ID through their round trips, so a registry
 * label shared by a tombstone and a live row (the state of a P3.6 install, built here by dropping the name keys of this class's own
 * container) never sends one collection's rows to the other. Each reader has data under BOTH rows and must report each under its own.
 */
class Rdr229P35cSharedLabelReadersTest extends Rdr229P35cRestFixture {

    private static final Gate3 LATCHED = () -> true;
    private static final String SHARED_TENANT = "p35c-shared-label";

    @BeforeAll
    void dropTheNameKeys() throws Exception {
        su(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (key.getFields().contains(CATALOG_COLLECTIONS.NAME)) {
                    ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(DSL.constraint(key.getName())).cascade().execute();
                }
            }
            return null;
        });
    }

    /** A live collection {@code label} and a tombstone of the same label with data of its own: {live id, tombstone id}. */
    private long[] sharedLabel(String type, String label) throws Exception {
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, SHARED_TENANT, label);
            return null;
        });
        long live = registryId(SHARED_TENANT, label);
        return new long[] {live, tombstoneOf(label, live)};
    }

    private long tombstoneOf(String label, long live) throws Exception {
        return su(ctx -> {
            var row = ctx.selectFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.COLLECTION_ID.eq(live)).fetchOne().copy();
            row.setTenantId(SHARED_TENANT);
            row.setName(label);
            row.setSupersededBy("successor-" + label);
            row.setSupersededById(live);
            row.changed(true);
            row.changed(CATALOG_COLLECTIONS.COLLECTION_ID, false);
            ctx.executeInsert(row);
            return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(SHARED_TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label))
                .and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNotNull()).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        });
    }

    @Test
    void pdfStubMetadataReads_reportsEachRowOfASharedLabelUnderItsOwnData() throws Exception {
        String label = "knowledge__p35cshared-pdf__" + MODEL + "__v1";
        long[] ids = sharedLabel("knowledge", label);
        stubPdf(label, ids[0], 2);    // the live row holds two stub PDFs
        stubPdf(label, ids[1], 1);    // the tombstone one
        var entries = new PdfStubMetadataReads(repo, LATCHED).read(SHARED_TENANT, 10).collections().stream()
            .filter(c -> label.equals(c.collection())).map(PdfStubMetadataReads.CollectionStubs::stubChunks).sorted().toList();
        assertThat(entries).as("two list entries for the label, each with its own row's stubs").containsExactly(1, 2);
    }

    private void stubPdf(String label, long id, int n) throws Exception {
        for (int i = 0; i < n; i++) {
            byte[] chash = nextChash();
            insertChunk(SHARED_TENANT, label, id, chash, "{\"title\":\"\",\"content_hash\":\"deadbeefcafe0123\"}");
            String tumbler = "9." + seq.incrementAndGet() + "." + id;
            insertDocument(SHARED_TENANT, tumbler, label, id, "pdf", "/p35c/" + tumbler + ".pdf",
                "file:///p35c/" + tumbler + ".pdf", "", "");
            insertManifest(SHARED_TENANT, tumbler, 0, label, id, chash);
        }
    }

    @Test
    void indexRunCensusReads_readsEachDocumentUnderItsOwnCollectionId() throws Exception {
        String label = "code__p35cshared-ir__" + MODEL + "__v1";
        long[] ids = sharedLabel("code", label);
        String liveDoc = "9." + seq.incrementAndGet() + ".1";
        String oldDoc = "9." + seq.incrementAndGet() + ".2";
        insertChunk(SHARED_TENANT, label, ids[0], nextChash(), "{\"content_hash\":\"h-live\"}");
        insertChunk(SHARED_TENANT, label, ids[1], nextChash(), "{\"content_hash\":\"h-old\"}");
        insertDocument(SHARED_TENANT, liveDoc, label, ids[0], "code", "/p35c/live.py", "", "failed", "h-live");
        insertDocument(SHARED_TENANT, oldDoc, label, ids[1], "code", "/p35c/old.py", "", "failed", "h-old");
        assertThat(new IndexRunCensusReads(repo, LATCHED).read(SHARED_TENANT, 50).failedHidden().value().examples())
            .as("a label resolved back to an id would send the retired document's hash to the live row's chunks")
            .extracting(IndexRunCensusReads.HiddenDoc::tumbler).containsExactlyInAnyOrder(liveDoc, oldDoc);
    }

    @Test
    void legacyChunkSurvey_ownersByIdNameTheOwnersOfTheirOwnRow() throws Exception {
        String label = "code__p35cshared-lg__" + MODEL + "__v1";
        long[] ids = sharedLabel("code", label);
        byte[] liveChash = nextChash();
        byte[] oldChash = nextChash();   // chunks are keyed by the label, so each row's chunk has its own chash
        String liveDoc = "9." + seq.incrementAndGet() + ".3";
        String oldDoc = "9." + seq.incrementAndGet() + ".4";
        insertChunk(SHARED_TENANT, label, ids[0], liveChash, "{}");
        insertChunk(SHARED_TENANT, label, ids[1], oldChash, "{}");
        insertDocument(SHARED_TENANT, liveDoc, label, ids[0], "code", "", "", "", "");
        insertDocument(SHARED_TENANT, oldDoc, label, ids[1], "code", "", "", "", "");
        insertManifest(SHARED_TENANT, liveDoc, 0, label, ids[0], liveChash);
        insertManifest(SHARED_TENANT, oldDoc, 0, label, ids[1], oldChash);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(LegacyChunkSurvey.ownersById(ctx, SHARED_TENANT, ids[1], MODEL, oldChash, 5, c -> true).owners())
                .as("the retired row's chunk: its own owner, by id").extracting(LegacyChunkSurvey.Owner::docId).containsExactly(oldDoc);
            assertThat(LegacyChunkSurvey.owners(ctx, SHARED_TENANT, label, MODEL, oldChash, 5, c -> true, LATCHED).owners())
                .as("by label the live row is meant, which holds no such chunk: the round trip would lose the owner").isEmpty();
            assertThat(LegacyChunkSurvey.ownersById(ctx, SHARED_TENANT, ids[0], MODEL, liveChash, 5, c -> true).owners())
                .extracting(LegacyChunkSurvey.Owner::docId).containsExactly(liveDoc);
            assertThat(LegacyChunkSurvey.owners(ctx, SHARED_TENANT, label, MODEL, liveChash, 5, c -> true, LATCHED).owners())
                .extracting(LegacyChunkSurvey.Owner::docId).containsExactly(liveDoc);
        }
    }
}
