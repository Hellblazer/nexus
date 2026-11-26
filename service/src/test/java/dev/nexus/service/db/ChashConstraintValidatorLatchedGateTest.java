// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RegistryRenameSupport;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.ChashConstraintValidator.Outcome;
import dev.nexus.service.db.ChashConstraintValidator.Step;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), test-strength follow-up 3 of the census evidence pass (bead comment of 2026-11-23 21:09):
 * {@link ChashConstraintValidator} had no latched-gate test. Its refusal reports the bad chunks, their owners and the manifest rows' reasons
 * by reading the collection through {@link LegacyChunkSurvey}, which takes the gate from the validator's constructor and reads the id from
 * the latch ({@code leafDetail}, {@code withOwners} and {@code manifestDetail} pass {@code gate3} on). {@code Rdr229P35cLegacySurveyByIdTest}
 * pins the survey; this class pins that the validator hands its own gate to it, which a validator built with {@link Gate3#never()} does not.
 *
 * <p>State. The chunk and the manifest row sit under the collection's name and id; the registry row is then renamed in place (the id stays).
 * <b>Leaf test: a reachable state.</b> The chunk keeps its old name, as after P3.7's one-row rename, so the bad chunk is listed under its stored
 * name before the latch and under the registry's current label from it; its owner's manifest row still names the old label too, as G17 has every
 * writer take a manifest row's name from its chunk ({@code ManifestChunkNames}, {@code CatalogRepository.insertManifestChunkRows},
 * {@code ManifestReplace}).
 * <b>Manifest test: a SYNTHETIC state.</b> Its manifest row names the NEW label while the chunk keeps the old name, so by name the row meets no
 * chunk ({@code manifest_without_chunk}) and by id it meets its chunk ({@code text_mismatch}). No Phase 3 writer produces a manifest row and a
 * chunk of one id under two names: G17 keeps them equal, and the ids are stamped in the same statements. Because they stay equal, the name join
 * and the id join give the same answer on every state a Phase 3 install can hold, so no reachable state discriminates them; this one is
 * written directly, with triggers off. It still pins what it can: that the validator hands its own gate to {@code LegacyChunkSurvey.manifestCounts}
 * and {@code manifestDocs} (a validator that drops it reads the name, and this test goes red); the survey's own id join is
 * {@code Rdr229P35cLegacySurveyByIdTest}'s. The test says nothing about a state a writer can reach.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashConstraintValidatorLatchedGateTest {

    private static final String TENANT = "t-chashlatch";
    private static final String COLLECTION = "knowledge__chashlatch__bge-base-en-v15-768__v1";
    private static final String RENAMED = "knowledge__chashlatch-renamed__bge-base-en-v15-768__v1";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final String LEGACY_HEX = "b".repeat(32);
    private static final String DOC = "9.9";

    private static final String SVC_ROLE = "svc_chashlatch";
    private static final String SVC_PASS = "svc_chashlatch_pw";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;
    private long collectionId;

    private static final class Recorder implements ChashConstraintValidator.Listener {
        final List<Step> steps = new ArrayList<>();

        @Override
        public void planned(int total) {
        }

        @Override
        public void current(String table, Long bytes) {
        }

        @Override
        public void stepped(Step step) {
            steps.add(step);
        }

        @Override
        public String stopReason() {
            return null;
        }
    }

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(ctx, "tok-chashlatch-0123456789abcdef0123", TENANT, "a");
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT);
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("fk_catalog_chunks_chunk").execute();
            ctx.alterTable(CHUNKS).dropConstraintIfExists("chunks_chash_octet_check").execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("catalog_document_chunks_chash_octet_check").execute();
            PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
            collectionId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLLECTION))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            // one 16-byte chash in the chunks, and in the manifest of document 9.9, both under the collection's name and id
            byte[] legacy = HexFormat.of().parseHex(LEGACY_HEX);
            ctx.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, COLLECTION)
                .set(CHUNKS.COLLECTION_ID, collectionId).set(CHUNKS.CHASH, legacy).set(CHUNKS.EMBEDDING_MODEL, MODEL)
                .set(CHUNKS.CHUNK_TEXT, "poisoned").set(CHUNKS.EMBEDDING_768, Vector.of(new float[768])).execute();
            ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, DOC)
                .set(CATALOG_DOCUMENTS.TITLE, "doc " + DOC).onConflictDoNothing().execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, DOC).set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, legacy).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, COLLECTION)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, collectionId).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL).execute();
            // the one-row rename: the registry row bears the new label and keeps its id; every child row keeps the old name (G17)
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(ctx);
            RegistryRenameSupport.rename(ctx, TENANT, COLLECTION, RENAMED);
            PgContainerHelper.readdChashOctetChecksNotValid(su);
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * Writes {@code name} into the manifest row's name column directly: triggers off (session_replication_role) and the legacy-width CHECKs off for
     * the update, because a NOT VALID CHECK still refuses an UPDATE of a row with a 16-byte chash.
     */
    private void setManifestName(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraintIfExists("chunks_chash_octet_check").execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("catalog_document_chunks_chash_octet_check").execute();
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                int n = ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, name)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(DOC)).execute();
                assertThat(n).as("setup: the manifest row's name is written").isEqualTo(1);
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
                PgContainerHelper.readdChashOctetChecksNotValid(su);
            }
        }
    }

    private ChashConstraintValidator validator(Gate3 gate3) {
        if (svcDs == null) {
            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(SVC_ROLE);
            cfg.setPassword(SVC_PASS);
            cfg.setMaximumPoolSize(3);
            cfg.setAutoCommit(true);
            svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        }
        return new ChashConstraintValidator(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()),
            new TenantScope(svcDs), () -> List.of(TENANT), gate3);
    }

    /** A dry run's two refusals: the leaf of the tenant's chunks and the manifest. */
    private record Refusals(Step leaf, Step manifest) {
    }

    private Refusals refusals(Gate3 gate3) throws Exception {
        Recorder dry = new Recorder();
        var result = validator(gate3).run(true, dry);
        assertThat(result.stopped()).isNull();
        var refused = dry.steps.stream().filter(s -> s.outcome() == Outcome.REFUSED).toList();
        assertThat(refused).as("non-vacuity: the leaf and the manifest each refuse their one bad row: " + dry.steps).hasSize(2);
        Step leaf = refused.stream().filter(s -> s.table().startsWith("nexus.chunks_")).findFirst().orElseThrow();
        Step manifest = refused.stream().filter(s -> s.table().equals("nexus.catalog_document_chunks")).findFirst().orElseThrow();
        assertThat(leaf.exampleChashes()).containsExactly(LEGACY_HEX);
        return new Refusals(leaf, manifest);
    }

    /**
     * Guards the {@code gate3} the validator hands to {@code LegacyChunkSurvey.counts} and {@code chunks} (the bad chunk's label and id; its
     * owner lookup then goes by that id). The control is the validator built with {@link Gate3#never()} over the same rows: the name path,
     * which lists the chunk under its stored name.
     */
    @Test
    void aLatchedValidatorListsTheBadChunkOfARenamedCollectionUnderItsCurrentLabelAndOwnsItById() throws Exception {
        Refusals byName = refusals(Gate3.never());
        assertThat(byName.leaf().detail().chunks()).hasSize(1);
        assertThat(byName.leaf().detail().chunks().get(0).collection()).as("control: the chunk's stored name").isEqualTo(COLLECTION);

        Refusals byId = refusals(() -> true);

        assertThat(byId.leaf().detail().chunks()).hasSize(1);
        var chunk = byId.leaf().detail().chunks().get(0);
        assertThat(chunk.collection()).as("latched: the bad chunk is listed under the registry's current label of its id").isEqualTo(RENAMED);
        assertThat(chunk.collectionId()).isEqualTo(collectionId);
        assertThat(chunk.owners().owners()).as("and its owner is found by the id").extracting(o -> o.docId()).containsExactly(DOC);
    }

    /**
     * Guards the {@code gate3} the validator hands to {@code manifestCounts} and {@code manifestDocs}: the manifest row's join to its chunk.
     * By name the row (named by the new label) meets no chunk (named by the old); by id it meets its chunk.
     */
    @Test
    void aLatchedValidatorJoinsTheManifestRowOfARenamedCollectionToItsChunkById_onASyntheticState() throws Exception {
        setManifestName(RENAMED);   // synthetic: see the class comment; undone below so the leaf test's state is the reachable one
        Refusals byName;
        Refusals byId;
        try {
            byName = refusals(Gate3.never());
            assertThat(byName.manifest().detail().reasonCounts()).as("control: by name the manifest row meets no chunk")
                .containsOnlyKeys("manifest_without_chunk");

            byId = refusals(() -> true);
        } finally {
            setManifestName(COLLECTION);
        }

        assertThat(byId.manifest().detail().reasonCounts()).as("latched: the manifest row meets its chunk on the id")
            .containsOnlyKeys("text_mismatch").containsEntry("text_mismatch", 1L);
        assertThat(byId.manifest().detail().documents()).containsOnlyKeys("text_mismatch");
        assertThat(byId.manifest().detail().documents().get("text_mismatch").docs()).extracting(d -> d.docId()).containsExactly(DOC);
    }
}
