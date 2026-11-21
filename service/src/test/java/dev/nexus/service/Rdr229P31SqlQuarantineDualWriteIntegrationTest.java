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
 * RDR-229 P3.1 stage 2b, the SQL family of the dual write (nexus-p2m1x.17), quarantine moves, restores and expiries: gc_quarantine_orphans, _bounded and _floored, reaper_quarantine_chunks, gc_restore_rereferenced and _bounded, quarantine_restore_chunks, gc_expire_quarantine and _bounded, reaper_expire_quarantine, reaper_expire_client_quarantine, plus purge_trash left alone. The three-state method is
 * {@link P31SqlDualWriteSupport#walk}'s: the seeds run before vectors-068 (unstamped rows), each scenario's call is then refused
 * with 23514 by a collection id CHECK before vectors-069 where the old body writes a guarded row, and the same call succeeds after
 * it and carries the registry's id (compared with the registry, never a literal). The attribute, rollback and census checks are in
 * {@code Rdr229P31SqlCatalogDualWriteIntegrationTest}.
 */
class Rdr229P31SqlQuarantineDualWriteIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p31_sqlq";

    private static List<Scenario> scenarios() {
        List<Scenario> s = new ArrayList<>();
        // quarantine moves: the sibling's chunk rows and the audit row
        s.add(move("gc_quarantine_orphans (sibling already holds a legacy row)", "mv1", "gc_quarantine_orphans", true,
            (e, o, q) -> e.ctx().selectFrom(GC_QUARANTINE_ORPHANS.call(768, T, o, q, TS, 20)).fetch(), null));
        s.add(move("gc_quarantine_orphans_bounded (sibling already holds a legacy row)", "mv2", "gc_quarantine_orphans_bounded", true,
            (e, o, q) -> e.ctx().selectFrom(GC_QUARANTINE_ORPHANS_BOUNDED.call(768, T, o, q, TS, 20, 100)).fetch(), null));
        s.add(move("gc_quarantine_orphans_floored (move with expiry)", "mv3", "gc_quarantine_orphans",
            (e, o, q) -> e.ctx().selectFrom(GC_QUARANTINE_ORPHANS_FLOORED.call(768, T, o, q, TS, 20, null, null, null, false, EXPIRES)).fetch(),
            (e, q, hash) -> assertThat(e.ctx().select(CHUNKS.METADATA).from(CHUNKS)
                    .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(q)).and(CHUNKS.CHASH.eq(hash)).fetchOne(CHUNKS.METADATA).data())
                .as("the expiry stamp the wrapper writes is still written").contains(EXPIRES)));
        s.add(move("reaper_quarantine_chunks (sibling already holds a legacy row)", "mv4", "reaper_quarantine", true,
            (e, o, q) -> e.ctx().selectFrom(REAPER_QUARANTINE_CHUNKS.call(T, o, q, TS, 100, null, 1.0, 0, false)).fetch(), null));
        // restores
        s.add(restore("gc_restore_rereferenced", "rs1", "gc_restore_rereferenced",
            (e, q, o) -> Routines.gcRestoreRereferenced(e.ctx().configuration(), 768, T, q, o)));
        s.add(restore("gc_restore_rereferenced_bounded", "rs2", "gc_restore_rereferenced_bounded",
            (e, q, o) -> e.ctx().selectFrom(GC_RESTORE_REREFERENCED_BOUNDED.call(768, T, q, o, 100)).fetch()));
        s.add(quarantineRestore());
        // expiries: history rows only
        s.add(expiry("gc_expire_quarantine", "ex1", "gc_expire_quarantine", false, false,
            (e, q, o) -> e.ctx().selectFrom(GC_EXPIRE_QUARANTINE.call(768, T, q, o, "2099-01-01T00:00:00Z", 1.0, 0, true)).fetch()));
        s.add(expiry("gc_expire_quarantine_bounded", "ex2", "gc_expire_quarantine_bounded", false, false,
            (e, q, o) -> e.ctx().selectFrom(GC_EXPIRE_QUARANTINE_BOUNDED.call(768, T, q, o, "2099-01-01T00:00:00Z", 1.0, 0, true, 100, false)).fetch()));
        s.add(expiry("reaper_expire_quarantine", "ex3", "reaper_expire_quarantine", true, false,
            (e, q, o) -> e.ctx().selectFrom(REAPER_EXPIRE_QUARANTINE.call(T, q, o, "2099-01-01T00:00:00Z", 100)).fetch()));
        s.add(expiry("reaper_expire_client_quarantine", "ex4", "reaper_expire_client_quarantine", false, true,
            (e, q, o) -> e.ctx().selectFrom(REAPER_EXPIRE_CLIENT_QUARANTINE.call(T, q, o, "2099-01-01T00:00:00Z", 100)).fetch()));
        s.add(flooredRefusal());
        s.add(purgeTrash());
        return s;
    }

    @Test
    void theQuarantineMovesRestoresAndExpiriesAreRefusedBeforeAndCarryTheIdAfter() throws Exception {
        List<Scenario> scenarios = scenarios();
        assertThat(scenarios).as("non-vacuity: the scenario list").hasSize(13);
        walk(ADMIN_ROLE, su -> PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS), scenarios, 7, null);
    }

    private interface MoveCall {
        void run(Env e, String origin, String quarantine) throws Exception;
    }

    private interface RestoreCall {
        void run(Env e, String quarantine, String origin) throws Exception;
    }

    private interface Extra {
        void run(Env e, String quarantine, byte[] chash);
    }

    /** An orphan chunk moves from {@code origin} to its quarantine sibling: the sibling's row and the audit row carry ids. */
    private static Scenario move(String name, String tag, String auditOp, MoveCall call, Extra extra) {
        return move(name, tag, auditOp, false, call, extra);
    }

    /**
     * With {@code siblingExists} the sibling is already registered and already holds a legacy (unstamped) row of the same chunk, so the
     * move takes the two ON CONFLICT DO UPDATE paths: the registry upsert (superseded_by_id) and the chunk upsert (collection_id).
     */
    private static Scenario move(String name, String tag, String auditOp, boolean siblingExists, MoveCall call, Extra extra) {
        String origin = docs(tag);
        String sibling = quarantine(origin);
        byte[][] hash = new byte[1][];
        return new Scenario(name, T, true,
            e -> {
                register(e, T, origin);
                hash[0] = chunk(e, T, origin, "orphan", 768, "{}", 90);
                if (siblingExists) {
                    register(e, T, sibling);
                    chunk(e, T, sibling, hash[0], "stale copy", 768, "{\"origin_collection\": \"" + origin + "\"}", 90);
                }
            },
            e -> call.run(e, origin, sibling),
            e -> {
                assertChunkId(e, T, sibling, hash[0]);
                assertThat(e.ctx().select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(sibling))
                        .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID))
                    .as("the sibling is live, so its superseded_by_id is NULL").isNull();
                assertAuditId(e, T, auditOp, origin, origin);
                assertThat(e.ctx().select(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection_id")).from(CHUNKS)
                        .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(sibling)).and(CHUNKS.CHASH.eq(hash[0]))
                        .fetchOne(0, String.class))
                    .as("the quarantine tag carries the origin's registry id beside origin_collection").isEqualTo(Long.toString(e.id(T, origin)));
                if (extra != null) extra.run(e, sibling, hash[0]);
            });
    }

    /** A quarantined chunk is named again by a document of the origin and comes back: the origin's chunk row and the audit row carry ids. */
    private static Scenario restore(String name, String tag, String auditOp, RestoreCall call) {
        String origin = docs(tag);
        String sibling = quarantine(origin);
        byte[][] hash = new byte[1][];
        return new Scenario(name, T, true,
            e -> {
                register(e, T, origin);
                register(e, T, sibling);
                hash[0] = chunk(e, T, sibling, "held", 768, "{\"origin_collection\": \"" + origin + "\", \"quarantined_at\": \"" + TS + "\"}", 1);
                PgContainerHelper.insertCatalogDocument(e.ctx(), T, tag + ".1");
                manifestWithoutChunk(e, T, tag + ".1", 0, hash[0], origin);
            },
            e -> call.run(e, sibling, origin),
            e -> {
                assertChunkId(e, T, origin, hash[0]);
                assertAuditId(e, T, auditOp, sibling, sibling);
            });
    }

    /** quarantine_restore_chunks with a reattach: the restored chunk, its reattached manifest row and the audit row. */
    private static Scenario quarantineRestore() {
        String origin = docs("qr");
        String sibling = quarantine(origin);
        byte[][] hash = new byte[1][];
        String doc = "qr.1";
        return new Scenario("quarantine_restore_chunks", T, true,
            e -> {
                register(e, T, origin);
                register(e, T, sibling);
                hash[0] = chunk(e, T, sibling, "held",  768,
                    "{\"origin_collection\": \"" + origin + "\", \"quarantined_at\": \"" + TS + "\", \"catalog_doc_id\": \"" + doc
                        + "\", \"chunk_index\": 0}", 1);
                doc(e, T, doc, origin);
            },
            e -> e.ctx().selectFrom(QUARANTINE_RESTORE_CHUNKS.call(T, origin, sibling, new String[] {hex(hash[0])}, "p31", null, false, true)).fetch(),
            e -> {
                assertChunkId(e, T, origin, hash[0]);
                List<Long> manifestIds = e.ctx().select(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID).from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(T)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))
                    .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(origin)).fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID);
                assertThat(manifestIds).as("the reattach wrote the manifest row").hasSize(1);
                assertThat(manifestIds.get(0)).as("the manifest row's collection_id").isEqualTo(e.id(T, origin));
                assertAuditId(e, T, "quarantine_restore", origin, origin);
            });
    }

    private interface ExpiryCall {
        void run(Env e, String quarantine, String origin) throws Exception;
    }

    /** An expiry deletes quarantine rows and writes one gc_audit row, a history row with no CHECK: phase 3 only. */
    private static Scenario expiry(String name, String tag, String auditOp, boolean engineOwned, boolean knowledge, ExpiryCall call) {
        String origin = knowledge ? coll("knowledge", tag, M768) : docs(tag);
        String sibling = quarantine(origin);
        String meta = "{\"origin_collection\": \"" + origin + "\", \"quarantined_at\": \"2026-01-01T00:00:00Z\""
            + (engineOwned ? ", \"quarantined_by\": \"engine-reaper\", \"reaper_quarantined_at\": \"2026-01-01T00:00:00Z\"" : "") + "}";
        return new Scenario(name, T, false,
            e -> {
                register(e, T, origin);
                register(e, T, sibling);
                chunk(e, T, sibling, "old", 768, meta, 1);
            },
            e -> call.run(e, sibling, origin),
            e -> assertAuditId(e, T, auditOp, sibling, sibling));
    }

    /** The floor's refusal row is a gc_audit row too (a history row: phase 3 only). */
    private static Scenario flooredRefusal() {
        String origin = docs("fl");
        String sibling = quarantine(origin);
        return new Scenario("gc_quarantine_orphans_floored (refusal row)", T, false,
            e -> {
                register(e, T, origin);
                chunk(e, T, origin, "o1", 768, "{}", 90);
                chunk(e, T, origin, "o2", 768, "{}", 90);
            },
            e -> {
                var rows = e.ctx().selectFrom(GC_QUARANTINE_ORPHANS_FLOORED.call(768, T, origin, sibling, TS, 20, null, 0.1, 0, false, null)).fetch();
                assertThat(rows.get(0).get("refused", Boolean.class)).as("the floor refused the move").isTrue();
            },
            e -> assertAuditId(e, T, "gc_quarantine_orphans_refused", origin, origin));
    }

    /** purge_trash names no collection ('' means unset, so its id stays NULL): not redefined, asserted unchanged. */
    private static Scenario purgeTrash() {
        String tenant = "p31sql-pt";
        return new Scenario("purge_trash (writes '' and NULL)", tenant, false,
            e -> {
                doc(e, tenant, "pt.1", "");
                e.ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now().minusDays(400))
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).execute();
            },
            e -> Routines.purgeTrash(e.ctx().configuration(), YearToSecond.valueOf(Duration.ofSeconds(1))),
            e -> {
                List<org.jooq.Record2<String, Long>> rows = e.ctx().select(GC_AUDIT.COLLECTION, GC_AUDIT.COLLECTION_ID).from(GC_AUDIT)
                    .where(GC_AUDIT.TENANT_ID.eq(tenant)).and(GC_AUDIT.OPERATION.eq("purge_trash")).fetch();
                assertThat(rows).as("purge_trash wrote its audit row").hasSize(1);
                assertThat(rows.get(0).value1()).isEmpty();
                assertThat(rows.get(0).value2()).as("no collection, no id").isNull();
            });
    }
}
