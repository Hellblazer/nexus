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
 * RDR-229 P3.1 stage 2b, the SQL family of the dual write (nexus-p2m1x.17), the catalog and ring writers: catalog_reconcile_commit, document_trash, document_restore, catalog_documents_owner_to_collection, canary_query_ring_upsert, and the checks over the whole changeset (attributes, rollback, census). The three-state method is
 * {@link P31SqlDualWriteSupport#walk}'s: the seeds run before vectors-068 (unstamped rows), each scenario's call is then refused
 * with 23514 by a collection id CHECK before vectors-069 where the old body writes a guarded row, and the same call succeeds after
 * it and carries the registry's id (compared with the registry, never a literal). The attribute, rollback and census checks are in
 * {@code Rdr229P31SqlCatalogDualWriteIntegrationTest}.
 */
class Rdr229P31SqlCatalogDualWriteIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p31_sqlfn";

    private static List<Scenario> scenarios() {
        List<Scenario> s = new ArrayList<>();
        // catalog
        s.add(reconcile());
        s.add(trash());
        s.add(restoreDoc());
        s.add(ownerToCollection());
        s.add(canary());
        return s;
    }

    @Test
    void theCatalogAndRingWritersAreRefusedBeforeAndCarryTheIdAfter_everyFunctionKeepsItsAttributes_andTheRollbackRestoresTheBodies() throws Exception {
        List<Scenario> scenarios = scenarios();
        assertThat(scenarios).as("non-vacuity: the scenario list").hasSize(5);
        walk(ADMIN_ROLE, su -> PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS), scenarios, 5, (pg, ds, w) -> {
            var before = w.before();
            var after = w.after();
            // "after" is the whole migration, so it also holds what the changesets AFTER vectors-069 add (vectors-071-1, collections-001/-002);
            // each such function is named in ADDED_AFTER_DUAL_WRITE with the reason it writes no guarded table. Anything else that
            // appeared or went still fails here.
            Set<String> addedAfter = new TreeSet<>();
            for (String key : after.keySet()) {
                if (!before.containsKey(key)) addedAfter.add(key.substring(0, key.indexOf('(')));
            }
            assertThat(addedAfter).as("the functions that appeared after vectors-069 are exactly the named ones").isEqualTo(ADDED_AFTER_DUAL_WRITE);
            assertThat(before.keySet().stream().filter(k -> !after.containsKey(k)).toList()).as("no function went").isEmpty();
            Set<String> redefined = redefinedFunctions();
            int same = 0;
            int changed = 0;
            int laterChanged = 0;
            for (var entry : before.entrySet()) {
                var b = entry.getValue();
                var a = after.get(entry.getKey());
                String fn = entry.getKey().substring(0, entry.getKey().indexOf('('));
                if (REDEFINED_AFTER_DUAL_WRITE.contains(fn)) {
                    // a later changeset replaces the body (collections-003); the definition around it is untouched
                    laterChanged++;
                    assertThat(a.body()).as("%s has a new body from its later changeset", entry.getKey()).isNotEqualTo(b.body());
                    assertThat(a.arguments()).as("%s arguments", fn).isEqualTo(b.arguments());
                    assertThat(a.result()).as("%s result", fn).isEqualTo(b.result());
                    assertThat(a.volatility()).as("%s volatility", fn).isEqualTo(b.volatility());
                    assertThat(a.language()).as("%s language", fn).isEqualTo(b.language());
                    assertThat(a.securityDefiner()).as("%s SECURITY mode", fn).isEqualTo(b.securityDefiner());
                    assertThat(a.config()).as("%s SET clauses (search_path and friends)", fn).isEqualTo(b.config());
                    assertThat(a.owner()).as("%s owner", fn).isEqualTo(b.owner());
                    assertThat(a.acl()).as("%s grants", fn).isEqualTo(b.acl());
                } else if (redefined.contains(fn)) {
                    changed++;
                    assertThat(a.body()).as("%s has a new body", entry.getKey()).isNotEqualTo(b.body());
                    // attributes: everything the definition says other than the body
                    assertThat(a.arguments()).as("%s arguments", fn).isEqualTo(b.arguments());
                    assertThat(a.result()).as("%s result", fn).isEqualTo(b.result());
                    assertThat(a.volatility()).as("%s volatility", fn).isEqualTo(b.volatility());
                    assertThat(a.language()).as("%s language", fn).isEqualTo(b.language());
                    assertThat(a.securityDefiner()).as("%s SECURITY mode", fn).isEqualTo(b.securityDefiner());
                    assertThat(a.config()).as("%s SET clauses (search_path and friends)", fn).isEqualTo(b.config());
                    assertThat(a.owner()).as("%s owner", fn).isEqualTo(b.owner());
                    assertThat(a.acl()).as("%s grants", fn).isEqualTo(b.acl());
                } else {
                    same++;
                    assertThat(a.definition()).as("%s is not redefined by this changeset", entry.getKey()).isEqualTo(b.definition());
                    assertThat(a.acl()).as("%s grants", fn).isEqualTo(b.acl());
                }
            }
            assertThat(changed).as("non-vacuity: functions redefined").isEqualTo(24);
            assertThat(laterChanged).as("non-vacuity: functions a later changeset redefines").isEqualTo(REDEFINED_AFTER_DUAL_WRITE.size());
            assertThat(same).as("non-vacuity: functions compared and unchanged").isGreaterThan(100);

            // the rollback restores every previous body verbatim
            rollbackTo(ds, TAG);
            try (Connection su = pg.createConnection("")) {
                var restored = PgCatalogProbes.functionDefinitionsIn(DSL.using(su, SQLDialect.POSTGRES), "nexus");
                for (var entry : before.entrySet()) {
                    assertThat(restored.get(entry.getKey()).definition()).as("%s after the rollback", entry.getKey())
                        .isEqualTo(entry.getValue().definition());
                    assertThat(restored.get(entry.getKey()).acl()).as("%s grants after the rollback", entry.getKey())
                        .isEqualTo(entry.getValue().acl());
                }
            }
            assertThat(recorded(pg, DUAL_FIRST)).as("the changeset is no longer recorded after the rollback").isFalse();
            SchemaMigrator.migrate(ds); // and the walk goes forward again
        });
    }

    /**
     * The other real state: a tombstone with a blank model_version (D2's repair not yet run) keeps vectors-068-19 from adding the
     * superseded_by_id CHECK at all, by design, so nothing refuses the old body here; and the new body still stamps the id on the
     * tombstone it rewrites, because the dual write sets superseded_by_id whether or not the CHECK exists (vectors-068-2).
     */
    @Test
    void aTombstoneWithABlankModelVersion_keepsTheCheckWaiting_andTheDualWriteStampsItAnyway() throws Exception {
        Scenario blank = ownerToCollection("ow2", false);
        assertThat(blank.refusedBefore()).as("this state is not refused before vectors-069").isFalse();
        walk(ADMIN_ROLE + "_b", su -> PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE + "_b", ADMIN_PASS), List.of(blank), 0, (pg, ds, w) -> {
            try (Connection su = pg.createConnection("")) {
                assertThat(PgCatalogProbes.constraintCount(DSL.using(su, SQLDialect.POSTGRES), dev.nexus.service.datamove.CompletionCheck.SUPERSEDED_BY_CHECK))
                    .as("the CHECK waits while a blank-model_version tombstone has no id").isZero();
            }
        });
    }

    @Test
    void theChangelogRedefinesEveryCensusFunction_andNoOtherFunctionWritesAGuardedTable() throws Exception {
        Set<String> census = censusFunctions();
        Set<String> redefined = redefinedFunctions();
        assertThat(census).as("non-vacuity: the census names functions").hasSizeGreaterThanOrEqualTo(25);
        Set<String> expected = new TreeSet<>(census);
        expected.removeAll(NOT_REDEFINED);
        assertThat(redefined).as("the changelog redefines exactly the census functions less the ones left alone").isEqualTo(expected);
        assertThat(NOT_REDEFINED).as("every function left alone is in the census").isSubsetOf(census);

        // the live scan: a function body outside the changeset that inserts into or updates a guarded table is a writer the census missed
        Set<String> guarded = guardedTables();
        assertThat(guarded).as("non-vacuity: the guarded tables").hasSizeGreaterThanOrEqualTo(20);
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE + "_c", ADMIN_PASS);
            }
            try (HikariDataSource ds = adminPool(pg, ADMIN_ROLE + "_c")) {
                SchemaMigrator.migrate(ds);
            }
            try (Connection su = pg.createConnection("")) {
                var defs = PgCatalogProbes.functionDefinitionsIn(DSL.using(su, SQLDialect.POSTGRES), "nexus");
                Pattern writer = Pattern.compile("(?is)\\b(insert\\s+into|update)\\s+(?:nexus\\.)?(" + String.join("|", guarded) + ")\\b");
                Set<String> found = new TreeSet<>();
                int examined = 0;
                for (var d : defs.values()) {
                    if (!"plpgsql".equals(d.language()) && !"sql".equals(d.language())) continue;
                    examined++;
                    Matcher m = writer.matcher(stripComments(d.body()));
                    if (m.find()) found.add(d.identity().substring(0, d.identity().indexOf('(')));
                }
                assertThat(examined).as("non-vacuity: function bodies scanned").isGreaterThan(100);
                assertThat(found).as("non-vacuity: the scan sees the writers the changeset redefines (a scan that found none is a broken scan)")
                    .containsAll(Set.of("gc_quarantine_orphans", "catalog_reconcile_commit", "document_trash", "topics_recount_doc_count",
                        "stamp_chunks_on_manifest_delete", "canary_query_ring_upsert", "assign_from_chashes_768"));
                found.removeAll(redefined);
                found.removeAll(NOT_REDEFINED);
                assertThat(found).as("functions outside vectors-069 that write a guarded table (each needs the id, or a recorded reason)")
                    .isEqualTo(KNOWN_OTHER_WRITERS);
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * Functions that match the writer scan and need nothing from this changeset, each with the reason. The reader of the list can
     * check it against the scan: an addition here is a decision, not a default.
     */
    private static final Set<String> KNOWN_OTHER_WRITERS = new TreeSet<>(Set.of());

    /**
     * Functions that exist only after vectors-069. nexus.rdr229_restamp(text), added by vectors-071-1 (RDR-229 P3.1 fix round A),
     * writes one table, nexus.data_moves (the stamp move's re-registration), which is not a CHECK-guarded collection-name table; it
     * only READS the tables whose CHECKs it finishes (an EXISTS for an orphan row) and the registry. RDR-229 P3.8's changesets
     * (collections-001 and -002) add three: a read-only SECURITY INVOKER count over nexus.catalog_collections (collections-001-1),
     * and two SECURITY DEFINER functions that only read nexus.catalog_collections, one for max(collection_id) and the sequence's
     * last value and a nextval loop on the sequence (a sequence is not a guarded table), the other an EXISTS on collection_id
     * (collections-002). The live scan below flags a writer of a guarded table, and flags none of them.
     */
    private static final Set<String> ADDED_AFTER_DUAL_WRITE = new TreeSet<>(Set.of(
        "catalog_collection_tuple_duplicates_unscoped", "collection_id_seq_heal", "collection_id_taken", "rdr229_restamp"));

    /**
     * Functions vectors-069 does not touch whose body a later changeset replaces: collections-003-1 narrows
     * nexus.collection_tuple_lock_key to 256 stripes. Pure (IMMUTABLE, SQL, reads no table), so it writes nothing.
     */
    private static final Set<String> REDEFINED_AFTER_DUAL_WRITE = new TreeSet<>(Set.of("collection_tuple_lock_key"));

    /** catalog_reconcile_commit, one complete walk that reaches all six of its guarded statements, plus the trash of the renamed row. */
    private static Scenario reconcile() {
        String tenant = "p31sql-rc";
        String owner = "1.7";
        String run = "run-p31-1";
        String a = coll("code", "rca", "voyage-code-3");
        String b = coll("code", "rcb", "voyage-code-3");
        return new Scenario("catalog_reconcile_commit", tenant, true,
            e -> {
                register(e, tenant, a);
                register(e, tenant, b);
                e.ctx().insertInto(CATALOG_OWNERS)
                    .set(CATALOG_OWNERS.TENANT_ID, tenant).set(CATALOG_OWNERS.TUMBLER_PREFIX, owner).set(CATALOG_OWNERS.NAME, "own-1.7")
                    .set(CATALOG_OWNERS.OWNER_TYPE, "repo").set(CATALOG_OWNERS.REPO_ROOT, "/r").set(CATALOG_OWNERS.NEXT_SEQ, 10L).execute();
                legacyDoc(e, tenant, "1.7.1", "a.py", a, "{}");                                                   // updated (1)
                legacyDoc(e, tenant, "1.7.2", "gone.py", a, "{}");                                                // absent: a miss (5)
                legacyDoc(e, tenant, "1.7.3", "kept.py", a, "{\"miss_count\": 1}");                               // present, miss reset (6)
                legacyDoc(e, tenant, "1.7.4", "old.py", a, "{\"content_hash\": \"h1\", \"non_evidentiary\": true}"); // renamed away (4), trashed
                legacyDoc(e, tenant, "1.7.5", "new.py", a, "{\"content_hash\": \"h1\"}");                          // rename target (4)
                e.ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/moved.py")
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.7.5")).execute();
                legacyDoc(e, tenant, "1.7.6", "old/r.py", a, "{}");                                               // repointed (2)
                e.ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/r.py")
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.7.6")).execute();
                e.ctx().insertInto(CATALOG_RECONCILE_RUNS)
                    .set(CATALOG_RECONCILE_RUNS.TENANT_ID, tenant).set(CATALOG_RECONCILE_RUNS.RUN_ID, run)
                    .set(CATALOG_RECONCILE_RUNS.OWNER_TUMBLER, owner).set(CATALOG_RECONCILE_RUNS.WALK, "complete")
                    .set(CATALOG_RECONCILE_RUNS.EXPECTED_ROWS, 5).execute();
                staged(e, tenant, run, 1, "a.py", b, "H1", "");          // moved to b with a new head: update
                staged(e, tenant, run, 2, "kept.py", a, "H0", "");       // unchanged
                staged(e, tenant, run, 3, "new.py", a, "H0", "h1");      // the rename target, unchanged
                staged(e, tenant, run, 4, "r.py", b, "H0", "");          // same uri as 1.7.6, other collection: repoint
                staged(e, tenant, run, 5, "n.py", a, "H0", "");          // a new file: insert
            },
            e -> {
                e.tenant(tenant);
                Routines.catalogReconcileCommit(e.ctx().configuration(), owner, run);
            },
            e -> {
                assertThat(docId(e, tenant, "1.7.1")).as("(1) update moved the name, the id followed").isEqualTo(e.id(tenant, b));
                assertThat(docId(e, tenant, "1.7.6")).as("(2) repoint moved the name, the id followed").isEqualTo(e.id(tenant, b));
                assertThat(docId(e, tenant, "1.7.2")).as("(5) the miss count update set the id").isEqualTo(e.id(tenant, a));
                assertThat(docId(e, tenant, "1.7.3")).as("(6) the miss reset set the id").isEqualTo(e.id(tenant, a));
                assertThat(docId(e, tenant, "1.7.5")).as("(4) the rename flag update set the id").isEqualTo(e.id(tenant, a));
                assertThat(docId(e, tenant, "1.7.4")).as("document_trash of the renamed row set the id").isEqualTo(e.id(tenant, a));
                List<Long> created = e.ctx().select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.FILE_PATH.eq("n.py"))
                    .fetch(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
                assertThat(created).as("(3) the insert created the row").hasSize(1);
                assertThat(created.get(0)).as("(3) the insert carries the id").isEqualTo(e.id(tenant, a));
            });
    }

    private static void legacyDoc(Env e, String tenant, String tumbler, String filePath, String physical, String metadataJson) {
        e.ctx().insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, filePath).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "code").set(CATALOG_DOCUMENTS.FILE_PATH, filePath)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, physical).set(CATALOG_DOCUMENTS.HEAD_HASH, "H0")
            .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/" + filePath)
            .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(metadataJson))
            .execute();
    }

    private static void staged(Env e, String tenant, String run, int seq, String path, String physical, String head, String contentHash) {
        var step = e.ctx().insertInto(CATALOG_RECONCILE_ROWS)
            .set(CATALOG_RECONCILE_ROWS.TENANT_ID, tenant).set(CATALOG_RECONCILE_ROWS.RUN_ID, run).set(CATALOG_RECONCILE_ROWS.SEQ, seq)
            .set(CATALOG_RECONCILE_ROWS.KIND, "present").set(CATALOG_RECONCILE_ROWS.FILE_PATH, path)
            .set(CATALOG_RECONCILE_ROWS.CONTENT_TYPE, "code").set(CATALOG_RECONCILE_ROWS.TITLE, path)
            .set(CATALOG_RECONCILE_ROWS.PHYSICAL_COLLECTION, physical).set(CATALOG_RECONCILE_ROWS.HEAD_HASH, head);
        if (!contentHash.isEmpty()) {
            step = step.set(CATALOG_RECONCILE_ROWS.CONTENT_HASH, contentHash)
                .set(CATALOG_RECONCILE_ROWS.BASE_METADATA, JSONB.valueOf("{\"content_hash\": \"" + contentHash + "\"}"));
        }
        step.execute();
    }

    /** document_trash of an unstamped document: the UPDATE sets deleted_at and the id. */
    private static Scenario trash() {
        String tenant = "p31sql-dt";
        String own = docs("dt");
        return new Scenario("document_trash", tenant, true,
            e -> {
                register(e, tenant, own);
                doc(e, tenant, "dt.1", own);
            },
            e -> {
                e.tenant(tenant);
                Routines.documentTrash(e.ctx().configuration(), "dt.1");
            },
            e -> {
                assertThat(docId(e, tenant, "dt.1")).as("document_trash set the id").isEqualTo(e.id(tenant, own));
                assertThat(e.ctx().fetchExists(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq("dt.1"))
                    .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull()))).as("the document is trashed").isTrue();
            });
    }

    /** document_restore of an unstamped, trashed document. */
    private static Scenario restoreDoc() {
        String tenant = "p31sql-dr";
        String own = docs("dr");
        return new Scenario("document_restore", tenant, true,
            e -> {
                register(e, tenant, own);
                doc(e, tenant, "dr.1", own);
                e.ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).execute();
            },
            e -> {
                e.tenant(tenant);
                Routines.documentRestore(e.ctx().configuration(), "dr.1");
            },
            e -> {
                assertThat(docId(e, tenant, "dr.1")).as("document_restore set the id").isEqualTo(e.id(tenant, own));
                assertThat(e.ctx().fetchExists(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq("dr.1"))
                    .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))).as("the document is restored").isTrue();
            });
    }

    /**
     * The owner trigger of catalog-044 repairs a collection's owner_id from its documents with an UPDATE of the registry row. When
     * that row is an unstamped tombstone, the UPDATE must set superseded_by_id: the successor's id, or, residual R7, the row's own id
     * when the successor is gone.
     */
    /**
     * The tombstones of this scenario carry the model_version D2's repair writes (the name's trailing version, 'v1'), the state of
     * an install that has run vectors-064's repair move. vectors-068-19 adds the superseded_by_id CHECK only while no tombstone has
     * a blank model_version and no id (a repair UPDATE would meet it), so with a blank one (what a bare registry insert leaves) the
     * CHECK waits, develop's catalog-051-2 body updates the tombstone's owner without refusal, and this scenario's "refused before"
     * would be false: {@link #aTombstoneWithABlankModelVersion_keepsTheCheckWaiting_andTheDualWriteStampsItAnyway} covers that state.
     */
    private static Scenario ownerToCollection() {
        return ownerToCollection("ow", true);
    }

    private static Scenario ownerToCollection(String tag, boolean repaired) {
        String tenant = "p31sql-" + tag;
        String tomb = coll("code", tag + "tomb", "voyage-code-3").replace("p31sql-" + tag + "tomb", "arcane-" + tag + "tomb");
        String orphan = coll("code", tag + "orph", "voyage-code-3").replace("p31sql-" + tag + "orph", "arcane-" + tag + "orph");
        String successor = coll("code", tag + "succ", "voyage-code-3");
        return new Scenario("catalog_documents_owner_to_collection", tenant, repaired,
            e -> {
                register(e, tenant, tomb);
                register(e, tenant, orphan);
                register(e, tenant, successor);
                e.ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(tomb)).execute();
                e.ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "code__gone__voyage-code-3__v1")
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(orphan)).execute();
                if (repaired) {
                    e.ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.MODEL_VERSION, "v1")
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(tomb, orphan)).execute();
                }
            },
            e -> {
                // a live document of the collection with a tumbler-shaped owner: the trigger moves the registry row's owner
                e.ctx().insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, "1.9.1").set(CATALOG_DOCUMENTS.TITLE, "d1")
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, tomb)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(tenant, tomb)).execute();
                e.ctx().insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, "1.9.2").set(CATALOG_DOCUMENTS.TITLE, "d2")
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, orphan)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(tenant, orphan)).execute();
            },
            e -> {
                for (String name : List.of(tomb, orphan)) {
                    assertThat(e.ctx().select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
                            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne(CATALOG_COLLECTIONS.OWNER_ID))
                        .as("non-vacuity: the trigger ran and repaired the owner of %s", name).isEqualTo("1-9");
                }
                assertThat(supersededId(e, tenant, tomb)).as("a tombstone takes its successor's id").isEqualTo(e.id(tenant, successor));
                assertThat(supersededId(e, tenant, orphan)).as("R7: a tombstone whose successor is gone takes its own id").isEqualTo(e.id(tenant, orphan));
            });
    }

    private static Long supersededId(Env e, String tenant, String name) {
        return e.ctx().select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID);
    }

    /** canary_query_ring_upsert: the ring row carries the id; an entry whose collection has no registry row is skipped, not refused. */
    private static Scenario canary() {
        String tenant = "p31sql-cy";
        String own = docs("cy");
        String vec = "[" + "0.1,".repeat(767) + "0.1]";
        return new Scenario("canary_query_ring_upsert", tenant, true,
            e -> register(e, tenant, own),
            e -> {
                Integer written = Routines.canaryQueryRingUpsert(e.ctx().configuration(), tenant,
                    new String[] {own}, new String[] {M768}, new String[] {vec}, new byte[][] {Chash.ofText("digest one").toBytes()});
                assertThat(written).as("the registered entry is written").isEqualTo(1);
            },
            e -> {
                List<Long> ids = e.ctx().select(CANARY_QUERY_RING.COLLECTION_ID).from(CANARY_QUERY_RING)
                    .where(CANARY_QUERY_RING.TENANT_ID.eq(tenant)).fetch(CANARY_QUERY_RING.COLLECTION_ID);
                assertThat(ids).as("one ring row").hasSize(1);
                assertThat(ids.get(0)).isEqualTo(e.id(tenant, own));
                // linda's ruling: a name that does not resolve raises SQLSTATE 23503 (the code assign_from_chashes uses), it is not skipped
                String state = refusal(() -> Routines.canaryQueryRingUpsert(e.ctx().configuration(), tenant,
                    new String[] {"docs__p31sql-never-registered__" + M768 + "__v1"}, new String[] {M768}, new String[] {vec},
                    new byte[][] {Chash.ofText("digest two").toBytes()}));
                assertThat(state).as("an unregistered name raises").startsWith("23503").contains("is not registered for tenant");
                assertThat(e.ctx().fetchCount(CANARY_QUERY_RING, CANARY_QUERY_RING.TENANT_ID.eq(tenant))).as("and writes nothing").isEqualTo(1);
            });
    }
}
