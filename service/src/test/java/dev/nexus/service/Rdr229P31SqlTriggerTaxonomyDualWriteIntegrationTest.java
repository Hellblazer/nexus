// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.jooq.nexus.Routines;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.types.YearToSecond;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_1024;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_384;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_768;
import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.GC_EXPIRE_QUARANTINE_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_FLOORED;
import static dev.nexus.service.jooq.nexus.Tables.GC_RESTORE_REREFERENCED_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.QUARANTINE_RESTORE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_CLIENT_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_EXPIRE_QUARANTINE;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_QUARANTINE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS_RECOUNT_DOC_COUNT;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.P31SqlDualWriteSupport.Act;
import dev.nexus.service.P31SqlDualWriteSupport.Env;
import dev.nexus.service.P31SqlDualWriteSupport.Scenario;

import static dev.nexus.service.P31SqlDualWriteSupport.*;

/**
 * RDR-229 P3.1 stage 2b, the SQL family of the dual write (nexus-p2m1x.17), the manifest triggers and the taxonomy writers: stamp_chunks_on_manifest_delete and _update, assign_from_chashes_384, _768 and _1024, topics_doc_count_recount_ins and _del, topics_recount_doc_count. The three-state method is
 * {@link P31SqlDualWriteSupport#walk}'s: the seeds run before vectors-068 (unstamped rows), each scenario's call is then refused
 * with 23514 by a collection id CHECK before vectors-069 where the old body writes a guarded row, and the same call succeeds after
 * it and carries the registry's id (compared with the registry, never a literal). The attribute, rollback and census checks are in
 * {@code Rdr229P31SqlCatalogDualWriteIntegrationTest}.
 */
class Rdr229P31SqlTriggerTaxonomyDualWriteIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p31_sqlt";

    private static List<Scenario> scenarios() {
        List<Scenario> s = new ArrayList<>();
        // chunk_orphaned_at triggers
        s.add(stampDelete());
        s.add(stampUpdate());
        // taxonomy
        s.add(assign(384, "minilm-l6-v2-384", "as384"));
        s.add(assign(768, M768, "as768"));
        s.add(assign(1024, "voyage-code-3", "as1024"));
        s.add(topicsTrigger("topics_doc_count_recount_ins", "tp1", false));
        s.add(topicsTrigger("topics_doc_count_recount_del", "tp2", true));
        s.add(topicsRecount());
        return s;
    }

    @Test
    void theManifestTriggersAndTheTaxonomyWritersAreRefusedBeforeAndCarryTheIdAfter() throws Exception {
        List<Scenario> scenarios = scenarios();
        assertThat(scenarios).as("non-vacuity: the scenario list").hasSize(8);
        walk(ADMIN_ROLE, su -> PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS), scenarios, 8, null);
    }

    /** DELETE of a manifest row: the trigger records the dropped chunk in chunk_orphaned_at with the id. */
    private static Scenario stampDelete() {
        String origin = docs("sd");
        byte[][] hash = new byte[1][];
        return new Scenario("stamp_chunks_on_manifest_delete", T, true,
            e -> {
                register(e, T, origin);
                hash[0] = chunk(e, T, origin, "kept", 768, "{}", 90);
                PgContainerHelper.insertCatalogDocument(e.ctx(), T, "sd.1");
                manifest(e, T, "sd.1", 0, hash[0], origin);
            },
            e -> e.ctx().deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("sd.1")).execute(),
            e -> assertOrphanId(e, origin, hash[0]));
    }

    /** UPDATE of a manifest row to another chunk: the old one is recorded in chunk_orphaned_at with the id. */
    private static Scenario stampUpdate() {
        String origin = docs("su");
        byte[][] hash = new byte[2][];
        return new Scenario("stamp_chunks_on_manifest_update", T, true,
            e -> {
                register(e, T, origin);
                hash[0] = chunk(e, T, origin, "dropped", 768, "{}", 90);
                hash[1] = chunk(e, T, origin, "taken", 768, "{}", 90);
                PgContainerHelper.insertCatalogDocument(e.ctx(), T, "su.1");
                manifest(e, T, "su.1", 0, hash[0], origin);
            },
            // the manifest row is itself a guarded row: the statement carries its id, so only the trigger's insert can be refused
            e -> e.ctx().update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, hash[1])
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(T, origin))
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("su.1")).execute(),
            e -> assertOrphanId(e, origin, hash[0]));
    }

    private static void assertOrphanId(Env e, String origin, byte[] chash) {
        List<Long> ids = e.ctx().select(CHUNK_ORPHANED_AT.COLLECTION_ID).from(CHUNK_ORPHANED_AT)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(T)).and(CHUNK_ORPHANED_AT.COLLECTION.eq(origin)).and(CHUNK_ORPHANED_AT.CHASH.eq(chash))
            .fetch(CHUNK_ORPHANED_AT.COLLECTION_ID);
        assertThat(ids).as("the dropped chunk is recorded").hasSize(1);
        assertThat(ids.get(0)).as("chunk_orphaned_at.collection_id").isEqualTo(e.id(T, origin));
    }

    /**
     * assign_from_chashes_{dim}: the same-collection branch inserts a new assignment, the cross branch inserts one and, through
     * ON CONFLICT DO UPDATE, moves the id with the name (a legacy row the new similarity beats) or gives a legacy row the registry's id
     * when the name did not move (a legacy row that keeps its higher similarity).
     */
    private static Scenario assign(int dim, String model, String tag) {
        String tenant = "p31sql-" + tag;
        String own = coll("docs", tag + "-own", model);
        String src = coll("docs", tag + "-src", model);
        String other = coll("docs", tag + "-oth", model);
        byte[][] h = new byte[4][];
        long[] topic = new long[2];
        return new Scenario("assign_from_chashes_" + dim, tenant, true,
            e -> {
                register(e, tenant, own);
                register(e, tenant, src);
                register(e, tenant, other);
                h[0] = chunk(e, tenant, own, "same", dim, "{}", 1);
                h[1] = chunk(e, tenant, src, "fresh", dim, "{}", 1);
                h[2] = chunk(e, tenant, src, "beaten", dim, "{}", 1);
                h[3] = chunk(e, tenant, src, "keeps", dim, "{}", 1);
                topic[0] = topic(e, tenant, own, "own");
                centroid(e, tenant, own, topic[0], dim, -0.1f);   // far from every chunk, so the cross branch never picks it
                topic[1] = topic(e, tenant, other, "other");
                centroid(e, tenant, other, topic[1], dim, 0.1f);
                // legacy assignments of the cross branch: one the new similarity beats, one that keeps a similarity nothing beats
                assignment(e, tenant, h[2], topic[1], src, -0.5);
                assignment(e, tenant, h[3], topic[1], src, 5.0);
            },
            e -> {
                e.tenant(tenant);
                assignFrom(e, dim, own, new byte[][] {h[0]}, false, model, tenant);
                assignFrom(e, dim, src, new byte[][] {h[1], h[2], h[3]}, true, model, tenant);
            },
            e -> {
                assertThat(assignmentId(e, tenant, h[0], topic[0])).as("same-collection insert").isEqualTo(e.id(tenant, own));
                assertThat(assignmentId(e, tenant, h[1], topic[1])).as("cross insert").isEqualTo(e.id(tenant, src));
                assertThat(assignmentId(e, tenant, h[2], topic[1])).as("cross: the name moved, the id moved with it").isEqualTo(e.id(tenant, src));
                assertThat(assignmentId(e, tenant, h[3], topic[1])).as("cross: the name stayed, the legacy row took the registry's id").isEqualTo(e.id(tenant, src));
            });
    }

    private static void assignFrom(Env e, int dim, String coll, byte[][] chashes, boolean cross, String model, String tenant) {
        String[] hexes = new String[chashes.length];
        for (int i = 0; i < chashes.length; i++) hexes[i] = hex(chashes[i]);
        switch (dim) {
            case 384 -> e.ctx().selectFrom(ASSIGN_FROM_CHASHES_384.call(coll, hexes, cross, model, tenant)).fetch();
            case 768 -> e.ctx().selectFrom(ASSIGN_FROM_CHASHES_768.call(coll, hexes, cross, model, tenant)).fetch();
            default -> e.ctx().selectFrom(ASSIGN_FROM_CHASHES_1024.call(coll, hexes, cross, model, tenant)).fetch();
        }
    }

    /** The doc_count triggers' UPDATE of the topics row: a legacy topic row takes the id from the registry. */
    private static Scenario topicsTrigger(String name, String tag, boolean onDelete) {
        String tenant = "p31sql-" + tag;
        String own = docs(tag);
        byte[][] h = new byte[1][];
        long[] topic = new long[1];
        return new Scenario(name, tenant, true,
            e -> {
                register(e, tenant, own);
                h[0] = chunk(e, tenant, own, "member", 768, "{}", 1);
                topic[0] = topic(e, tenant, own, "t");
                if (onDelete) assignment(e, tenant, h[0], topic[0], own, 0.5);
            },
            e -> {
                if (onDelete) {
                    e.ctx().deleteFrom(TOPIC_ASSIGNMENTS).where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic[0])).execute();
                } else {
                    e.ctx().insertInto(TOPIC_ASSIGNMENTS)
                        .set(TOPIC_ASSIGNMENTS.TENANT_ID, tenant).set(TOPIC_ASSIGNMENTS.DOC_ID, h[0]).set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic[0])
                        .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "seed").set(TOPIC_ASSIGNMENTS.SIMILARITY, 0.5)
                        .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, own).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, M768)
                        .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(tenant, own))
                        .execute();
                }
            },
            e -> {
                assertThat(topicId(e, tenant, topic[0])).as("topics.collection_id after the trigger's UPDATE").isEqualTo(e.id(tenant, own));
                assertThat(e.ctx().select(TOPICS.DOC_COUNT).from(TOPICS).where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topic[0])).fetchOne(TOPICS.DOC_COUNT))
                    .as("the recount itself is unchanged").isEqualTo(onDelete ? 0 : 1);
            });
    }

    /** topics_recount_doc_count corrects a drifted doc_count: the corrected legacy topic row takes the id. */
    private static Scenario topicsRecount() {
        String tenant = "p31sql-tp3";
        String own = docs("tp3");
        byte[][] h = new byte[1][];
        long[] topic = new long[1];
        return new Scenario("topics_recount_doc_count", tenant, true,
            e -> {
                register(e, tenant, own);
                h[0] = chunk(e, tenant, own, "member", 768, "{}", 1);
                topic[0] = topic(e, tenant, own, "t");
                assignment(e, tenant, h[0], topic[0], own, 0.5);
                e.ctx().update(TOPICS).set(TOPICS.DOC_COUNT, 99).where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topic[0])).execute();
            },
            e -> e.ctx().selectFrom(TOPICS_RECOUNT_DOC_COUNT.call(tenant, false)).fetch(),
            e -> {
                assertThat(topicId(e, tenant, topic[0])).as("topics.collection_id after the recount").isEqualTo(e.id(tenant, own));
                assertThat(e.ctx().select(TOPICS.DOC_COUNT).from(TOPICS).where(TOPICS.TENANT_ID.eq(tenant)).and(TOPICS.ID.eq(topic[0])).fetchOne(TOPICS.DOC_COUNT))
                    .as("the recount corrected the drift").isEqualTo(1);
            });
    }
}
