// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.api.io.TempDir;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-8ool4 -- the real engine: {@code Main} booted twice in child JVMs (local mode, the real bge model,
 * a real Postgres), each SIGKILLed once it logs {@code service_ready}. The two boots share one native
 * directory, and with {@code -Djava.io.tmpdir} pointed at a scratch directory the kills leave no file there.
 *
 * <p>Needs the provisioned bge model; skips LOUDLY without it and fails when {@code NX_REQUIRE_ORT_MODEL=1}
 * (see {@link OrtTestModel}). {@code OrtNativeDirProcessTest} covers the same property on CI without a model.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MainOrtNativeDirIntegrationTest {

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

    private record Boot(int exit, String output) {}

    private Boot bootAndKill(Path scratchTmp, Path nativeRoot) throws IOException, InterruptedException {
        String java = Path.of(System.getProperty("java.home"), "bin", "java").toString();
        ProcessBuilder pb = new ProcessBuilder(java, "-Djava.io.tmpdir=" + scratchTmp,
                "-cp", System.getProperty("java.class.path"), "dev.nexus.service.Main");
        var env = pb.environment();
        env.put("NX_DB_URL", pg.getJdbcUrl());
        env.put("NX_DB_USER", pg.getUsername());
        env.put("NX_DB_PASS", pg.getPassword());
        env.put("NX_SERVICE_PORT", "0");
        env.put(OrtNativeDir.ROOT_ENV, nativeRoot.toString());
        env.remove("NX_VOYAGE_API_KEY");
        pb.redirectErrorStream(true);
        Process p = pb.start();
        var out = new StringBuffer();
        var ready = new java.util.concurrent.atomic.AtomicBoolean();
        Thread reader = new Thread(() -> {
            try (var in = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = in.readLine()) != null) {
                    out.append(line).append('\n');
                    if (line.contains("event=service_ready") && ready.compareAndSet(false, true)) {
                        p.destroyForcibly();   // SIGKILL: no shutdown hook, no deleteOnExit
                    }
                }
            } catch (IOException e) {
                out.append("read failed: ").append(e).append('\n');
            }
        }, "main-ort-output");
        reader.setDaemon(true);
        reader.start();
        if (!p.waitFor(180, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            reader.join(5_000);
            throw new AssertionError("Main did not reach service_ready within 180 s; output:\n" + out);
        }
        reader.join(10_000);
        assertThat(ready.get()).as("the boot reached service_ready:\n" + out).isTrue();
        return new Boot(p.exitValue(), out.toString());
    }

    private static long bytesUnder(Path dir) throws IOException {
        try (Stream<Path> s = Files.walk(dir)) {
            return s.filter(Files::isRegularFile).mapToLong(f -> f.toFile().length()).sum();
        }
    }

    private static String listing(Path dir) throws IOException {
        try (Stream<Path> s = Files.walk(dir)) {
            return s.filter(Files::isRegularFile).map(f -> dir.relativize(f) + " " + f.toFile().length())
                    .sorted().toList().toString();
        }
    }

    @Test
    @Timeout(value = 8, unit = TimeUnit.MINUTES)
    void twoKilledEngineBootsShareOneNativeDirectoryAndLeaveNothingInTmp(
            @TempDir Path scratchTmp, @TempDir Path nativeRoot) throws Exception {
        Assumptions.assumeTrue(!System.getProperty("os.name", "").toLowerCase().contains("win"),
                "a Windows temp directory holds loaded DLLs open");
        OrtTestModel.requireBgeOrSkip();

        Boot first = bootAndKill(scratchTmp, nativeRoot);
        Boot second = bootAndKill(scratchTmp, nativeRoot);

        List<Path> dirs;
        try (Stream<Path> s = Files.list(nativeRoot)) {
            dirs = s.toList();
        }
        assertThat(dirs).as("one shared native directory, no stage left").hasSize(1);
        assertThat(first.output()).contains("event=ort_native_dir source=extracted");
        assertThat(second.output()).contains("event=ort_native_dir source=reused");
        assertThat(bytesUnder(dirs.get(0))).isGreaterThan(5_000_000L);
        assertThat(bytesUnder(scratchTmp))
                .as("neither killed engine put a library, or any file, in its temp directory: %s",
                        listing(scratchTmp))
                .isZero();
    }
}
