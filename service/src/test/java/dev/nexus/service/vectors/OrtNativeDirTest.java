// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.file.attribute.FileTime;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/** nexus-8ool4 -- the extract-once, reuse-always contract of {@link OrtNativeDir}. */
class OrtNativeDirTest {

    private static final String OS = System.getProperty("os.name");
    private static final String ARCH = System.getProperty("os.arch");

    /** The real jar's resources: the same bytes production extracts. */
    private static final OrtNativeDir.Resources REAL =
            name -> ai.onnxruntime.OrtEnvironment.class.getResourceAsStream(name);

    private static List<String> names(Path dir) throws IOException {
        try (Stream<Path> s = Files.list(dir)) {
            return s.map(p -> p.getFileName().toString()).sorted().toList();
        }
    }

    @Test
    void extractsOnceThenReusesTheSameFiles(@TempDir Path root) throws Exception {
        OrtNativeDir.Result first = OrtNativeDir.ensure(root, OS, ARCH, REAL);
        assertThat(first.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        Path dir = first.dir();
        assertThat(dir.getParent()).isEqualTo(root);
        Path lib = dir.resolve(OrtNativeDir.mapLibraryName("onnxruntime"));
        Path jni = dir.resolve(OrtNativeDir.mapLibraryName("onnxruntime4j_jni"));
        assertThat(lib).isRegularFile();
        assertThat(Files.size(lib)).as("the real native library, not a stub").isGreaterThan(5_000_000L);
        assertThat(jni).isRegularFile();
        FileTime libStamp = Files.getLastModifiedTime(lib);
        Object libKey = Files.readAttributes(lib, BasicFileAttributes.class).fileKey();

        // A second initialisation must read nothing from the jar at all.
        OrtNativeDir.Result second = OrtNativeDir.ensure(root, OS, ARCH, name -> {
            throw new AssertionError("a reuse must not open a jar resource: " + name);
        });
        assertThat(second.outcome()).isEqualTo(OrtNativeDir.Outcome.REUSED);
        assertThat(second.dir()).isEqualTo(dir);
        assertThat(Files.getLastModifiedTime(lib)).isEqualTo(libStamp);
        assertThat(Files.readAttributes(lib, BasicFileAttributes.class).fileKey()).isEqualTo(libKey);
        assertThat(names(root)).as("no stage directory left behind").containsExactly(dir.getFileName().toString());
    }

    @Test
    void concurrentFirstStartsAgreeOnOneCompleteDirectory(@TempDir Path root) throws Exception {
        int n = 8;
        ExecutorService pool = Executors.newFixedThreadPool(n);
        try {
            CountDownLatch go = new CountDownLatch(1);
            List<Future<OrtNativeDir.Result>> futures = new ArrayList<>();
            for (int i = 0; i < n; i++) {
                futures.add(pool.submit(() -> {
                    go.await();
                    return OrtNativeDir.ensure(root, OS, ARCH, REAL);
                }));
            }
            go.countDown();
            List<OrtNativeDir.Result> results = new ArrayList<>();
            for (Future<OrtNativeDir.Result> f : futures) {
                results.add(f.get(60, TimeUnit.SECONDS));
            }
            Path dir = results.get(0).dir();
            assertThat(dir).isNotNull();
            assertThat(results).allSatisfy(r -> assertThat(r.dir()).isEqualTo(dir));
            assertThat(results.stream().filter(r -> r.outcome() == OrtNativeDir.Outcome.EXTRACTED).count())
                    .as("exactly one starter publishes; the rest reuse or lose the rename")
                    .isEqualTo(1);
            assertThat(results).extracting(OrtNativeDir.Result::outcome)
                    .containsOnly(OrtNativeDir.Outcome.EXTRACTED, OrtNativeDir.Outcome.REUSED);
            assertThat(names(root)).as("no stage directory left behind").containsExactly(dir.getFileName().toString());
            String lib = OrtNativeDir.mapLibraryName("onnxruntime");
            try (var in = REAL.open("/ai/onnxruntime/native/" + OrtNativeDir.osArch(OS, ARCH) + "/" + lib)) {
                assertThat(Files.size(dir.resolve(lib)))
                        .as("the published library is whole")
                        .isEqualTo(in.readAllBytes().length);
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void aBrokenFinalDirectoryIsReplaced(@TempDir Path root) throws Exception {
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, REAL).dir();
        Files.delete(dir.resolve(OrtNativeDir.mapLibraryName("onnxruntime4j_jni")));

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, REAL);

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(r.dir().resolve(OrtNativeDir.mapLibraryName("onnxruntime4j_jni"))).isRegularFile();
    }

    @Test
    void aStageLeftByAKilledStarterIsSweptAndAFreshOneIsKept(@TempDir Path root) throws Exception {
        Path old = Files.createDirectory(root.resolve(".stage-old"));
        Files.writeString(old.resolve("libonnxruntime.so"), "partial");
        Files.setLastModifiedTime(old, FileTime.from(Instant.now().minus(Duration.ofHours(1))));
        Path fresh = Files.createDirectory(root.resolve(".stage-fresh"));

        assertThat(OrtNativeDir.ensure(root, OS, ARCH, REAL).dir()).isNotNull();

        assertThat(old).doesNotExist();
        assertThat(fresh).as("a stage a live starter may be filling").exists();
    }

    @Test
    void aMissingResourceLeavesNothingAndNoDirectory(@TempDir Path root) throws Exception {
        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, name -> null);

        assertThat(r.dir()).isNull();
        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        assertThat(names(root)).isEmpty();
    }

    @Test
    void aResourceTheJarDoesCarryIsExtractedByItsMappedName(@TempDir Path root) throws Exception {
        // A fake jar that also carries the optional providers library: it lands beside the required ones.
        String platform = OrtNativeDir.osArch(OS, ARCH);
        List<String> carried = List.of(
                OrtNativeDir.mapLibraryName("onnxruntime_providers_shared"),
                OrtNativeDir.mapLibraryName("onnxruntime"),
                OrtNativeDir.mapLibraryName("onnxruntime4j_jni"));
        OrtNativeDir.Resources fake = name -> carried.stream().anyMatch(name::endsWith)
                ? new ByteArrayInputStream("x".getBytes(StandardCharsets.UTF_8)) : null;

        Path dir = OrtNativeDir.ensure(root, OS, ARCH, fake).dir();

        assertThat(dir.getFileName().toString()).isEqualTo(OrtNativeDir.ORT_VERSION + "-" + platform);
        assertThat(names(dir)).containsExactlyInAnyOrderElementsOf(carried);
    }

    @Test
    void anUnsupportedPlatformPreparesNothing(@TempDir Path root) throws Exception {
        assertThat(OrtNativeDir.ensure(root, "Plan 9", "mips", REAL).dir()).isNull();
        assertThat(OrtNativeDir.osArch("Mac OS X", "aarch64")).isEqualTo("osx-aarch64");
        assertThat(OrtNativeDir.osArch("Windows 11", "amd64")).isEqualTo("win-x64");
        assertThat(OrtNativeDir.osArch("Linux", "x86_64")).isEqualTo("linux-x64");
        assertThat(OrtNativeDir.osArch("Linux", "aarch64")).isEqualTo("linux-aarch64");
        assertThat(names(root)).isEmpty();
    }

    @Test
    void theOperatorsPropertyWinsAndNothingIsExtracted(@TempDir Path root) throws Exception {
        String before = System.getProperty(OrtNativeDir.PROPERTY);
        System.setProperty(OrtNativeDir.PROPERTY, "/operator/chosen");
        try {
            OrtNativeDir.Result r = OrtNativeDir.applyAtBoot(
                    k -> OrtNativeDir.ROOT_ENV.equals(k) ? root.toString() : null, "/nowhere", OS, ARCH);

            assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.OPERATOR_SET);
            assertThat(System.getProperty(OrtNativeDir.PROPERTY)).isEqualTo("/operator/chosen");
            assertThat(names(root)).as("no extraction when the operator set the property").isEmpty();
        } finally {
            restore(before);
        }
    }

    @Test
    void applyAtBootSetsThePropertyToTheExtractedDirectory(@TempDir Path root) throws Exception {
        String before = System.getProperty(OrtNativeDir.PROPERTY);
        System.clearProperty(OrtNativeDir.PROPERTY);
        try {
            OrtNativeDir.Result r = OrtNativeDir.applyAtBoot(
                    k -> OrtNativeDir.ROOT_ENV.equals(k) ? root.toString() : null, "/nowhere", OS, ARCH);

            assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
            assertThat(System.getProperty(OrtNativeDir.PROPERTY)).isEqualTo(r.dir().toString());
        } finally {
            restore(before);
        }
    }

    private static void restore(String before) {
        if (before == null) {
            System.clearProperty(OrtNativeDir.PROPERTY);
        } else {
            System.setProperty(OrtNativeDir.PROPERTY, before);
        }
    }

    @Test
    void theRootIsTheEnvOverrideThenHomeThenUserHomeNeverTheTempDir() {
        assertThat(OrtNativeDir.resolveRoot(k -> k.equals("NX_ORT_NATIVE_DIR") ? " /x/ort " : "/h", "/u"))
                .isEqualTo(Path.of("/x/ort"));
        assertThat(OrtNativeDir.resolveRoot(k -> k.equals("HOME") ? "/h" : null, "/u"))
                .isEqualTo(Path.of("/h/.cache/nexus/ort_native"));
        assertThat(OrtNativeDir.resolveRoot(k -> null, "/u"))
                .isEqualTo(Path.of("/u/.cache/nexus/ort_native"));
    }

    @Test
    void mainSetsThePathAfterTheGateAndBeforeAnyEmbedderIsBuilt() throws Exception {
        String main = Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
        int gate = main.indexOf("OrtInitGate.process().installSignalHandlers();");
        int apply = main.indexOf("OrtNativeDir.applyAtBoot();");
        int firstEmbedder = main.indexOf("new Bge768Embedder()");
        assertThat(gate).isPositive();
        assertThat(apply).as("Main calls OrtNativeDir.applyAtBoot()").isPositive();
        assertThat(firstEmbedder).isPositive();
        assertThat(apply).as("after the signal gate is installed, which Main does first thing")
                .isGreaterThan(gate);
        assertThat(apply).as("before ONNX Runtime is first touched, or the property is read too late")
                .isLessThan(firstEmbedder);
    }

    // ── ORT_VERSION is a property of the pinned jar ─────────────────────────────────────────────

    @Test
    void theDirectoryVersionIsThePomsAndTheJarsOnTheClasspath() throws Exception {
        String pom = Files.readString(Path.of("pom.xml"));
        Matcher m = Pattern.compile("<onnxruntime.version>([^<]+)</onnxruntime.version>").matcher(pom);
        assertThat(m.find()).as("pom.xml pins onnxruntime.version").isTrue();
        assertThat(OrtNativeDir.ORT_VERSION)
                .as("OrtNativeDir.ORT_VERSION names the extraction directory; a jar bump must change it, "
                        + "or the new jar loads the old jar's libraries")
                .isEqualTo(m.group(1));
        Path jar = Path.of(ai.onnxruntime.OrtEnvironment.class.getProtectionDomain()
                .getCodeSource().getLocation().toURI());
        assertThat(jar.getFileName().toString()).isEqualTo("onnxruntime-" + OrtNativeDir.ORT_VERSION + ".jar");
    }
}
