// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.CompletionBootWork;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.DataMoveWorker;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.NameKeyDropMove;
import dev.nexus.service.datamove.P35bChangesets;
import dev.nexus.service.datamove.RenameReadiness;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.BackendReaper;
import dev.nexus.service.db.DmlUtilsInstaller;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenStore;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;
import java.util.function.Function;

/**
 * RDR-229 P3.7 / residual R9 (nexus-p2m1x.27, nexus-p2m1x.48): a booted engine on a database that survives a restart, for the
 * rename tests. It is {@code Main}'s boot in one JVM: the schema walk (as the migrating role, on the pg_background image),
 * {@link NexusService} over the same database, the real {@link DataMoveWorker} and the real {@link CompletionBootWork}. A
 * {@link #restart} stops all of them and boots again on the same database, so a CONTINUE changeset that the first walk skipped
 * (its precondition read an incomplete stamp move) is applied by the second walk, as on an install.
 *
 * <p><b>Gate 3 is simulated at its input.</b> {@code Main} builds the completion check with {@code Gate3Source.NOT_INSTALLED}
 * because P3.4's id-index builds do not exist yet, so on a real boot gate 3 never holds. Here the check reads a
 * {@link CompletionCheck.Gate3Source} that answers "P3.4 is complete" the way P3.4 will: every live registry row is planned and has a
 * VALID id index ({@link CompletionCheck.IdIndexCoverage}). Nothing else is faked: the stamp moves, the FK validation moves and the
 * name-key drop move run through the real worker, and the completion check reads the real registry, CHECKs and databasechangelog.
 * {@link #gate3(boolean)} closes and opens that input, so a test can stand before gate 3.
 *
 * <p><b>Two more inputs are stood in for, each until the code that owns it is on this branch.</b>
 * (1) The name-key drop move is registered by an engine pass after the engine serves (stage 2 of P3.7); until it exists a registrar
 * thread of this harness plays that pass: every 100 ms it hands the check's latest result and an admin connection to
 * {@link NameKeyDropMove#registerIfAbsent}, the one operation the pass will call, which registers nothing before gate 3 and one row
 * after it. The move itself is the real one and runs through the real worker. (2) P3.5b's CONTINUE changesets and its list
 * ({@value #P35B_LIST}) are not on this branch. While the resource is absent the engine's list is {@link #P35B_FIXTURE_ID}, a changeset
 * that no walk applies, and {@link #restart} records it in {@code databasechangelog} as P3.5b's second walk will record the real ones; when
 * P3.5b's list exists the harness reads it and records nothing.
 *
 * <p>The engine is built through {@code NexusService}'s package-private constructor that takes {@link Gate3} and
 * {@link RenameReadiness} (both read per call), fed from {@link CompletionBootWork#gate3()} and
 * {@link CompletionBootWork#renameReadiness()}. Until P3.7 the readiness is a stub that always says ready, so the rename stays the
 * name path.
 */
final class Rdr229RenameEngine implements AutoCloseable {

    static final ObjectMapper MAPPER = new ObjectMapper();
    static final String ROOT = "tok-root-rdr229-rename-0123456789abcdefghij";
    /** The name-key drop move's label (P3.7). */
    static final String DROP_LABEL = "rdr229-name-key-drop";
    /** The resource P3.5b's code commit writes: the CONTINUE changesets the completion check waits to see applied. */
    static final String P35B_LIST = P35bChangesets.RESOURCE;
    /** The changeset id the harness lists while P3.5b's list is absent (see the class comment). */
    static final String P35B_FIXTURE_ID = "p37-harness-p35b-continue-fixture";

    final PostgreSQLContainer<?> pg;
    final AdminConnection admin;
    final HttpClient http = TestHttp.client();
    private final AtomicBoolean gate3Open = new AtomicBoolean(true);
    private int boots;
    private int minted;

    private DmlUtilsInstaller installer;
    private HikariDataSource ds;
    private NexusService service;
    private BootWorkRegistry bootWork;
    private DataMoveWorker worker;
    private CompletionBootWork check;
    private TenantScope scope;
    private RenameReadiness readiness;
    private Thread registrar;
    private volatile boolean registrarStop;

    /** A database on the pg_background image with the roles and libraries a data-move engine needs; nothing is walked yet. */
    Rdr229RenameEngine() throws Exception {
        this.pg = PgContainerHelper.startDataMove();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.createDataMoveExtensions(su);
            DmlUtilsTestFixtures.createAdmin(su);
            DmlUtilsTestFixtures.bootstrapDmlUtils(su);
        }
        this.admin = new AdminConnection(pg.getJdbcUrl(), DmlUtilsTestFixtures.ADMIN, DmlUtilsTestFixtures.ADMIN_PASSWORD);
    }

    // ---- boot and restart -----------------------------------------------------------------------------------------------

    /** Opens or closes the simulated P3.4 input of gate 3, for the next boot and for the running check (read on its next sample). */
    Rdr229RenameEngine gate3(boolean open) {
        gate3Open.set(open);
        return this;
    }

    /** One boot: walk the schema, start the engine, the worker and the completion check. */
    void boot() throws Exception {
        boots++;
        installer = new DmlUtilsInstaller(true);
        try (HikariDataSource migration = pool(2, "rename-engine-migration")) {
            SchemaMigrator.migrate(migration, installer);
        }
        if (boots == 1) {
            su(ctx -> {
                PgContainerHelper.seedServiceToken(ctx, ROOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                    TokenStore.SCOPE_ROOT, null, null);
                return null;
            });
        }
        ds = pool(12, "rename-engine-service");
        scope = new TenantScope(ds);
        bootWork = new BootWorkRegistry(Clock.systemUTC());
        check = new CompletionBootWork(bootWork, admin, CompletionCheck.ReadSet.boot(), this::simulatedGate3,
            Rdr229RenameEngine::p35bIds);
        readiness = counting(check.renameReadiness());
        service = new NexusService(0, ROOT, ds, null, null, null, null, null, null, System::getenv,
            () -> dev.nexus.service.db.PgSession.startupPciSettings().enabled(), null, null, check.gate3(), readiness);
        service.bootWork(bootWork);
        service.start();
        startWorker();
        check.start();
        startRegistrar();
    }

    /** The renames that went through {@link RenameReadiness#pinned}, over every boot of this engine: a rename that skipped the pin is not counted. */
    private final java.util.concurrent.atomic.AtomicInteger pinnedRenames = new java.util.concurrent.atomic.AtomicInteger();

    private RenameReadiness counting(RenameReadiness inner) {
        return new RenameReadiness() {
            @Override
            public Missing missing() {
                return inner.missing();
            }

            @Override
            public <T> T pinned(java.util.function.Supplier<T> rename) {
                pinnedRenames.incrementAndGet();
                return inner.pinned(rename);
            }
        };
    }

    /** How many renames ran inside the rename window since this engine was built. */
    int pinnedRenames() {
        return pinnedRenames.get();
    }

    /** Runs {@code work} inside the rename window, as a rename does: gate 3 cannot flip to true until it returns. */
    <T> T pinned(java.util.function.Supplier<T> work) {
        return readiness.pinned(work);
    }

    /** The in-process gate-3 signal the engine's readers read, now. */
    boolean gate3Holds() {
        return check.gate3().holds();
    }

    /** Whether {@code boot_work} lists the restart entry (the P3.5b changesets wait for one restart) in this boot. */
    boolean restartEntryPresent() {
        return bootWork.snapshot().stream().anyMatch(e -> CompletionBootWork.RESTART_ENTRY_ID.equals(e.id()));
    }

    /** How many rows {@code nexus.data_moves} holds under {@code label}. */
    int moveRows(String label) {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "data_moves")),
            DSL.field(DSL.name("label"), String.class).eq(label)));
    }

    /** The P3.5b changeset ids the engine waits for: the real list when it exists, else the harness's one fixture id. */
    static java.util.Set<String> p35bIds() {
        java.util.Set<String> real = P35bChangesets.fromResource();
        return real.isEmpty() ? java.util.Set.of(P35B_FIXTURE_ID) : real;
    }

    /** True while P3.5b's list is absent and {@link #P35B_FIXTURE_ID} stands in for it. */
    static boolean usingFixtureList() {
        return P35bChangesets.fromResource().isEmpty();
    }

    /** What P3.5b's second walk does to {@code databasechangelog}, for the fixture id only: one EXECUTED row. */
    private void recordFixtureChangeset() {
        su(ctx -> {
            var log = DSL.table(DSL.name("public", "databasechangelog"));
            var id = DSL.field(DSL.name("id"), String.class);
            if (!ctx.fetchExists(log, id.eq(P35B_FIXTURE_ID))) {
                Integer next = ctx.select(DSL.coalesce(DSL.max(DSL.field(DSL.name("orderexecuted"), Integer.class)), 0).plus(1))
                    .from(log).fetchOne(0, Integer.class);
                ctx.insertInto(log, id, DSL.field(DSL.name("author"), String.class), DSL.field(DSL.name("filename"), String.class),
                        DSL.field(DSL.name("dateexecuted"), java.time.OffsetDateTime.class),
                        DSL.field(DSL.name("orderexecuted"), Integer.class), DSL.field(DSL.name("exectype"), String.class))
                    .values(P35B_FIXTURE_ID, "p37-harness", "p37-harness-fixture.xml", java.time.OffsetDateTime.now(), next, "EXECUTED")
                    .execute();
            }
            return null;
        });
    }

    /** The harness's stand-in for the registration pass: see the class comment. Stops with the engine. */
    private void startRegistrar() {
        registrarStop = false;
        CompletionBootWork watched = check;
        registrar = new Thread(() -> {
            while (!registrarStop) {
                try (DSLContext ctx = DSL.using(admin.url(), admin.user(), admin.password())) {
                    NameKeyDropMove.registerIfAbsent(watched.latest(), ctx);
                } catch (RuntimeException e) {
                    // a tick that cannot reach the database is retried on the next one
                }
                try {
                    Thread.sleep(100);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }, "rdr229-rename-engine-registrar");
        registrar.setDaemon(true);
        registrar.start();
    }

    /** Stops the engine, the worker and the check, and boots again on the same database (the engine's restart). */
    void restart() throws Exception {
        stopEngine();
        if (usingFixtureList()) {
            recordFixtureChangeset();
        }
        boot();
    }

    /** Starts the data-move worker (it is started by {@link #boot} already; this is for a test that stopped it). */
    void startWorker() {
        if (worker != null) {
            worker.close();
        }
        worker = DataMoveWorker.create(admin, installer,
            new DataMoveWorker.Settings(Duration.ofMillis(100), Duration.ofMillis(100), 0),
            BackendReaper.newApplicationName("rdr229-rename-engine-" + boots), true);
        worker.onMoveCompleted(check::onMoveCompleted);
        worker.start();
    }

    /** Stops the data-move worker, so a move that is open stays open: a test stands in the window before it completes. */
    void stopWorker() throws InterruptedException {
        if (worker == null) {
            return;
        }
        worker.close();
        worker.awaitStopped(Duration.ofSeconds(30));
        worker = null;
    }

    private void stopEngine() {
        registrarStop = true;
        if (registrar != null) {
            registrar.interrupt();
            registrar = null;
        }
        if (worker != null) {
            worker.close();
            worker = null;
        }
        if (check != null) {
            check.close();
            check = null;
        }
        if (service != null) {
            service.stop();
            service = null;
        }
        if (ds != null) {
            ds.close();
            ds = null;
        }
    }

    @Override
    public void close() {
        stopEngine();
        pg.stop();
    }

    private HikariDataSource pool(int size, String name) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(admin.user());
        cfg.setPassword(admin.password());
        cfg.setMaximumPoolSize(size);
        cfg.setPoolName(name + "-" + boots);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    /** P3.4's answer, simulated at the input the check reads: every live registry row is planned and has a VALID id index. */
    private Optional<CompletionCheck.IdIndexCoverage> simulatedGate3(DSLContext ctx) {
        if (!gate3Open.get()) {
            return Optional.empty();
        }
        Set<String> live = new TreeSet<>();
        for (Long id : ctx.select(DSL.field(DSL.name("collection_id"), Long.class))
                .from(DSL.table(DSL.name("nexus", "catalog_collections")))
                .where(DSL.field(DSL.name("superseded_by"), String.class).eq(""))
                .fetch(0, Long.class)) {
            if (id != null) {
                live.add(String.valueOf(id));
            }
        }
        return Optional.of(new CompletionCheck.IdIndexCoverage(live, live, Set.of()));
    }

    // ---- state the engine reports ---------------------------------------------------------------------------------------

    /** The completion check's latest result, read now. */
    CompletionCheck.Result completion() {
        return check.refresh();
    }

    /** What a rename waits for now (always present; kept as an Optional for the callers). */
    Optional<RenameReadiness.Missing> readiness() {
        check.refresh();
        return Optional.of(readiness.missing());
    }

    /** {@code GET /v1/status}'s {@code rdr229_completion} object (unauthenticated), or a missing node. */
    JsonNode statusCompletion() throws Exception {
        HttpResponse<String> r = http.send(TestHttp.request(url("/v1/status")).GET().build(), HttpResponse.BodyHandlers.ofString());
        return MAPPER.readTree(r.body()).path("rdr229_completion");
    }

    // ---- polling --------------------------------------------------------------------------------------------------------

    /** Polls {@code condition} every 100 ms until it holds or {@code timeout} passes; true when it held. No bare sleep: every wait has this deadline. */
    static boolean await(Duration timeout, BooleanSupplier condition) throws InterruptedException {
        long deadline = System.nanoTime() + timeout.toNanos();
        while (true) {
            if (condition.getAsBoolean()) {
                return true;
            }
            if (System.nanoTime() >= deadline) {
                return false;
            }
            Thread.sleep(100);
        }
    }

    // ---- database --------------------------------------------------------------------------------------------------------

    /** Runs {@code work} as the superuser (bypasses RLS), autocommit, on a connection of its own. */
    <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The engine's own tenant scope (RLS-bound), for repositories a test builds beside the service. */
    TenantScope scope() {
        return scope;
    }

    /** The changeset ids recorded as applied in {@code databasechangelog}. */
    Set<String> appliedChangesets() {
        return su(ctx -> new TreeSet<>(ctx.select(DSL.field(DSL.name("id"), String.class))
            .from(DSL.table(DSL.name("public", "databasechangelog"))).fetch(0, String.class)));
    }

    /** Whether {@code label} has a row in {@code nexus.data_moves} with {@code completed_at} set. */
    boolean moveCompleted(String label) {
        return su(ctx -> ctx.fetchExists(DSL.selectOne().from(DSL.table(DSL.name("nexus", "data_moves")))
            .where(DSL.field(DSL.name("label"), String.class).eq(label))
            .and(DSL.field(DSL.name("completed_at"), java.time.OffsetDateTime.class).isNotNull())));
    }

    // ---- tenants and the rename route -----------------------------------------------------------------------------------

    /** A tenant with a token and its partition leaves (its first request is made here, so a test starts clean). */
    String newTenant(String tenant) throws Exception {
        String token = "tok-" + tenant + "-" + (minted++) + "-0123456789abcdefghijklmnopqrstuv";
        su(ctx -> {
            PgContainerHelper.seedServiceToken(ctx, token, tenant, "label-" + tenant);
            PgContainerHelper.ensureTenantPartitions(ctx, tenant);
            return null;
        });
        HttpResponse<String> who = call("GET", "/v1/_whoami", token, null);
        if (who.statusCode() != 200) {
            throw new IllegalStateException("whoami for " + tenant + " answered " + who.statusCode() + ": " + who.body());
        }
        return token;
    }

    String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    HttpResponse<String> call(String method, String path, String token, String jsonBody) throws Exception {
        var body = jsonBody == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(jsonBody);
        var b = TestHttp.request(url(path)).method(method, body);
        if (jsonBody != null) {
            b.header("Content-Type", "application/json");
        }
        if (token != null) {
            b.header("Authorization", "Bearer " + token);
        }
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    /** What the rename route answered. {@code reason} and {@code missing} are the refusal contract ({@code rename_refused}, {@code name_key_drop_move}|{@code restart}). */
    record Renamed(int status, String body, String reason, String missing) {
        boolean refused() {
            return status == 409 && "rename_refused".equals(reason);
        }

        @Override
        public String toString() {
            return status + " " + body;
        }
    }

    /** {@code POST /v1/catalog/collections/rename}. {@code extra} adds request fields ({@code content_type}, {@code embedding_model}). */
    Renamed rename(String token, String oldName, String newName, Map<String, String> extra) throws Exception {
        var req = MAPPER.createObjectNode().put("old_name", oldName).put("new_name", newName);
        extra.forEach(req::put);
        HttpResponse<String> r = call("POST", "/v1/catalog/collections/rename", token, MAPPER.writeValueAsString(req));
        String reason = null;
        String missing = null;
        try {
            JsonNode n = MAPPER.readTree(r.body());
            reason = n.hasNonNull("reason") ? n.get("reason").asText() : null;
            missing = n.hasNonNull("missing") ? n.get("missing").asText() : null;
        } catch (com.fasterxml.jackson.core.JsonProcessingException notJson) {
            // a body that is not JSON carries no contract fields
        }
        return new Renamed(r.statusCode(), r.body(), reason, missing);
    }

    Renamed rename(String token, String oldName, String newName) throws Exception {
        return rename(token, oldName, newName, Map.of());
    }
}
