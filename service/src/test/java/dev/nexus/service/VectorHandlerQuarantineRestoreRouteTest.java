// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 9 Day-2 (bead nexus-2x9xa): {@code POST /v1/vectors/gc/quarantine-restore}, the route behind
 * {@code nx t3 quarantine restore}. Over real HTTP through the RLS-subject service role, fixtures by substrate SQL.
 * The data-path behaviour (grace, collisions, the gate, the audit row) is {@link QuarantineRestoreIntegrationTest}'s;
 * this pins what the route adds: the three sources and their exclusivity, the response shape, the refusals as 4xx,
 * and tenant isolation through a bearer token.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerQuarantineRestoreRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN_A = "tok-qrestore-tenant-a-0123456789abcdef00";
    private static final String TOKEN_B = "tok-qrestore-tenant-b-0123456789abcdef00";
    private static final String SVC_ROLE = "svc_qrestore_route";
    private static final String SVC_PASS = "svc_qrestore_route_pass";
    private static final String TENANT_A = "qrestore-route-a";
    private static final String TENANT_B = "qrestore-route-b";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private NexusService service;
    private HttpClient http;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN_A, TENANT_A, "qr-a");
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN_B, TENANT_B, "qr-b");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        FakeEmbedder embedder = new FakeEmbedder(384);
        var repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);
        service = new NexusService(0, TOKEN_A, svcDs, null, repo);
        service.start();
        http = TestHttp.client();
        for (String token : List.of(TOKEN_A, TOKEN_B)) {
            http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
                .header("Authorization", "Bearer " + token).GET().build(), HttpResponse.BodyHandlers.ofString());
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── http ────────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String token, Map<String, Object> body) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/gc/quarantine-restore")
            .header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> r) throws Exception {
        return MAPPER.readValue(r.body(), Map.class);
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> rows(Map<String, Object> body) {
        return (List<Map<String, Object>>) body.get("rows");
    }

    private static Map<String, Object> req(String origin, Object... kv) {
        var m = new LinkedHashMap<String, Object>();
        m.put("origin_collection", origin);
        m.put("quarantine_collection", "quarantine-" + origin);
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    // ── fixture (substrate SQL) ──────────────────────────────────────────────

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private String col() {
        return "knowledge__qrr" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    /** An origin and its sibling, registered, with one chunk per seed in the sibling stamped as quarantined. */
    private List<String> quarantined(String tenant, String origin, String quarantinedAt, String... seeds)
            throws Exception {
        String sibling = "quarantine-" + origin;
        List<String> hexes = new java.util.ArrayList<>();
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            for (String seed : seeds) {
                String hex = Chash.ofText(origin + "/" + seed).toHex();
                hexes.add(hex);
                PgContainerHelper.insertChunks(ctx, tenant, sibling, List.of(hex), List.of(seed + " text"),
                    List.of(new float[384]), List.of(Map.<String, Object>of(
                        "title", seed, "quarantined_at", quarantinedAt, "origin_collection", origin)));
            }
        });
        return hexes;
    }

    private boolean in(String tenant, String collection, String hex) throws Exception {
        boolean[] r = new boolean[1];
        su(ctx -> r[0] = ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
            .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))));
        return r[0];
    }

    private long auditRow(String tenant, String operation, String collection, List<String> chashes, String detailsJson)
            throws Exception {
        long[] id = new long[1];
        su(ctx -> id[0] = ctx.insertInto(GC_AUDIT)
            .set(GC_AUDIT.TENANT_ID, tenant).set(GC_AUDIT.OPERATION, operation).set(GC_AUDIT.COLLECTION, collection)
            .set(GC_AUDIT.ACTOR, "engine").set(GC_AUDIT.DRY_RUN, false).set(GC_AUDIT.CHASH_COUNT, chashes.size())
            .set(GC_AUDIT.CHASHES, JSONB.jsonb(toJson(chashes)))
            .set(GC_AUDIT.DETAILS, detailsJson == null ? (JSONB) null : JSONB.jsonb(detailsJson))
            .returningResult(GC_AUDIT.ID).fetchOne().value1());
        return id[0];
    }

    private static String toJson(Object o) {
        try { return MAPPER.writeValueAsString(o); } catch (Exception e) { throw new IllegalStateException(e); }
    }

    // ── the three sources ────────────────────────────────────────────────────

    @Test
    void namedChashesComeBack_withThePerChashRowsTheCountsAndTheAuditId() throws Exception {
        String o = col();
        List<String> hs = quarantined(TENANT_A, o, "2026-09-01T00:00:00Z", "a", "b");
        String nowhere = Chash.ofText("route-nowhere").toHex();

        var r = post(TOKEN_A, req(o, "chashes", List.of(hs.get(0), nowhere, hs.get(1)), "actor", "route-test"));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        var body = json(r);
        assertThat(body.get("restored")).isEqualTo(2);
        assertThat(body.get("missing")).isEqualTo(1);
        assertThat(body.get("present")).isEqualTo(0);
        assertThat(body.get("dry_run")).isEqualTo(false);
        assertThat(body.get("audit_id")).isNotNull();
        assertThat(rows(body)).extracting(x -> x.get("outcome")).containsExactly("restored", "missing", "restored");
        assertThat(rows(body).get(0).get("no_manifest")).isEqualTo(true);
        assertThat(rows(body).get(0).get("reapable_after")).isNotNull();
        assertThat(rows(body).get(1).get("reapable_after")).isNull();
        assertThat(in(TENANT_A, o, hs.get(0))).isTrue();
        assertThat(in(TENANT_A, "quarantine-" + o, hs.get(0))).isFalse();
    }

    @Test
    void aDryRunMovesNothing() throws Exception {
        String o = col();
        List<String> hs = quarantined(TENANT_A, o, "2026-09-01T00:00:00Z", "dry");

        var body = json(post(TOKEN_A, req(o, "chashes", hs, "dry_run", true)));

        assertThat(body.get("dry_run")).isEqualTo(true);
        assertThat(body.get("would_restore")).isEqualTo(1);
        assertThat(body.get("restored")).isEqualTo(0);
        assertThat(body.get("audit_id")).isNull();
        assertThat(in(TENANT_A, "quarantine-" + o, hs.get(0))).isTrue();
    }

    @Test
    void aQuarantinedAtWindowSelectsFromTheSibling_andPagesByNextAfter() throws Exception {
        String o = col();
        List<String> hs = quarantined(TENANT_A, o, "2026-09-05T00:00:00Z", "w1", "w2", "w3");

        var first = json(post(TOKEN_A, req(o, "quarantined_since", "2026-09-01T00:00:00Z",
            "quarantined_before", "2026-09-10T00:00:00Z", "limit", 2)));
        assertThat(first.get("restored")).isEqualTo(2);
        assertThat(first.get("next_after")).isNotNull();

        var second = json(post(TOKEN_A, req(o, "quarantined_since", "2026-09-01T00:00:00Z",
            "after_chash", first.get("next_after"), "limit", 2)));
        assertThat(second.get("restored")).isEqualTo(1);
        assertThat(second.get("next_after")).isNull();
        for (String h : hs) assertThat(in(TENANT_A, o, h)).isTrue();
    }

    @Test
    void anAuditIdNamesTheChashes_andASampleRowIsRefusedWithTheReason() throws Exception {
        String o = col();
        List<String> hs = quarantined(TENANT_A, o, "2026-09-01T00:00:00Z", "au1", "au2");
        long full = auditRow(TENANT_A, "reaper_quarantine", o, hs, "{\"quarantine_collection\":\"quarantine-" + o + "\"}");
        long sample = auditRow(TENANT_A, "gc_quarantine_orphans", o, hs.subList(0, 1),
            "{\"chashes_is_sample\":true,\"quarantine_collection\":\"quarantine-" + o + "\"}");

        var refused = post(TOKEN_A, req(o, "audit_id", sample));
        assertThat(refused.statusCode()).isEqualTo(400);
        assertThat(refused.body()).contains("sample").contains("quarantined_since");
        assertThat(in(TENANT_A, "quarantine-" + o, hs.get(0))).as("nothing moved").isTrue();

        var ok = post(TOKEN_A, req(o, "audit_id", full));
        assertThat(ok.statusCode()).as(ok.body()).isEqualTo(200);
        var body = json(ok);
        assertThat(body.get("restored")).isEqualTo(2);
        @SuppressWarnings("unchecked") var source = (Map<String, Object>) body.get("source");
        assertThat(source.get("audit_id")).isEqualTo((int) full);
        assertThat(source.get("operation")).isEqualTo("reaper_quarantine");
        assertThat(source.get("next_offset")).isNull();
    }

    // ── refusals ─────────────────────────────────────────────────────────────

    @Test
    void theRequestIsValidatedBeforeAnythingMoves() throws Exception {
        String o = col();
        List<String> hs = quarantined(TENANT_A, o, "2026-09-01T00:00:00Z", "v");

        assertThat(post(TOKEN_A, req(o)).statusCode()).as("no source").isEqualTo(400);
        assertThat(post(TOKEN_A, req(o, "chashes", hs, "audit_id", 1)).statusCode()).as("two sources").isEqualTo(400);
        assertThat(post(TOKEN_A, req(o, "chashes", List.of("nope"))).statusCode()).as("malformed chash").isEqualTo(400);
        assertThat(post(TOKEN_A, req(o, "chashes", List.of())).statusCode()).as("empty list").isEqualTo(400);
        assertThat(post(TOKEN_A, req(o, "quarantined_since", "yesterday")).statusCode()).as("bad instant").isEqualTo(400);
        var asQuarantine = req(o, "chashes", hs);
        asQuarantine.put("origin_collection", "quarantine-" + o);
        assertThat(post(TOKEN_A, asQuarantine).statusCode()).as("origin may not be a quarantine collection").isEqualTo(400);
        // A "quarantine_collection" in the body is no longer read (nexus-wbfpw.55: the engine finds the sibling), so
        // there is no malformed or unregistered sibling to refuse; theEngineFindsTheSiblingItself pins that.
        assertThat(post(TOKEN_A, req(o, "chashes", java.util.Collections.nCopies(1001, hs.get(0)))).statusCode())
            .as("over the per-call cap").isEqualTo(400);
        assertThat(in(TENANT_A, "quarantine-" + o, hs.get(0))).as("every refusal moved nothing").isTrue();
    }

    @Test
    void theEngineFindsTheSiblingItself_whateverTheCallerNamesOrOmits() throws Exception {
        // nexus-wbfpw.55 (RDR-192 Phase 3 gate I-1): catalog-044 rewrote owner_id, so the sibling the client derives
        // from the catalog row is NOT where the reaper (which names it from the collection's name) put the chunks.
        String o = col();
        List<String> hs = quarantined(TENANT_A, o, "2026-09-01T00:00:00Z", "r1", "r2");
        su(ctx -> assertThat(ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.OWNER_ID, "curator-9")
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A).and(CATALOG_COLLECTIONS.NAME.eq(o))).execute()).isEqualTo(1));
        String[] seg = o.split("__");
        String rowDerived = "quarantine-" + seg[0] + "__curator-9__" + seg[2] + "__" + seg[3];

        var named = req(o, "chashes", List.of(hs.get(0)));
        named.put("quarantine_collection", rowDerived);          // what the old client sends: unregistered
        var viaRow = post(TOKEN_A, named);
        assertThat(viaRow.statusCode()).as(viaRow.body()).isEqualTo(200);
        assertThat(json(viaRow).get("restored")).isEqualTo(1);
        assertThat(json(viaRow).get("quarantine_collection")).as("the response names where it really was")
            .isEqualTo("quarantine-" + o);
        assertThat(json(viaRow).get("quarantine_collections")).isEqualTo(List.of("quarantine-" + o));
        assertThat(json(viaRow).get("audit_ids")).isEqualTo(List.of(json(viaRow).get("audit_id")));

        var omitted = req(o, "chashes", List.of(hs.get(1)));
        omitted.remove("quarantine_collection");                 // the new client sends none
        var viaName = post(TOKEN_A, omitted);
        assertThat(viaName.statusCode()).as(viaName.body()).isEqualTo(200);
        assertThat(json(viaName).get("restored")).isEqualTo(1);
        assertThat(in(TENANT_A, o, hs.get(0))).isTrue();
        assertThat(in(TENANT_A, o, hs.get(1))).isTrue();
    }

    @Test
    void anOriginNoQuarantineHoldsAnythingOfReadsMissing_withNoSiblingNamed() throws Exception {
        String o = col();
        su(ctx -> PgContainerHelper.insertCollection(ctx, TENANT_A, o));
        String nowhere = Chash.ofText("route-no-sibling").toHex();
        var body = req(o, "chashes", List.of(nowhere));
        body.remove("quarantine_collection");

        var r = post(TOKEN_A, body);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).get("missing")).isEqualTo(1);
        assertThat(json(r).get("quarantine_collection")).isNull();
        assertThat(json(r).get("quarantine_collections")).isEqualTo(List.of());
        assertThat(json(r).get("audit_ids")).isEqualTo(List.of());
    }

    @Test
    void aGetIsRefused() throws Exception {
        var r = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/gc/quarantine-restore")
            .header("Authorization", "Bearer " + TOKEN_A).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).isEqualTo(405);
    }

    // ── tenant scope ─────────────────────────────────────────────────────────

    @Test
    void anotherTenantsTokenCannotRestoreOrReadTheChunk_orTheAuditRow() throws Exception {
        String o = col();
        List<String> hs = quarantined(TENANT_A, o, "2026-09-01T00:00:00Z", "tenant");
        long id = auditRow(TENANT_A, "reaper_quarantine", o, hs, null);
        // Tenant B has the same collections, empty.
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT_B, o);
            PgContainerHelper.insertCollection(ctx, TENANT_B, "quarantine-" + o);
        });

        var byChash = json(post(TOKEN_B, req(o, "chashes", hs)));
        assertThat(byChash.get("missing")).isEqualTo(1);
        assertThat(byChash.get("restored")).isEqualTo(0);
        assertThat(post(TOKEN_B, req(o, "audit_id", id)).statusCode()).as("A's audit row is invisible to B").isEqualTo(400);
        assertThat(json(post(TOKEN_B, req(o, "quarantined_since", "2000-01-01T00:00:00Z"))).get("restored")).isEqualTo(0);
        assertThat(in(TENANT_A, "quarantine-" + o, hs.get(0))).as("A's quarantine is untouched").isTrue();
    }

    // ── reattach (nexus-wbfpw.49) ────────────────────────────────────────────

    /** One quarantined chunk of {@code origin} whose metadata names {@code doc}, and that live document. */
    private String quarantinedForLiveDoc(String tenant, String origin, String doc, String seed) throws Exception {
        String sibling = "quarantine-" + origin;
        String hex = Chash.ofText(origin + "/" + seed).toHex();
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            PgContainerHelper.insertChunks(ctx, tenant, sibling, List.of(hex), List.of(seed + " text"),
                List.of(new float[384]), List.of(Map.<String, Object>of(
                    "title", seed, "quarantined_at", "2026-09-01T00:00:00Z", "origin_collection", origin,
                    "catalog_doc_id", doc, "chunk_index", 0)));
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT)
               .values(tenant, doc, "Title of " + doc, origin, 1).execute();
        });
        return hex;
    }

    private int manifestRows(String tenant, String doc) throws Exception {
        int[] n = new int[1];
        su(ctx -> n[0] = ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))));
        return n[0];
    }

    @Test
    void reattachIsTheDefault_andTheResponseSaysWhatWasAttachedToWhat() throws Exception {
        String o = col();
        String h = quarantinedForLiveDoc(TENANT_A, o, "9.1.1", "attached");

        var r = post(TOKEN_A, req(o, "chashes", List.of(h)));

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        var body = json(r);
        assertThat(body.get("reattach")).isEqualTo(true);
        assertThat(body.get("attached")).isEqualTo(1);
        assertThat(body.get("superseded")).isEqualTo(0);
        var row = rows(body).get(0);
        assertThat(row.get("outcome")).isEqualTo("restored");
        assertThat(row.get("reattach")).isEqualTo("attach");
        assertThat(row.get("attached")).isEqualTo(true);
        assertThat(row.get("owner")).isEqualTo("9.1.1");
        assertThat(row.get("owner_title")).isEqualTo("Title of 9.1.1");
        assertThat(row.get("position")).isEqualTo(0);
        assertThat(row.get("no_manifest")).isEqualTo(false);
        assertThat(row.get("reapable_after")).isNull();
        assertThat(manifestRows(TENANT_A, "9.1.1")).isEqualTo(1);
        // The per-owner count the client reads for "M of N attached", and the superseded reason (null here).
        assertThat(row).containsKeys("reason", "owner_rows", "owner_chunks");
        assertThat(row.get("reason")).isNull();
        assertThat(row.get("owner_rows")).isEqualTo(1);
        assertThat(row.get("owner_chunks")).isEqualTo(1);
    }

    @Test
    void aSupersededChunkSaysWhy_onTheWire() throws Exception {
        String o = col();
        String h = quarantinedForLiveDoc(TENANT_A, o, "9.3.1", "stale");
        su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.INDEX_STATE, "complete")
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A).and(CATALOG_DOCUMENTS.TUMBLER.eq("9.3.1"))).execute());

        var body = json(post(TOKEN_A, req(o, "chashes", List.of(h))));

        var row = rows(body).get(0);
        assertThat(row.get("reattach")).isEqualTo("superseded");
        assertThat(row.get("reason")).as("a complete document's manifest is authoritative").isEqualTo("complete");
        assertThat(row.get("attached")).isEqualTo(false);
        assertThat(body.get("superseded")).isEqualTo(1);
        assertThat(manifestRows(TENANT_A, "9.3.1")).isZero();
    }

    @Test
    void reattachFalseMovesBytesOnly_andStillReportsWhatItWouldHaveDone() throws Exception {
        String o = col();
        String h = quarantinedForLiveDoc(TENANT_A, o, "9.2.1", "bytes-only");

        var body = json(post(TOKEN_A, req(o, "chashes", List.of(h), "reattach", false)));

        assertThat(body.get("reattach")).isEqualTo(false);
        assertThat(body.get("attached")).isEqualTo(0);
        var row = rows(body).get(0);
        assertThat(row.get("outcome")).isEqualTo("restored");
        assertThat(row.get("attached")).isEqualTo(false);
        assertThat(row.get("reattach")).as("what it would have done").isEqualTo("attach");
        assertThat(row.get("no_manifest")).isEqualTo(true);
        assertThat(manifestRows(TENANT_A, "9.2.1")).isZero();
        // A repeat with reattach (the default) finishes it.
        var again = json(post(TOKEN_A, req(o, "chashes", List.of(h))));
        assertThat(again.get("present")).isEqualTo(1);
        assertThat(again.get("attached")).isEqualTo(1);
        assertThat(manifestRows(TENANT_A, "9.2.1")).isEqualTo(1);
    }

    @Test
    void aBusyTripOnALaterSiblingIs503WithNothingMovedFalseAndTheAuditIdsAlreadyWritten() throws Exception {
        // nexus-wbfpw.55 round 2: each sibling restores in its own transaction. Sibling one commits; sibling two
        // trips the index-run lock of its chunk's document. "nothing_moved: true" would be false of the call.
        String o = col();
        String[] seg = o.split("__");
        String clientSibling = "quarantine-" + seg[0] + "__old-owner__" + seg[2] + "__" + seg[3];
        String h1 = Chash.ofText(o + "/first").toHex();
        String h2 = Chash.ofText(o + "/second").toHex();
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT_A, o);
            PgContainerHelper.insertCollection(ctx, TENANT_A, "quarantine-" + o);
            PgContainerHelper.insertCollection(ctx, TENANT_A, clientSibling);
            PgContainerHelper.insertChunks(ctx, TENANT_A, "quarantine-" + o, List.of(h1), List.of("first text"),
                List.of(new float[384]), List.of(Map.<String, Object>of("title", "first",
                    "quarantined_at", "2026-09-01T00:00:00Z", "origin_collection", o)));
            PgContainerHelper.insertChunks(ctx, TENANT_A, clientSibling, List.of(h2), List.of("second text"),
                List.of(new float[384]), List.of(Map.<String, Object>of("title", "second",
                    "quarantined_at", "2026-09-01T00:00:00Z", "origin_collection", o,
                    "catalog_doc_id", "9.7.1", "chunk_index", 0)));
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT,
                    CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.METADATA)
               .values(TENANT_A, "9.7.1", "Locked", o, 1, "l.md", JSONB.jsonb("{}")).execute();
        });

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock",
                org.jooq.impl.SQLDataType.OTHER, DSL.function("hashtext", org.jooq.impl.SQLDataType.INTEGER,
                    DSL.val("indexrun:" + TENANT_A + ":9.7.1")))).execute();
            var body = req(o, "chashes", List.of(h1, h2));
            body.remove("quarantine_collection");
            var r = post(TOKEN_A, body);

            assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
            var j = json(r);
            assertThat(j.get("reason")).isEqualTo("quarantine_restore_busy");
            assertThat(j.get("nothing_moved")).as("sibling one had committed").isEqualTo(false);
            assertThat((List<?>) j.get("audit_ids")).as("and wrote an audit row").hasSize(1);
            assertThat(j.get("moved_chashes")).isEqualTo(List.of(h1));
            assertThat(j.get("error").toString()).doesNotContain("nothing was moved");
            assertThat(r.headers().firstValue("Retry-After")).hasValue("5");
            holder.rollback();
        }
        assertThat(in(TENANT_A, o, h1)).as("the first sibling's chunk is home").isTrue();
        assertThat(in(TENANT_A, clientSibling, h2)).as("the tripped sibling's did not move").isTrue();
        var again = json(post(TOKEN_A, req(o, "chashes", List.of(h1, h2))));
        assertThat(again.get("present")).isEqualTo(1);
        assertThat(again.get("restored")).isEqualTo(1);
    }

    @Test
    void aHeldSweepGateIsATypedRetryable503_withNothingMoved() throws Exception {
        String o = col();
        List<String> hs = quarantined(TENANT_A, o, "2026-09-01T00:00:00Z", "busy");

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock_shared",
                org.jooq.impl.SQLDataType.OTHER, DSL.function("hashtext", org.jooq.impl.SQLDataType.INTEGER,
                    DSL.val("sweepgate:" + TENANT_A + "/" + o)))).execute();
            var r = post(TOKEN_A, req(o, "chashes", hs));

            assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
            var body = json(r);
            assertThat(body.get("reason")).isEqualTo("quarantine_restore_busy");
            assertThat(body.get("nothing_moved")).isEqualTo(true);
            assertThat(body.get("audit_ids")).as("nothing was committed, so nothing was audited").isEqualTo(List.of());
            assertThat(body.get("moved_chashes")).isEqualTo(List.of());
            assertThat(body.get("retry_after_seconds")).isEqualTo(5);
            assertThat(r.headers().firstValue("Retry-After")).hasValue("5");
            assertThat(body.get("error").toString()).as("not the opaque 500 text").doesNotContain("internal server error");
            holder.rollback();
        }
        assertThat(in(TENANT_A, "quarantine-" + o, hs.get(0))).as("nothing moved").isTrue();
        // The same request, sent again once the gate is free, goes through.
        assertThat(json(post(TOKEN_A, req(o, "chashes", hs))).get("restored")).isEqualTo(1);
    }
}
