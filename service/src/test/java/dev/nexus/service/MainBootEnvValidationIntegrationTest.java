// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (nexus-43ulx.12 fix round, code review I3): a real boot of {@code Main} in a child JVM with a
 * malformed {@code NX_SEARCH_PCI*} value must exit 1 with {@code event=pg_session_env_invalid} naming the variable.
 *
 * <p>PgSession parses its env in static initializers, so the first PgSession static call decides where a malformed
 * value surfaces. Before the fix the first call was the embedding-profile seed (and, in local mode,
 * {@code LocalOnnxAdmission.fromEnv}), which sat OUTSIDE the boot catch, so the value escaped {@code main} as an
 * uncaught {@code ExceptionInInitializerError} and the event docs/configuration.md promises never appeared. The child
 * needs only a database that accepts the pool: the validation block now runs before the schema migration.
 *
 * <p>Refusal is told apart from a later accident by three things together: the event line itself carries the
 * variable's own message (an unwrapped {@code ExceptionInInitializerError} logs {@code error="null"} and leaves the
 * name only in the stack trace), the process never logs {@code event=schema_migration_start} (a catch without its
 * {@code System.exit(1)} falls through to the migration and exits 1 for some unrelated reason), and a control boot
 * with a valid environment does reach that line, so its absence in the refusals is not vacuous. Every boot is
 * killed the moment it logs {@code schema_migration_start}: past that line nothing here is under test.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MainBootEnvValidationIntegrationTest {

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void startPg() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stopPg() {
        if (pg != null) {
            pg.stop();
        }
    }

    private record Boot(int exitCode, String output, boolean reachedMigration) {
        boolean logged(String event) {
            return output.contains(event);
        }
    }

    private static final String MIGRATION_START = "event=schema_migration_start";

    /**
     * Boot Main in a child JVM, with one extra env variable when {@code variable} is non-null; returns its exit code
     * and combined output. The child is killed as soon as it logs {@code stopOn}: for the validation runs that is
     * {@link #MIGRATION_START} (a refusal never gets there, and the control run has proved what it set out to).
     */
    private Boot boot(String variable, String value) throws IOException, InterruptedException {
        return boot(variable, value, MIGRATION_START);
    }

    private Boot boot(String variable, String value, String stopOn) throws IOException, InterruptedException {
        return bootWith(variable == null ? java.util.Map.of() : java.util.Map.of(variable, value), stopOn);
    }

    private Boot bootWith(java.util.Map<String, String> extraEnv, String stopOn)
            throws IOException, InterruptedException {
        String java = Path.of(System.getProperty("java.home"), "bin", "java").toString();
        ProcessBuilder pb = new ProcessBuilder(java, "-cp", System.getProperty("java.class.path"),
            "dev.nexus.service.Main");
        var env = pb.environment();
        env.put("NX_DB_URL", pg.getJdbcUrl());
        env.put("NX_DB_USER", pg.getUsername());
        env.put("NX_DB_PASS", pg.getPassword());
        env.put("NX_SERVICE_PORT", "0");
        env.remove("NX_VOYAGE_API_KEY");
        env.putAll(extraEnv);
        pb.redirectErrorStream(true);
        Process p = pb.start();
        // Drain on a thread of its own so a full pipe cannot stall the child while we wait on it.
        var out = new StringBuffer();
        var reached = new java.util.concurrent.atomic.AtomicBoolean();
        var ready = new java.util.concurrent.atomic.AtomicBoolean();
        Thread reader = new Thread(() -> {
            try (var in = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = in.readLine()) != null) {
                    out.append(line).append('\n');
                    if (line.contains(stopOn) && reached.compareAndSet(false, true)) {
                        p.destroyForcibly();
                    } else if (line.contains("event=service_ready") && ready.compareAndSet(false, true)) {
                        // A booted service never exits on its own. The line a test waits for follows within
                        // milliseconds; this only keeps a regression that never logs it from costing the 120 s wait.
                        Thread killer = new Thread(() -> {
                            try {
                                Thread.sleep(10_000);
                            } catch (InterruptedException ignored) {
                                return;
                            }
                            p.destroyForcibly();
                        }, "main-boot-killer");
                        killer.setDaemon(true);
                        killer.start();
                    }
                }
            } catch (IOException e) {
                out.append("read failed: ").append(e).append('\n');
            }
        }, "main-boot-output");
        reader.setDaemon(true);
        reader.start();
        if (!p.waitFor(120, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            reader.join(5_000);
            throw new AssertionError("Main did not exit within 120 s; output so far:\n" + out);
        }
        reader.join(10_000);
        String all = out.toString();
        return new Boot(p.exitValue(), all, all.contains(MIGRATION_START));
    }

    private static List<String> lines(Boot boot, String contains) {
        return Arrays.stream(boot.output().split("\n")).filter(l -> l.contains(contains)).toList();
    }

    /** Exit 1 at validation: one event line carrying {@code message}, and the schema migration never began. */
    private static void assertRefusedAtValidation(Boot boot, String message) {
        assertThat(boot.reachedMigration()).as("a refusal never reaches the schema migration:\n" + boot.output())
            .isFalse();
        assertThat(boot.output()).as("no migration line").doesNotContain(MIGRATION_START);
        assertThat(boot.exitCode()).as(boot.output()).isEqualTo(1);
        List<String> events = lines(boot, "event=pg_session_env_invalid");
        assertThat(events).as("exactly one refusal event:\n" + boot.output()).hasSize(1);
        assertThat(events.get(0)).as("the event line itself carries the variable's own message, unwrapped")
            .containsPattern(Pattern.compile("event=pg_session_env_invalid error=\"[^\"]*" + Pattern.quote(message)));
        assertThat(boot.output()).as("not an escaped static-init failure")
            .doesNotContain("Exception in thread \"main\"");
    }

    /** The control: with nothing malformed the same boot passes validation and starts the migration. */
    @Test
    void aValidEnvironment_passesValidation_andReachesTheSchemaMigration() throws Exception {
        Boot boot = boot(null, null);

        assertThat(boot.reachedMigration()).as(boot.output()).isTrue();
        assertThat(boot.output()).contains(MIGRATION_START)
            .contains("event=pci_settings enabled=true")
            .doesNotContain("event=pg_session_env_invalid");
        assertThat(boot.output().indexOf("event=pci_settings")).as("validation logs before the migration starts")
            .isLessThan(boot.output().indexOf(MIGRATION_START));
    }

    /**
     * NX_SEARCH_PCI=0 switches the DDL half off; the read half still starts and reads, on a real boot. A fresh
     * database has its tenant leaves already, so the first read lands at once and logs event=pci_sweep.
     */
    @Test
    void withTheDdlHalfOff_theReadHalfStillStartsAndReads() throws Exception {
        Boot boot = boot("NX_SEARCH_PCI", "0", "event=pci_sweep valid=");

        assertThat(boot.logged("event=pci_settings enabled=false")).as(boot.output()).isTrue();
        assertThat(boot.logged("event=pci_sweep_read_started")).as(boot.output()).isTrue();
        assertThat(boot.logged("event=pci_sweep valid=")).as(boot.output()).isTrue();
        assertThat(boot.output()).doesNotContain("event=pg_session_env_invalid");
    }

    @Test
    void aMalformedNxSearchPci_exitsOne_andTheLogNamesTheVariable() throws Exception {
        assertRefusedAtValidation(boot("NX_SEARCH_PCI", "yes"), "NX_SEARCH_PCI must be 1");
    }

    @Test
    void aMalformedSweepSeconds_pastTheNewMaximum_exitsOne() throws Exception {
        assertRefusedAtValidation(boot("NX_SEARCH_PCI_SWEEP_SECONDS", "3601"), "NX_SEARCH_PCI_SWEEP_SECONDS");
    }

    // ── the test-only tenant reset route's bind check (nexus-0bl77, behavioural in nexus-vr8wk) ──────────

    private static final String BIND_REFUSED = "event=tenant_reset_route_bind_refused";

    /**
     * A real boot with {@code NX_TEST_TENANT_RESET_ROUTE=1} on a non-loopback bind exits 1 with the refusal event,
     * and never reaches the schema migration. A source-text check cannot tell this from a Main whose
     * {@code System.exit(1)} was removed: the event would still be logged, the boot would carry on to open the pool
     * and migrate, and only the missing exit makes this test red. This test cannot tell where the refusal sits
     * relative to the pool (the database here accepts the pool); the test below pins that.
     */
    @Test
    void theTenantResetRouteOnANonLoopbackBind_exitsOneBeforeTheMigration() throws Exception {
        for (String bind : new String[] {"0.0.0.0", "10.1.2.3"}) {
            Boot boot = bootWith(java.util.Map.of("NX_TEST_TENANT_RESET_ROUTE", "1", "NX_SERVICE_BIND", bind),
                MIGRATION_START);

            assertThat(boot.reachedMigration()).as("bind " + bind + " never reaches the migration:\n" + boot.output())
                .isFalse();
            assertThat(boot.exitCode()).as(boot.output()).isEqualTo(1);
            assertThat(lines(boot, BIND_REFUSED)).as("exactly one refusal event:\n" + boot.output()).hasSize(1);
            assertThat(lines(boot, BIND_REFUSED).get(0)).contains("NX_TEST_TENANT_RESET_ROUTE").contains(bind);
        }
    }

    /**
     * The refusal comes BEFORE the database pool opens (nexus-vr8wk fix round, critic S3): the same boot with
     * {@code NX_DB_URL} on a port nothing listens on. HikariCP connects eagerly, so a Main that opened the pool
     * first would die on a pool-initialization error and never log the refusal; a Main that refuses first logs it
     * and exits 1 without ever touching the database.
     */
    @Test
    void theTenantResetRouteOnANonLoopbackBind_isRefusedBeforeThePoolOpens() throws Exception {
        int closedPort;
        try (var s = new java.net.ServerSocket(0)) {
            closedPort = s.getLocalPort();
        }
        for (String bind : new String[] {"0.0.0.0", "10.1.2.3"}) {
            Boot boot = bootWith(java.util.Map.of("NX_TEST_TENANT_RESET_ROUTE", "1", "NX_SERVICE_BIND", bind,
                "NX_DB_URL", "jdbc:postgresql://127.0.0.1:" + closedPort + "/postgres"), MIGRATION_START);

            assertThat(lines(boot, BIND_REFUSED)).as("bind " + bind + ": the refusal event, with no database to "
                + "open a pool on:\n" + boot.output()).hasSize(1);
            assertThat(lines(boot, BIND_REFUSED).get(0)).contains("NX_TEST_TENANT_RESET_ROUTE").contains(bind);
            assertThat(boot.exitCode()).as(boot.output()).isEqualTo(1);
            assertThat(boot.output()).as("the pool was never opened")
                .doesNotContain("HikariPool").doesNotContain("PoolInitializationException");
        }
    }

    /** The controls: the flag on a loopback bind boots through to the migration, and a non-loopback bind
     *  without the flag does too. A refusal that fired on either would make the test above vacuous. */
    @Test
    void theBindCheckLetsLoopbackWithTheFlag_andAnyBindWithoutIt_throughToTheMigration() throws Exception {
        Boot loopback = bootWith(java.util.Map.of("NX_TEST_TENANT_RESET_ROUTE", "1", "NX_SERVICE_BIND", "127.0.0.1"),
            MIGRATION_START);
        assertThat(loopback.reachedMigration()).as(loopback.output()).isTrue();
        assertThat(loopback.output()).doesNotContain(BIND_REFUSED);

        Boot noFlag = bootWith(java.util.Map.of("NX_SERVICE_BIND", "0.0.0.0"), MIGRATION_START);
        assertThat(noFlag.reachedMigration()).as(noFlag.output()).isTrue();
        assertThat(noFlag.output()).doesNotContain(BIND_REFUSED);
    }

    // ── one engine per database (nexus-5tykh) ────────────────────────────────────────────────────────────

    private static final String LOCK_REFUSED = "event=engine_singleton_lock_refused";

    /** A connection of this test that holds the engine singleton lock, standing in for a running first engine. */
    private java.sql.Connection holdTheEngineLock() throws Exception {
        var props = new java.util.Properties();
        props.setProperty("user", pg.getUsername());
        props.setProperty("password", pg.getPassword());
        props.setProperty("ApplicationName", "first-engine-stand-in");
        var c = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), props);
        var took = org.jooq.impl.DSL.using(c, org.jooq.SQLDialect.POSTGRES)
            .select(org.jooq.impl.DSL.function("pg_try_advisory_lock", org.jooq.impl.SQLDataType.BOOLEAN,
                org.jooq.impl.DSL.val(dev.nexus.service.db.EngineSingletonLock.LOCK_KEY)))
            .fetchOne(0, Boolean.class);
        assertThat(took).as("the stand-in took the lock").isTrue();
        return c;
    }

    /** The stand-in's backend pid and the address Postgres records for it, as the refusal line must repeat them. */
    private record StandIn(int pid, String clientAddr) { }

    private StandIn standIn() throws Exception {
        try (var c = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword())) {
            var activity = org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("pg_catalog", "pg_stat_activity"));
            var rec = org.jooq.impl.DSL.using(c, org.jooq.SQLDialect.POSTGRES)
                .select(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("pid"), Integer.class),
                    org.jooq.impl.DSL.function("host", org.jooq.impl.SQLDataType.VARCHAR,
                        org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("client_addr"))))
                .from(activity)
                .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("application_name"), String.class)
                    .eq("first-engine-stand-in"))
                .fetchOne();
            return new StandIn(rec.value1(), rec.value2());
        }
    }

    /**
     * A real boot against a database whose lock another session holds exits 1 after the wait, before the schema
     * migration, and its refusal line names the holder's backend pid and client address: the operator's handle for a
     * half-open connection left by a stopped engine (conexus, 2026-10-22).
     */
    @Test
    void aSecondEngine_isRefusedBeforeTheMigration_andTheLogNamesTheHolderPidAndClientAddr() throws Exception {
        try (var holder = holdTheEngineLock()) {
            StandIn holderBackend = standIn();
            String pid = Integer.toString(holderBackend.pid());
            String addr = holderBackend.clientAddr();
            assertThat(addr).as("the stand-in came in over TCP").isNotBlank();

            Boot boot = bootWith(java.util.Map.of("NX_ENGINE_SINGLETON_LOCK_WAIT_SECONDS", "1"), MIGRATION_START);

            assertThat(boot.exitCode()).as(boot.output()).isEqualTo(1);
            assertThat(boot.reachedMigration()).as("a refused engine never migrates:\n" + boot.output()).isFalse();
            assertThat(lines(boot, LOCK_REFUSED)).as("exactly one refusal event:\n" + boot.output()).hasSize(1);
            assertThat(lines(boot, LOCK_REFUSED).get(0))
                .contains("holder_pid=" + pid)
                .contains("holder_client_addr=" + addr)
                .contains("holder_application_name=first-engine-stand-in")
                .doesNotContain(pg.getPassword());
        }
    }

    /** The control for the refusal above: with the lock free the same boot takes it and reaches the migration. */
    @Test
    void anEngineWithTheLockFree_takesItBeforeTheMigration() throws Exception {
        Boot boot = boot(null, null);

        assertThat(boot.reachedMigration()).as(boot.output()).isTrue();
        assertThat(boot.output()).doesNotContain(LOCK_REFUSED);
        assertThat(boot.output().indexOf("event=engine_singleton_lock_acquired"))
            .as("acquired, and before the migration starts:\n" + boot.output()).isPositive()
            .isLessThan(boot.output().indexOf(MIGRATION_START));
    }

    /** The operator's opt-out boots past a held lock and says so at WARN. */
    @Test
    void theOptOut_bootsPastAHeldLock_withAWarn() throws Exception {
        try (var holder = holdTheEngineLock()) {
            Boot boot = boot("NX_ENGINE_SINGLETON_LOCK", "off");

            assertThat(boot.reachedMigration()).as(boot.output()).isTrue();
            assertThat(boot.output()).doesNotContain(LOCK_REFUSED);
            assertThat(lines(boot, "event=engine_singleton_lock_disabled")).as(boot.output()).hasSize(1);
            assertThat(lines(boot, "event=engine_singleton_lock_disabled").get(0)).contains("WARN");
        }
    }

    @Test
    void aMalformedSingletonLockSetting_exitsOneNamingTheVariable_beforeTheMigration() throws Exception {
        Boot boot = boot("NX_ENGINE_SINGLETON_LOCK", "maybe");

        assertThat(boot.exitCode()).as(boot.output()).isEqualTo(1);
        assertThat(boot.reachedMigration()).as(boot.output()).isFalse();
        assertThat(lines(boot, "event=engine_singleton_lock_env_invalid")).as(boot.output()).hasSize(1);
        assertThat(lines(boot, "event=engine_singleton_lock_env_invalid").get(0)).contains("NX_ENGINE_SINGLETON_LOCK");
    }

    /**
     * nexus-s5lfh: an exception from {@code new Bge768Embedder()} that Main does not catch (only
     * ShutdownInProgressException is) ends the JVM on its own, because no non-daemon thread is alive by then
     * (Hikari's pool threads are daemons). The bge files are present, so the boot plan builds the embedder,
     * and the model is a sparse file of zeros past the size floor, so ONNX Runtime refuses it in
     * createSession. A process that stayed up would trip bootWith's 120 s "did not exit" assertion; the
     * native smoke would read that as a 60 s "never became healthy" FAIL, never a pass.
     */
    @Test
    void anUncaughtBgeEmbedderFailure_endsTheProcessOnItsOwn() throws Exception {
        Path root = java.nio.file.Files.createTempDirectory("nx-s5lfh-onnx");
        try {
            Path onnx = java.nio.file.Files.createDirectories(root.resolve("bge-base-en-v1.5").resolve("onnx"));
            try (var model = new java.io.RandomAccessFile(onnx.resolve("model.onnx").toFile(), "rw")) {
                model.setLength(250_000_000L); // over Bge768Embedder.MIN_MODEL_BYTES, all zeros: not an ONNX model
            }
            java.nio.file.Files.writeString(onnx.resolve("tokenizer.json"), "{}");
            long t0 = System.nanoTime();
            Boot boot = bootWith(java.util.Map.of("NX_ONNX_MODEL_DIR", root.toString()), "event=never-logged-s5lfh");
            long seconds = TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - t0);

            assertThat(boot.output()).as("the failure is the embedder's").contains("Failed to initialise Bge768Embedder");
            assertThat(boot.output()).as("it escaped main").contains("Exception in thread \"main\"");
            assertThat(boot.logged("event=service_ready")).as(boot.output()).isFalse();
            assertThat(boot.exitCode()).as(boot.output()).isNotZero();
            assertThat(seconds).as("the process ended on its own, well inside the smoke's 60 s health wait")
                .isLessThan(60);
        } finally {
            try (var walk = java.nio.file.Files.walk(root)) {
                walk.sorted(java.util.Comparator.reverseOrder()).forEach(q -> q.toFile().delete());
            }
        }
    }
}
