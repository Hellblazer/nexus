// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-8ool4 -- real JVMs: two engine-style starts that initialise ONNX Runtime and are then killed
 * without a shutdown hook (the way a SIGKILLed engine ends) share one native directory and put no
 * library in the temp directory.
 *
 * <p>Each child gets {@code -Djava.io.tmpdir=<scratch>}, so the assertion looks at the directory
 * onnxruntime-java would otherwise fill, never at the machine's real {@code /tmp}.
 *
 * <p>Non-vacuity: a control child started with {@code --no-fix} (the old behaviour) must leave the
 * large library in the scratch temp directory, so the "nothing left" assertion could fail.
 */
class OrtNativeDirProcessTest {

    private record Run(int exit, String output) {}

    private static Run run(Path scratchTmp, Path root, String... args) throws IOException, InterruptedException {
        Path java = Path.of(System.getProperty("java.home"), "bin", "java");
        List<String> cmd = new ArrayList<>(List.of(
                java.toString(),
                "-Djava.io.tmpdir=" + scratchTmp,
                "-cp", System.getProperty("java.class.path"),
                OrtNativeDirProbe.class.getName()));
        cmd.addAll(List.of(args));
        ProcessBuilder pb = new ProcessBuilder(cmd).redirectErrorStream(true);
        pb.environment().put(OrtNativeDir.ROOT_ENV, root.toString());
        Process p = pb.start();
        String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        assertThat(p.waitFor(120, TimeUnit.SECONDS)).as("child finished: %s", out).isTrue();
        return new Run(p.exitValue(), out);
    }

    private static long bytesUnder(Path dir) throws IOException {
        try (Stream<Path> s = Files.walk(dir)) {
            return s.filter(Files::isRegularFile).mapToLong(f -> f.toFile().length()).sum();
        }
    }

    private static List<String> ortTempDirs(Path scratchTmp) throws IOException {
        try (Stream<Path> s = Files.list(scratchTmp)) {
            return s.map(p -> p.getFileName().toString())
                    .filter(n -> n.startsWith(OrtTempSweep.DIR_PREFIX)).sorted().toList();
        }
    }

    @Test
    @Timeout(value = 5, unit = TimeUnit.MINUTES)
    void twoKilledStartsShareOneNativeDirectoryAndLeaveNoLibraryInTmp(
            @TempDir Path scratchTmp, @TempDir Path root) throws Exception {
        Assumptions.assumeTrue(!System.getProperty("os.name", "").toLowerCase().contains("win"),
                "a Windows temp directory holds loaded DLLs open");

        Run first = run(scratchTmp, root);
        Run second = run(scratchTmp, root);

        for (Run r : List.of(first, second)) {
            assertThat(r.exit()).as("halted by the probe, not crashed: %s", r.output()).isEqualTo(137);
            assertThat(r.output()).contains("ORT_VERSION=" + OrtNativeDir.ORT_VERSION);
        }
        List<Path> dirs;
        try (Stream<Path> s = Files.list(root)) {
            dirs = s.toList();
        }
        assertThat(dirs).as("one shared directory under the root, no leftover stage").hasSize(1);
        Path shared = dirs.get(0);
        assertThat(first.output()).contains("NATIVE_PATH=" + shared);
        assertThat(second.output()).contains("NATIVE_PATH=" + shared);
        assertThat(bytesUnder(shared)).as("the libraries were extracted once").isGreaterThan(5_000_000L);

        // The killed starts each left onnxruntime-java's own directory, but nothing in it.
        assertThat(ortTempDirs(scratchTmp)).as("onnxruntime-java still creates its directory per start")
                .hasSize(2);
        assertThat(bytesUnder(scratchTmp)).as("no native library, or any other file, in the temp dir")
                .isZero();
    }

    @Test
    @Timeout(value = 5, unit = TimeUnit.MINUTES)
    void controlWithoutTheFixLeavesTheLibraryInTmp(@TempDir Path scratchTmp, @TempDir Path root) throws Exception {
        Assumptions.assumeTrue(!System.getProperty("os.name", "").toLowerCase().contains("win"),
                "a Windows temp directory holds loaded DLLs open");

        Run r = run(scratchTmp, root, "--no-fix");

        assertThat(r.exit()).as(r.output()).isEqualTo(137);
        assertThat(r.output()).contains("NATIVE_PATH=null");
        assertThat(bytesUnder(scratchTmp))
                .as("the old behaviour: the 16 MB library lands in the temp dir and a kill leaves it")
                .isGreaterThan(5_000_000L);
        assertThat(bytesUnder(root)).isZero();
    }
}
