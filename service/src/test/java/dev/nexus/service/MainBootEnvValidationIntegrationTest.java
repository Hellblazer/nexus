// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.concurrent.TimeUnit;

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

    private record Boot(int exitCode, String output) { }

    /** Boot Main in a child JVM with one extra env variable; returns its exit code and combined output. */
    private Boot boot(String variable, String value) throws IOException, InterruptedException {
        String java = Path.of(System.getProperty("java.home"), "bin", "java").toString();
        ProcessBuilder pb = new ProcessBuilder(java, "-cp", System.getProperty("java.class.path"),
            "dev.nexus.service.Main");
        var env = pb.environment();
        env.put("NX_DB_URL", pg.getJdbcUrl());
        env.put("NX_DB_USER", pg.getUsername());
        env.put("NX_DB_PASS", pg.getPassword());
        env.put("NX_SERVICE_PORT", "0");
        env.remove("NX_VOYAGE_API_KEY");
        env.put(variable, value);
        pb.redirectErrorStream(true);
        Process p = pb.start();
        // Drain on a thread of its own so a full pipe cannot stall the child while we wait on it.
        var out = new java.util.concurrent.atomic.AtomicReference<String>("");
        Thread reader = new Thread(() -> {
            try {
                out.set(new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8));
            } catch (IOException e) {
                out.set("read failed: " + e);
            }
        }, "main-boot-output");
        reader.setDaemon(true);
        reader.start();
        if (!p.waitFor(120, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            reader.join(5_000);
            throw new AssertionError("Main did not exit within 120 s; output so far:\n" + out.get());
        }
        reader.join(10_000);
        return new Boot(p.exitValue(), out.get());
    }

    @Test
    void aMalformedNxSearchPci_exitsOne_andTheLogNamesTheVariable() throws Exception {
        Boot boot = boot("NX_SEARCH_PCI", "yes");

        assertThat(boot.exitCode()).as(boot.output()).isEqualTo(1);
        assertThat(boot.output()).contains("event=pg_session_env_invalid").contains("NX_SEARCH_PCI must be 1");
        assertThat(boot.output()).as("not an escaped static-init failure")
            .doesNotContain("Exception in thread \"main\"");
    }

    @Test
    void aMalformedSweepSeconds_pastTheNewMaximum_exitsOne() throws Exception {
        Boot boot = boot("NX_SEARCH_PCI_SWEEP_SECONDS", "3601");

        assertThat(boot.exitCode()).as(boot.output()).isEqualTo(1);
        assertThat(boot.output()).contains("event=pg_session_env_invalid").contains("NX_SEARCH_PCI_SWEEP_SECONDS");
    }
}
