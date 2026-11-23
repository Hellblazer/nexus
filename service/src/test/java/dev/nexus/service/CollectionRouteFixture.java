// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.RenameReadiness;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * RDR-229 P3.5a write half (nexus-p2m1x.23): one engine over one PG container, with the two seams the P3.5a behaviour
 * reads injected by constructor, and the helpers the route tests share.
 *
 * <p><b>The two seams</b> (RDR item 6, "Readers during the stamp"; bead comments on nexus-p2m1x.23): {@link #gate3} is the
 * {@link Gate3} signal, read on each call, so a test flips it between two requests with no restart (the RDR's "reads by the
 * id once the check sees gate 3 hold, with no restart"). {@link #renameWindow} is the second condition of the rename
 * refusal (RDR line 2709: "while the move has no {@code completed_at} or a P3.5b changeset has not applied"), as the
 * {@link RenameReadiness} the engine would build from the name-key drop move and the changelog; tests set it directly.
 *
 * <p><b>Missing production symbol</b>: the package-private {@code NexusService} constructor that takes both, appended to the
 * 13-argument one ({@code ..., reembedSettings, Gate3 gate3, RenameReadiness renameReadiness}). Main builds one {@link Gate3}
 * over the completion check and passes it to the service, which hands it to {@code CollectionRegistry} and the repositories
 * (linda's ruling, bead comment 2026-11-22); {@link RenameReadiness} is the rename refusal's second input, which P3.7 fills in
 * from its move.
 */
final class CollectionRouteFixture implements AutoCloseable {

    static final String TENANT = "p35a-routes";
    static final String TOKEN = "tok-p35a-routes-0123456789abcdef0000";

    /**
     * This engine's tenant and token. The standard engine is {@link #TENANT}; the local-embed variant has its own, because the
     * route boundary's id index is the process-wide registry cache, keyed by tenant and id, and two engines here each have
     * their own database whose identity column starts at 1. One tenant on both would let an id of one database resolve to a
     * row of the other. (Production has one database per JVM: EngineSingletonLock.)
     */
    final String tenant;
    final String token;
    static final ObjectMapper MAPPER = new ObjectMapper();

    final AtomicBoolean gate3 = new AtomicBoolean(false);
    final AtomicReference<RenameReadiness.Missing> renameWindow = new AtomicReference<>(RenameReadiness.Missing.NONE);

    final PostgreSQLContainer<?> pg;
    final HikariDataSource svcDs;
    final NexusService service;
    final HttpClient http;

    /** A response: the status, the body text, and the body as JSON ({@code MissingNode} when it is not JSON). */
    record Resp(int status, String body) {
        JsonNode json() {
            try {
                return MAPPER.readTree(body);
            } catch (Exception e) {
                return com.fasterxml.jackson.databind.node.MissingNode.getInstance();
            }
        }

        /** The {@code reason} of a typed error body, or "" when it carries none. */
        String reason() {
            return json().path("reason").asText("");
        }
    }

    /** An embedder that serves the fixture's minilm collections (the router dispatches on the model token). */
    private static final class MiniEmbedder implements Embedder {
        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new java.util.ArrayList<>(texts.size());
            for (int i = 0; i < texts.size(); i++) {
                float[] v = new float[384];
                v[0] = 1.0f;
                out.add(v);
            }
            return out;
        }

        @Override
        public String modelToken() {
            return "minilm-l6-v2-384";
        }
    }

    /** As {@code ReembedRoutesTest}'s: records what is executed on it, runs none of it, and schedules nothing. */
    private static final class RecordingExecutor extends ScheduledThreadPoolExecutor {
        RecordingExecutor() {
            super(1, r -> {
                Thread t = new Thread(r, "recording-p35a-routes-executor");
                t.setDaemon(true);
                return t;
            });
        }

        @Override
        public ScheduledFuture<?> scheduleWithFixedDelay(Runnable command, long initialDelay, long delay, TimeUnit unit) {
            return null;
        }

        @Override
        public void execute(Runnable command) {
            // recorded by being dropped: nothing here runs a re-embed job
        }
    }

    /** The standard engine: no embedder, not local. The embed and re-embed routes answer 503 on it. */
    CollectionRouteFixture() throws Exception {
        this(false);
    }

    /**
     * @param localEmbed true for the {@code LOCAL_EMBED} variant ({@link CollectionRouteCensus.Engine}): the engine built
     *                   like {@code ReembedRoutesTest}'s, an {@code EmbedderRouter} over a minilm embedder,
     *                   {@code NX_ENGINE_LOCAL=1} through the env supplier, and a recording re-embed executor. Used only by
     *                   the census rows that name it; every other row runs on the standard engine.
     */
    CollectionRouteFixture(boolean localEmbed) throws Exception {
        this.tenant = localEmbed ? TENANT + "-local" : TENANT;
        this.token = localEmbed ? "tok-p35a-routes-local-0123456789abcdef" : TOKEN;
        dev.nexus.service.db.CollectionRegistry.shared().evictTenant(tenant);   // an earlier class's rows, same tenant, another database
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), token, tenant, "p35a-routes");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        var repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);
        Gate3 gate = gate3::get;
        RenameReadiness readiness = renameWindow::get;
        EmbedderRouter router = localEmbed ? new EmbedderRouter(new MiniEmbedder(), "document") : null;
        java.util.function.Function<String, String> env = localEmbed
            ? name -> "NX_ENGINE_LOCAL".equals(name) ? "1" : null
            : System::getenv;
        service = new NexusService(0, token, svcDs, router, repo, null, null, null, null, env, () -> false,
            localEmbed ? new RecordingExecutor() : null, null, gate, readiness);
        service.start();
        http = TestHttp.client();
        // The tenant's first request runs the once-per-process ghost sweep, which deletes a collection that is registered
        // and has no chunk at that moment (CatalogHandlerRenameTest). Burn it before any test registers one.
        send("GET", "/v1/catalog/collections/list", null);
    }

    @Override
    public void close() {
        dev.nexus.service.db.CollectionRegistry.shared().evictTenant(tenant);
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ---- HTTP ---------------------------------------------------------------------------------------------------------

    Resp send(String method, String pathAndQuery, String jsonBody) {
        try {
            var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + pathAndQuery)
                .header("Authorization", "Bearer " + token)
                .header("Content-Type", "application/json");
            HttpRequest req = "GET".equals(method) ? b.GET().build()
                : b.method(method, HttpRequest.BodyPublishers.ofString(jsonBody == null ? "" : jsonBody)).build();
            var r = http.send(req, HttpResponse.BodyHandlers.ofString());
            return new Resp(r.statusCode(), r.body());
        } catch (Exception e) {
            throw new IllegalStateException(method + " " + pathAndQuery, e);
        }
    }

    Resp post(String path, String jsonBody) {
        return send("POST", path, jsonBody);
    }

    Resp get(String pathAndQuery) {
        return send("GET", pathAndQuery, null);
    }

    // ---- collections --------------------------------------------------------------------------------------------------

    /** A conformant docs label (the model token minilm-l6-v2-384 is seeded, so a vector route can embed with a 384-wide fake). */
    static String label(String stem) {
        return "docs__" + stem + "__minilm-l6-v2-384__v1";
    }

    /** Registers each label (a live registry row, no chunks) as the tenant; idempotent. */
    void register(String... labels) {
        CollectionIdSeeds.register(pg, tenant, labels);
    }

    /** The registry id of {@code label}, or null when no row has it, read as the superuser straight from the table. */
    Long idOf(String label) {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    boolean exists(String label) {
        return idOf(label) != null;
    }

    /** The label that row {@code id} has now, or null when no row has the id. */
    String labelOf(long id) {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id))
                .fetchOne(CATALOG_COLLECTIONS.NAME);
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** {@code superseded_by} of {@code label}'s row ("" when live), or null when the row does not exist. */
    String supersededBy(String label) {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
                .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY);
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Runs {@code work} on a fresh superuser connection (typed jOOQ only: the raw-SQL gate counts string statements). */
    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    static String json(String s) {
        try {
            return MAPPER.writeValueAsString(s);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
