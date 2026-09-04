// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-164 P3 — HTTP coverage for {@code POST /v1/catalog/collections/rename}
 * ({@link dev.nexus.service.http.CatalogHandler#handleCollectionRename}). The repo-level
 * coherent re-home is exhaustively covered by {@link CatalogRenameCollectionTest}; this
 * exercises the HTTP glue the repo test cannot: the {@code old_name/new_name} canonical
 * keys, the {@code old/new} compat alias, the 400 missing-key guard, the 405 method guard,
 * and the {@code {"renamed": {...}}} response shape.
 *
 * <p>Also covers its PAIRED verb {@code POST /collections/supersede} — the two are one
 * operation from the CLI's point of view ({@code nx catalog rename-collection} calls
 * rename and then supersede), the guards live in the same handler, and they share this
 * fixture exactly. See {@link dev.nexus.service.http.CatalogHandler
 * #handleCollectionSupersede}: nexus-g8z8n's four guards and nexus-cecqy's
 * tombstone/idempotence contract.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerRenameTest {

    private static final String TOKEN = "catalog-rename-handler-token-def456";
    private static final String SVC_ROLE = "svc_cat_ren_handler";
    private static final String SVC_PASS = "svc_cat_ren_handler_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // bootstrapServiceRole's Liquibase run leaves su's autoCommit disabled
            // (Liquibase manages its own changeset-boundary commits); re-enable it
            // so this INSERT via seedServiceToken actually commits before su closes.
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
            // Seed two registry rows to rename (one per route-shape test).
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', 'hren__old')");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', 'hren__old-alias')");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void post_canonicalKeys_returns200WithRenamedCounts() throws Exception {
        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__old\",\"new_name\":\"hren__new\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKey("renamed");
        @SuppressWarnings("unchecked")
        Map<String, Object> renamed = (Map<String, Object>) body.get("renamed");
        assertThat(((Number) renamed.get("catalog_collections_inserted")).intValue())
            .as("registry Y inserted via HTTP").isEqualTo(1);
        assertThat(((Number) renamed.get("catalog_collections_superseded")).intValue())
            .as("registry X retired as a tombstone via HTTP (nexus-cecqy)").isEqualTo(1);
    }

    @Test
    void post_oldNewAlias_returns200() throws Exception {
        // The handler accepts old/new as a compat alias for old_name/new_name.
        var resp = post("/v1/catalog/collections/rename",
            "{\"old\":\"hren__old-alias\",\"new\":\"hren__new-alias\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        Map<String, Object> renamed = (Map<String, Object>) body.get("renamed");
        assertThat(((Number) renamed.get("catalog_collections_inserted")).intValue())
            .as("alias old/new resolved").isEqualTo(1);
    }

    @Test
    void post_missingKeys_returns400() throws Exception {
        var resp = post("/v1/catalog/collections/rename", "{}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void post_missingNewName_returns400() throws Exception {
        var resp = post("/v1/catalog/collections/rename", "{\"old_name\":\"hren__whatever\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void post_renameMissingCollection_returns404() throws Exception {
        // nexus-hz785: renaming an unregistered collection must fail loud with 404, not
        // silently return 200 with all-zero counts.
        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__never-registered-xyz\",\"new_name\":\"hren__missing-target\"}");
        assertThat(resp.statusCode()).isEqualTo(404);
        assertThat(resp.body()).contains("collection not found");
    }

    @Test
    void post_renameOntoExistingCollection_returns409() throws Exception {
        // nexus-gaou3: a plain rename onto an already-registered collection is a collision —
        // it must 409, not silently take the RDR-162 cross-model COPY branch (repoint-only).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', 'hren__c409-src') ON CONFLICT DO NOTHING");
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', 'hren__c409-tgt') ON CONFLICT DO NOTHING");
        }
        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__c409-src\",\"new_name\":\"hren__c409-tgt\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("target collection already exists");
    }

    @Test
    void post_crossModelTrue_ontoExistingTarget_returns200Repoint() throws Exception {
        // nexus-gaou3: cross_model:true opts into the RDR-162 repoint branch — target already
        // exists (ETL populated it), only catalog_documents.physical_collection moves.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', 'hren__xm-src') ON CONFLICT DO NOTHING");
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', 'hren__xm-tgt') ON CONFLICT DO NOTHING");
            su.createStatement().execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT + "', 'xm-doc-1', 'XM', 'hren__xm-src') ON CONFLICT DO NOTHING");
        }
        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__xm-src\",\"new_name\":\"hren__xm-tgt\",\"cross_model\":true}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        Map<String, Object> renamed = (Map<String, Object>) body.get("renamed");
        // cross-model COPY branch returns ONLY catalog_documents (repoint, not full re-home).
        assertThat(((Number) renamed.get("catalog_documents")).intValue())
            .as("the doc under the source repoints to the target").isEqualTo(1);
        assertThat(renamed).as("no full re-home in the cross-model branch")
            .doesNotContainKey("catalog_collections_inserted");
    }

    // ── POST /collections/supersede — the guards (nexus-g8z8n, nexus-cecqy) ─────
    // The route had NONE of these: every refusal below replied 200 {"updated":0},
    // and HttpCatalogClient discarded the count, so all four were silent.

    private void seedCollections(String... names) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String n : names) {
                su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                    + "VALUES ('" + TENANT + "', '" + n + "') ON CONFLICT DO NOTHING");
            }
        }
    }

    /** Seed a document row INTO a collection, so the collection is not empty.
     *  nexus-v6za0 round 2: seedCollections creates a registry row ONLY. Every rename pin
     *  written before this helper existed therefore exercised an EMPTY target, which is
     *  precisely the case the merge hazard does not apply to — so they could not have
     *  caught it, and the original P0 pin passed on its identity check alone. */
    private void seedDocumentIn(String collection, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT + "', '" + tumbler + "', 'seeded', '" + collection + "') "
                + "ON CONFLICT DO NOTHING");
        }
    }

    private Map<String, Object> collectionRow(String name) throws Exception {
        var resp = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort()
                + "/v1/catalog/collections/get?name=" + name))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var r = http.send(resp, HttpResponse.BodyHandlers.ofString());
        return r.statusCode() == 200 ? mapper.readValue(r.body(), MAP_T) : null;
    }

    @Test
    void post_supersede_stampsSupersededAtWithoutCallerSuppliedValue() throws Exception {
        // nexus-g8z8n guard 4: superseded_at was bound ONLY from the request body and no
        // caller ever sent one, so every supersession landed undatable.
        seedCollections("hsup__t-old", "hsup__t-new");
        assertThat(collectionRow("hsup__t-old").get("superseded_at"))
            .as("guard: starts un-stamped").isIn(null, "");

        var resp = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__t-old\",\"superseded_by\":\"hsup__t-new\"}");
        assertThat(resp.statusCode()).isEqualTo(200);

        var row = collectionRow("hsup__t-old");
        assertThat(row.get("superseded_by")).isEqualTo("hsup__t-new");
        assertThat((String) row.get("superseded_at"))
            .as("the supersession must be datable without the caller supplying an instant")
            .isNotBlank();
    }

    @Test
    void post_supersede_toItself_returns400() throws Exception {
        // A self-referential supersession retires a live collection with nothing to
        // redirect to: collectionForTuple skips any row with superseded_by set, so the
        // name becomes permanently unresolvable while still existing.
        seedCollections("hsup__g0-self");
        var resp = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__g0-self\",\"superseded_by\":\"hsup__g0-self\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("cannot supersede itself");
        assertThat((String) collectionRow("hsup__g0-self").get("superseded_by"))
            .as("a refused self-supersede must write nothing").isEmpty();
    }

    @Test
    void post_supersede_unknownOldName_returns404() throws Exception {
        // nexus-g8z8n guard 1: a typo on an explicit action must fail loud.
        seedCollections("hsup__g1-new");
        var resp = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__never-registered\",\"superseded_by\":\"hsup__g1-new\"}");
        assertThat(resp.statusCode()).isEqualTo(404);
        assertThat(resp.body()).contains("collection not found");
    }

    @Test
    void post_supersede_unregisteredNewName_returns404AndWritesNothing() throws Exception {
        // nexus-g8z8n guard 3: an unvalidated superseded_by leaves a pointer that
        // resolves to nothing and no join can follow.
        seedCollections("hsup__g3-old");
        var resp = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__g3-old\",\"superseded_by\":\"hsup__g3-nosuch\"}");
        assertThat(resp.statusCode()).isEqualTo(404);
        assertThat(resp.body()).contains("unregistered collection");
        assertThat((String) collectionRow("hsup__g3-old").get("superseded_by"))
            .as("a refused supersede must write nothing").isEmpty();
    }

    @Test
    void post_supersede_targetIsARenameTombstone_returns409NamingItsSupersededBy() throws Exception {
        // nexus-laa8j, shape (a): a RENAME tombstone — EMPTY, its children already
        // re-homed by renameCollectionTxn step 3. Guard 3 used to be
        // repo.collectionExists(tenant, supersededBy), which accepts ANY row including
        // this one, building the two-hop unaudited chain guard 2 exists to refuse from
        // the source side. It must 409, naming the target's OWN superseded_by.
        seedCollections("hsup__g3r-old", "hsup__g3r-tgt-src");
        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hsup__g3r-tgt-src\",\"new_name\":\"hsup__g3r-tgt\"}")
            .statusCode()).isEqualTo(200);
        // NON-VACUITY: the target really is a tombstone, and it is EMPTY (a rename leaves
        // it so) — this is shape (a), not shape (b) below.
        assertThat(collectionRow("hsup__g3r-tgt-src").get("superseded_by")).isEqualTo("hsup__g3r-tgt");

        var resp = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__g3r-old\",\"superseded_by\":\"hsup__g3r-tgt-src\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("is itself superseded by").contains("hsup__g3r-tgt");
        assertThat((String) collectionRow("hsup__g3r-old").get("superseded_by"))
            .as("a refused supersede must write nothing").isEmpty();
    }

    @Test
    void post_supersede_targetIsASupersedeTombstone_returns409NamingItsSupersededBy() throws Exception {
        // nexus-laa8j, shape (b): a SUPERSEDE tombstone — FULLY POPULATED, since
        // supersedeCollection is a pure UPDATE that never touches chunks. superseded_by
        // cannot distinguish this from shape (a) above (nexus-v6za0's lesson), and guard 3
        // does not need to: both shapes refuse identically here.
        seedCollections("hsup__g3s-old", "hsup__g3s-tgt", "hsup__g3s-successor");
        assertThat(post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__g3s-tgt\",\"superseded_by\":\"hsup__g3s-successor\"}")
            .statusCode()).isEqualTo(200);
        // NON-VACUITY: the target really is superseded.
        assertThat(collectionRow("hsup__g3s-tgt").get("superseded_by")).isEqualTo("hsup__g3s-successor");

        var resp = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__g3s-old\",\"superseded_by\":\"hsup__g3s-tgt\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("is itself superseded by").contains("hsup__g3s-successor");
        assertThat((String) collectionRow("hsup__g3s-old").get("superseded_by"))
            .as("a refused supersede must write nothing").isEmpty();
    }

    @Test
    void post_supersede_concurrentHardDelete_returns404NotAFalseConflict() throws Exception {
        // nexus-0svvu (b): the zero-rows branch used to assert a cause it never
        // observed — "superseded to a different target concurrently" — but a concurrent
        // HARD DELETE (POST /collections/delete) also yields zero rows from the same
        // UPDATE, and the correct answer there is 404 (the row is gone), not a 409
        // naming a supersession that never happened.
        //
        // Force the race deterministically instead of hoping two HTTP calls interleave:
        // hold the target row's lock in a manually-managed transaction (FOR UPDATE) BEFORE
        // starting the supersede request. The request's own guards are plain SELECTs and
        // are not blocked by the lock (MVCC readers do not wait on writers), so they see
        // the row live and proceed to the UPDATE, which DOES need the lock and blocks.
        // Deleting-and-committing on the lock-holding connection then lets Postgres
        // re-evaluate the blocked UPDATE's WHERE against the now-gone row: it matches
        // zero rows, exactly the concurrent-delete shape this test exists to pin.
        seedCollections("hsup__del409-old", "hsup__del409-new");

        var pool = java.util.concurrent.Executors.newFixedThreadPool(1);
        try (Connection lockConn = pg.createConnection("")) {
            lockConn.setAutoCommit(false);
            try (var st = lockConn.createStatement()) {
                st.execute("SELECT 1 FROM nexus.catalog_collections WHERE name='hsup__del409-old' FOR UPDATE");
            }
            var supersedeFuture = pool.submit(() ->
                post("/v1/catalog/collections/supersede",
                    "{\"name\":\"hsup__del409-old\",\"superseded_by\":\"hsup__del409-new\"}"));
            // Let the request's guard reads (unblocked SELECTs) complete before its
            // UPDATE reaches the same row and blocks on the lock this connection holds.
            Thread.sleep(300);
            try (var del = lockConn.createStatement()) {
                del.execute("DELETE FROM nexus.catalog_collections WHERE name='hsup__del409-old'");
            }
            lockConn.commit();

            var resp = supersedeFuture.get(10, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(resp.statusCode())
                .as("a concurrent hard delete must read as 404 (row gone), not a 409 "
                    + "naming a supersession that never happened")
                .isEqualTo(404);
            assertThat(resp.body()).contains("collection not found");
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void post_supersede_alreadySupersededToDifferentTarget_returns409() throws Exception {
        // nexus-g8z8n guard 2: a second supersession rewrote the chain unaudited.
        seedCollections("hsup__g2-old", "hsup__g2-new1", "hsup__g2-new2");
        assertThat(post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__g2-old\",\"superseded_by\":\"hsup__g2-new1\"}").statusCode()).isEqualTo(200);

        var resp = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__g2-old\",\"superseded_by\":\"hsup__g2-new2\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("already superseded by");
        assertThat(collectionRow("hsup__g2-old").get("superseded_by"))
            .as("the ORIGINAL pointer survives a refused re-supersede").isEqualTo("hsup__g2-new1");
    }

    @Test
    void post_supersede_sameTargetIsIdempotentAndDoesNotRestamp() throws Exception {
        // nexus-cecqy: the carve-out guard 2 must leave open. The canonical rename now
        // tombstones X -> Y itself and its caller then issues supersede(X, Y); that has
        // to succeed. Idempotent means the recorded instant holds still, too — otherwise
        // every retry relabels when the supersession happened.
        seedCollections("hsup__idem-old", "hsup__idem-new");
        var first = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__idem-old\",\"superseded_by\":\"hsup__idem-new\"}");
        assertThat(first.statusCode()).isEqualTo(200);
        String stamp = (String) collectionRow("hsup__idem-old").get("superseded_at");
        assertThat(stamp).as("guard: the first supersede stamped an instant").isNotBlank();

        var again = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__idem-old\",\"superseded_by\":\"hsup__idem-new\"}");
        assertThat(again.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(again.body(), MAP_T).get("updated"))
            .as("a re-assertion still reports a marked row — the CLI gates its "
                + "CollectionSuperseded message on this count")
            .isEqualTo(1);
        assertThat((String) collectionRow("hsup__idem-old").get("superseded_at"))
            .as("a repeat must not move the recorded supersession instant").isEqualTo(stamp);
    }

    @Test
    void post_supersede_afterRename_marksTheTombstoneTheRenameLeft() throws Exception {
        // nexus-cecqy end to end over HTTP: rename retires X, the paired supersede call
        // re-asserts it, and X survives as a datable record of where it went. Before the
        // fix X was DELETEd and this second call updated ZERO rows while the CLI
        // announced "Emitted CollectionSuperseded".
        seedCollections("hsup__ren-old");
        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hsup__ren-old\",\"new_name\":\"hsup__ren-new\"}").statusCode()).isEqualTo(200);

        var row = collectionRow("hsup__ren-old");
        assertThat(row).as("the rename must leave the old name as a tombstone").isNotNull();
        assertThat(row.get("superseded_by")).isEqualTo("hsup__ren-new");

        var resp = post("/v1/catalog/collections/supersede",
            "{\"name\":\"hsup__ren-old\",\"superseded_by\":\"hsup__ren-new\"}");
        assertThat(resp.statusCode()).as("the paired supersede must not 409 on the "
            + "tombstone the rename itself wrote").isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("updated")).isEqualTo(1);
    }

    @Test
    void post_renameBackOntoATombstonedName_revivesItInsteadOf409() throws Exception {
        // nexus-cecqy REGRESSION. Undoing a rename is the operation the tombstone change
        // was built around, and it is the one the HTTP path stopped allowing.
        //
        // renameCollectionTxn selects its branch on a LIVE target (superseded_by = ''), so
        // a tombstone at the target takes the canonical branch and step 1's upsert revives
        // it. But this handler's collision guard called collectionExists(), a bare
        // row-existence check that sees tombstones, so the request 409'd before reaching
        // the repo. Before the tombstone change the old row was DELETEd and this round
        // trip worked; the two predicates disagreeing is what broke it.
        //
        // The repo-level round-trip test (CatalogRenameCollectionTest @Order(40)) calls
        // repo.renameCollection directly and so never crossed this guard.
        seedCollections("hren__rt-a");
        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__rt-a\",\"new_name\":\"hren__rt-b\"}").statusCode()).isEqualTo(200);
        // NON-VACUITY: the forward rename really did leave a tombstone at the old name —
        // otherwise the guard below has nothing to trip on.
        assertThat(collectionRow("hren__rt-a").get("superseded_by")).isEqualTo("hren__rt-b");

        var back = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__rt-b\",\"new_name\":\"hren__rt-a\"}");
        assertThat(back.statusCode())
            .as("renaming back onto a RETIRED name is a revive, not a collision — "
                + "an operator undoing a rename has no other verb to reach for")
            .isEqualTo(200);

        // The revived row must be LIVE again, or it stays invisible to collectionForTuple
        // and the round trip only looks complete.
        var revived = collectionRow("hren__rt-a");
        assertThat(revived.get("superseded_by")).isEqualTo("");
        // ...and the name it was renamed to is now the tombstone.
        assertThat(collectionRow("hren__rt-b").get("superseded_by")).isEqualTo("hren__rt-a");
    }

    @Test
    void post_renameOntoALIVEcollection_still409s() throws Exception {
        // The other half: making the guard tombstone-aware must NOT weaken the nexus-gaou3
        // collision check it was added for. A LIVE target is still a collision.
        seedCollections("hren__live-src", "hren__live-tgt");
        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__live-src\",\"new_name\":\"hren__live-tgt\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("target collection already exists");
    }

    @Test
    void post_crossModelOntoATOMBSTONE_409s() throws Exception {
        // cross_model:true means the RDR-162 COPY branch: the target already exists AND IS
        // LIVE (the ETL just populated it), and only catalog_documents is repointed.
        // A tombstoned target is not that. The repo would treat it as non-live and take the
        // canonical FULL-REHOME branch instead — a more destructive operation than the flag
        // promises — so the mismatch must fail loud rather than silently do something else.
        seedCollections("hren__xm-t-src", "hren__xm-t-tgt");
        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__xm-t-tgt\",\"new_name\":\"hren__xm-t-retired\"}")
            .statusCode()).isEqualTo(200);
        assertThat(collectionRow("hren__xm-t-tgt").get("superseded_by")).isEqualTo("hren__xm-t-retired");

        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__xm-t-src\",\"new_name\":\"hren__xm-t-tgt\",\"cross_model\":true}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("retired");
    }

    @Test
    void post_renameOntoASupersedeMarkedCollection_409sAndPreservesTheChain() throws Exception {
        // nexus-v6za0 — THE REGRESSION 1232585d introduced. A tombstone has two provenances
        // and the widened guard reasoned about only one:
        //   (a) renameCollectionTxn step 3 leaves an EMPTY tombstone (children re-homed first);
        //   (b) POST /collections/supersede leaves a FULLY POPULATED one — supersedeCollection
        //       is a pure UPDATE that never touches chunks.
        // liveCollectionExists() is false for BOTH, so before this fix a rename onto (b) passed
        // the collision guard, took the canonical FULL-REHOME branch, overwrote the target's
        // metadata with the source's, ERASED its superseded_by, and re-homed the source's
        // chunks on top of the target's rows: two collections merged across two vector spaces.
        seedCollections("hren__v6-src", "hren__v6-tgt", "hren__v6-successor");
        // The hazard is a tombstone that STILL HOLDS DATA. Seed it, or this pin proves
        // nothing about merging (nexus-v6za0 round 2).
        seedDocumentIn("hren__v6-tgt", "9.601");

        // Mark the target superseded — the RDR-101 P6 / cross-model migration marking. Its
        // data is untouched by design.
        assertThat(post("/v1/catalog/collections/supersede",
            "{\"name\":\"hren__v6-tgt\",\"superseded_by\":\"hren__v6-successor\"}")
            .statusCode()).isEqualTo(200);
        assertThat(collectionRow("hren__v6-tgt").get("superseded_by")).isEqualTo("hren__v6-successor");

        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__v6-src\",\"new_name\":\"hren__v6-tgt\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("merge");

        // The supersession chain must survive the refusal — erasing it was the unaudited
        // chain rewrite handleCollectionSupersede guard 2 exists to refuse.
        assertThat(collectionRow("hren__v6-tgt").get("superseded_by")).isEqualTo("hren__v6-successor");
        // ...and the source must be untouched: no half-done rename.
        assertThat(collectionRow("hren__v6-src").get("superseded_by")).isEqualTo("");
    }

    @Test
    void post_renameBackOntoOwnTombstone_stillRevives() throws Exception {
        // The counterpart to the pin above: the identity gate must not break the round trip
        // nexus-u4e20 exists to restore. A tombstone whose superseded_by NAMES THE COLLECTION
        // BEING RENAMED is the undo, and it still revives.
        seedCollections("hren__v6-rt");
        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__v6-rt\",\"new_name\":\"hren__v6-rt2\"}")
            .statusCode()).isEqualTo(200);
        assertThat(collectionRow("hren__v6-rt").get("superseded_by")).isEqualTo("hren__v6-rt2");

        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__v6-rt2\",\"new_name\":\"hren__v6-rt\"}")
            .statusCode()).isEqualTo(200);
        assertThat(collectionRow("hren__v6-rt").get("superseded_by")).isEqualTo("");
    }

    @Test
    void post_renameToItself_400s() throws Exception {
        // nexus-mxzxs. handleCollectionSupersede's guard 0 comment claims this sibling refuses
        // old==new "for the same reason" — true only INCIDENTALLY (collectionExists(newName)
        // was true when newName==oldName), and 1232585d's widening removed that cover for
        // tombstones. X->X onto a tombstoned X would revive it in step 1, no-op in step 2, and
        // have step 3 stamp superseded_by = X ON X: the self-superseded row guard 0 calls fatal.
        seedCollections("hren__self");
        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__self\",\"new_name\":\"hren__self\"}")
            .statusCode()).isEqualTo(400);

        // The tombstoned variant — the one the widened guard actually let through.
        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__self\",\"new_name\":\"hren__self2\"}")
            .statusCode()).isEqualTo(200);
        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__self\",\"new_name\":\"hren__self\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(collectionRow("hren__self").get("superseded_by")).isEqualTo("hren__self2");
    }

    @Test
    void post_renameFromARetiredSource_409s() throws Exception {
        // nexus-c29vr. Step 1's INSERT used to copy superseded_by/at from the source row, so
        // renaming a RETIRED X onto a free name produced a BORN-DEAD target: permanently
        // filtered out of collectionForTuple, with all of X's data re-homed onto it. The
        // select-list now clears those columns explicitly AND the source must be live.
        seedCollections("hren__c29-src", "hren__c29-successor");
        assertThat(post("/v1/catalog/collections/supersede",
            "{\"name\":\"hren__c29-src\",\"superseded_by\":\"hren__c29-successor\"}")
            .statusCode()).isEqualTo(200);

        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__c29-src\",\"new_name\":\"hren__c29-fresh\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("retired");
        // The would-be born-dead row must not exist at all.
        assertThat(collectionRow("hren__c29-fresh")).isNull();
    }

    @Test
    void post_crossModelOntoAnABSENTtarget_409s() throws Exception {
        // nexus-tnx48. 1232585d's converse guard fired only when the target was retired AND
        // present, so cross_model:true onto a target that does not exist at all skipped every
        // guard and still ran the canonical FULL-REHOME — moving chunks, taxonomy and aspects
        // under a flag whose contract is documents-only. The guard now keys on !live, which
        // covers absent and retired alike.
        seedCollections("hren__tnx-src");
        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__tnx-src\",\"new_name\":\"hren__tnx-absent\",\"cross_model\":true}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("does not exist");
        assertThat(collectionRow("hren__tnx-absent")).isNull();
    }

    @Test
    void post_supersedeThenRenameSuccessorOntoIt_409sInsteadOfMerging() throws Exception {
        // nexus-v6za0, SECOND ROUND. The 351874c5 identity gate discriminated
        // DIRECTION, not PROVENANCE, and this is the arrangement that proved it:
        // both reviewers found it independently and a probe confirmed 200-not-409.
        // supersede(old -> its OWN successor) is the DOMINANT real shape (it is what
        // `nx catalog doctor --collections-drift` steers operators into, nexus-e1k14);
        // the original pin picked a THIRD-PARTY successor, the one arrangement where
        // identity and provenance happen to agree.
        // supersede(X -> Y) leaves X POPULATED with superseded_by = Y.
        // Then rename Y -> X gives oldName == tgtSuperseded == Y, so the identity
        // gate matches and permits the revive -> canonical FULL-REHOME -> merge.
        seedCollections("hren__prov-x", "hren__prov-y");
        seedDocumentIn("hren__prov-x", "9.602");
        assertThat(post("/v1/catalog/collections/supersede",
            "{\"name\":\"hren__prov-x\",\"superseded_by\":\"hren__prov-y\"}")
            .statusCode()).isEqualTo(200);
        assertThat(collectionRow("hren__prov-x").get("superseded_by")).isEqualTo("hren__prov-y");

        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__prov-y\",\"new_name\":\"hren__prov-x\"}");
        assertThat(resp.statusCode())
            .as("a POPULATED supersede tombstone must not be revived just because it points at the source")
            .isEqualTo(409);
        // NAME THE CONDITION. identityOk is TRUE here (superseded_by == oldName), so the only
        // thing that can refuse is emptiness — assert the emptiness branch's text, or this pin
        // would keep passing if the refusal ever came from somewhere else. Same lesson the
        // Python half of this change records: a red test tells you SOMETHING caught it.
        //
        // nexus-34wrg option (c): the message now NAMES which table blocked (here,
        // catalog_documents via seedDocumentIn) instead of the bare "still holds data" —
        // an operator can tell real data from an audit breadcrumb without guessing.
        assertThat(resp.body()).contains("still holds real data in 'catalog_documents'");
        // ...and the target's data must be untouched by the refusal.
        assertThat(collectionRow("hren__prov-x").get("superseded_by")).isEqualTo("hren__prov-y");
    }

    @Test
    void post_renameOntoAnEMPTYTombstoneSupersededToAThirdParty_409sOnIdentity() throws Exception {
        // The one arrangement where IDENTITY is the SOLE refusing condition. Without this,
        // deleting identityOk from the handler leaves every test green (found in review):
        // the merge pins all have emptiness false too, and the revive pin has identity true.
        // Here the tombstone is EMPTY (a rename leaves it so) but points at a THIRD party,
        // so reviving it would erase that supersession unaudited — the chain rewrite
        // handleCollectionSupersede guard 2 refuses.
        seedCollections("hren__id-x", "hren__id-z");
        assertThat(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__id-x\",\"new_name\":\"hren__id-y\"}")
            .statusCode()).isEqualTo(200);
        // NON-VACUITY: the tombstone must be empty, or this pins emptiness rather than identity.
        assertThat(collectionRow("hren__id-x").get("superseded_by")).isEqualTo("hren__id-y");

        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__id-z\",\"new_name\":\"hren__id-x\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body())
            .as("must refuse on the IDENTITY half, not the emptiness half")
            .contains("Unwind the rename chain");
    }

    @Test
    void get_returns405() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/rename"))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(405);
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
