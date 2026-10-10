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
        String java = Path.of(System.getProperty("java.home"), "bin", "java").toString();
        ProcessBuilder pb = new ProcessBuilder(java, "-cp", System.getProperty("java.class.path"),
            "dev.nexus.service.Main");
        var env = pb.environment();
        env.put("NX_DB_URL", pg.getJdbcUrl());
        env.put("NX_DB_USER", pg.getUsername());
        env.put("NX_DB_PASS", pg.getPassword());
        env.put("NX_SERVICE_PORT", "0");
        env.remove("NX_VOYAGE_API_KEY");
        if (variable != null) {
            env.put(variable, value);
        }
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
}
