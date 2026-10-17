// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.file.attribute.FileTime;
import java.nio.file.attribute.PosixFilePermissions;
import java.nio.channels.FileChannel;
import java.nio.file.StandardOpenOption;
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

    /** Small deterministic stand-in for the jar: same bytes on every open, any size, no 25 MB copies. */
    private static OrtNativeDir.Resources small() {
        byte[] bytes = new byte[64 * 1024];
        for (int i = 0; i < bytes.length; i++) {
            bytes[i] = (byte) (i * 31 + 7);
        }
        return name -> new ByteArrayInputStream(bytes);
    }

    private static Path lib(Path dir, String library) {
        return dir.resolve(OrtNativeDir.mapLibraryName(library));
    }

    @Test
    void aStaleStageIsSweptEvenWhenTheFinalDirectoryIsAlreadyComplete(@TempDir Path root) throws Exception {
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        Path old = Files.createDirectory(root.resolve(".stage-killed"));
        Files.writeString(old.resolve("libonnxruntime.so"), "partial");
        Files.setLastModifiedTime(old, FileTime.from(Instant.now().minus(Duration.ofHours(1))));
        Path oldBroken = Files.createDirectory(root.resolve(".broken-killed"));
        Files.setLastModifiedTime(oldBroken, FileTime.from(Instant.now().minus(Duration.ofHours(1))));
        Path fresh = Files.createDirectory(root.resolve(".stage-live"));

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, name -> {
            throw new AssertionError("a reuse must not open a jar resource: " + name);
        });

        assertThat(r.outcome()).as("the complete directory is reused").isEqualTo(OrtNativeDir.Outcome.REUSED);
        assertThat(r.dir()).isEqualTo(dir);
        assertThat(old).as("a killed starter's stage goes on the reuse path too").doesNotExist();
        assertThat(oldBroken).doesNotExist();
        assertThat(fresh).as("a stage a live starter may be filling").exists();
    }

    @Test
    void aTruncatedLibraryCountsAsBrokenAndIsReplaced(@TempDir Path root) throws Exception {
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        Path jni = lib(dir, "onnxruntime4j_jni");
        long whole = Files.size(jni);
        try (FileChannel ch = FileChannel.open(jni, StandardOpenOption.WRITE)) {
            ch.truncate(whole / 2);
        }
        assertThat(Files.size(jni)).isLessThan(whole);

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(Files.size(lib(r.dir(), "onnxruntime4j_jni"))).isEqualTo(whole);
        assertThat(names(root)).as("the broken directory was deleted, not left")
                .containsExactlyInAnyOrder(dir.getFileName().toString(), ".lock");
    }

    @Test
    void aMissingSizeManifestCountsAsBroken(@TempDir Path root) throws Exception {
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        Files.delete(dir.resolve(OrtNativeDir.SIZES_FILE));

        assertThat(OrtNativeDir.ensure(root, OS, ARCH, small()).outcome())
                .isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
    }

    @Test
    void concurrentStartersOnABrokenDirectoryNeverFailAndNeverLeaveItBroken(@TempDir Path base) throws Exception {
        int n = 8;
        int trials = 25;
        ExecutorService pool = Executors.newFixedThreadPool(n);
        try {
            for (int t = 0; t < trials; t++) {
                Path root = Files.createDirectory(base.resolve("trial-" + t));
                Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
                Files.delete(lib(dir, "onnxruntime4j_jni"));
                CountDownLatch go = new CountDownLatch(1);
                List<Future<OrtNativeDir.Result>> futures = new ArrayList<>();
                for (int i = 0; i < n; i++) {
                    futures.add(pool.submit(() -> {
                        go.await();
                        return OrtNativeDir.ensure(root, OS, ARCH, small());
                    }));
                }
                go.countDown();
                for (Future<OrtNativeDir.Result> f : futures) {
                    OrtNativeDir.Result r = f.get(60, TimeUnit.SECONDS);
                    assertThat(r.outcome()).as("trial %d: no starter is spuriously UNAVAILABLE", t)
                            .isIn(OrtNativeDir.Outcome.EXTRACTED, OrtNativeDir.Outcome.REUSED);
                    assertThat(r.dir()).isEqualTo(dir);
                }
                assertThat(OrtNativeDir.ensure(root, OS, ARCH, name -> {
                    throw new AssertionError("the final directory must be complete: " + name);
                }).outcome()).as("trial %d: complete at the end", t).isEqualTo(OrtNativeDir.Outcome.REUSED);
                assertThat(lib(dir, "onnxruntime4j_jni")).isRegularFile();
                assertThat(names(root)).as("trial %d: only the directory and (maybe) the lock remain", t)
                        .allMatch(nm -> nm.equals(dir.getFileName().toString()) || nm.equals(".lock"));
            }
        } finally {
            pool.shutdownNow();
        }
    }

    private static void assumePosix(Path p) {
        Assumptions.assumeTrue(p.getFileSystem().supportedFileAttributeViews().contains("posix"),
                "POSIX ownership and modes only");
    }

    @Test
    void aGroupOrWorldWritableRootIsNotUsed(@TempDir Path root) throws Exception {
        assumePosix(root);
        Files.setPosixFilePermissions(root, PosixFilePermissions.fromString("rwxrwx---"));

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        assertThat(r.dir()).isNull();
        assertThat(names(root)).as("nothing extracted into an untrusted root").isEmpty();
        Files.setPosixFilePermissions(root, PosixFilePermissions.fromString("rwx----w-"));
        assertThat(OrtNativeDir.ensure(root, OS, ARCH, small()).outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
    }

    @Test
    void aRootOwnedBySomeoneElseIsNotUsed(@TempDir Path root) throws Exception {
        assumePosix(root);

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small(), "not-the-owner-of-this-dir");

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        assertThat(names(root)).isEmpty();
        assertThat(OrtNativeDir.ensure(root, OS, ARCH, small()).outcome())
                .as("the real owner still can").isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
    }

    @Test
    void aWritableFinalDirectoryIsNeitherLoadedNorReplaced(@TempDir Path root) throws Exception {
        assumePosix(root);
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwxrwxrwx"));
        Object key = Files.readAttributes(dir, BasicFileAttributes.class).fileKey();

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        assertThat(Files.readAttributes(dir, BasicFileAttributes.class).fileKey())
                .as("a directory we do not trust is left exactly as found").isEqualTo(key);
        assertThat(lib(dir, "onnxruntime")).isRegularFile();
    }

    @Test
    void anUnwritableCacheFallsBackToTheOldPathWithoutThrowing(@TempDir Path base) throws Exception {
        // A regular file where the root should be: the directory cannot be created, whoever runs this.
        Path root = Files.writeString(base.resolve("not-a-directory"), "x");
        String before = System.getProperty(OrtNativeDir.PROPERTY);
        System.clearProperty(OrtNativeDir.PROPERTY);
        try {
            OrtNativeDir.Result r = OrtNativeDir.applyAtBoot(
                    k -> OrtNativeDir.ROOT_ENV.equals(k) ? root.toString() : null, "/nowhere", OS, ARCH);

            assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
            assertThat(r.dir()).isNull();
            assertThat(System.getProperty(OrtNativeDir.PROPERTY)).as("ORT behaves exactly as before").isNull();
            assertThat(OrtNativeDir.ensure(root, OS, ARCH, small()).outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        } finally {
            restore(before);
        }
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
        List<String> expected = new ArrayList<>(carried);
        expected.add(OrtNativeDir.SIZES_FILE);
        assertThat(names(dir)).containsExactlyInAnyOrderElementsOf(expected);
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

    /** Java source with block and line comments removed, so a commented-out call is not a call. */
    static String withoutComments(String src) {
        return src.replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
    }

    @Test
    void theCommentStripperRemovesBothCommentForms() {
        String src = "a(); // OrtNativeDir.applyAtBoot();\n/* OrtNativeDir.applyAtBoot(); */ b();";
        assertThat(withoutComments(src)).doesNotContain("applyAtBoot").contains("a();").contains("b();");
    }

    @Test
    void mainSetsThePathOnlyInTheLocalBranchBeforeTheFirstEmbedderIsBuilt() throws Exception {
        String main = withoutComments(Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java")));
        int gate = main.indexOf("OrtInitGate.process().installSignalHandlers();");
        int apply = main.indexOf("OrtNativeDir.applyAtBoot();");
        int cloud = main.indexOf("new EmbedderRouter(voyageKey");
        int local = main.indexOf("OnnxModelPaths.resolved()");
        int firstEmbedder = main.indexOf("new Bge768Embedder()");
        assertThat(gate).isPositive();
        assertThat(apply).as("Main calls OrtNativeDir.applyAtBoot() (not in a comment)").isPositive();
        assertThat(main.indexOf("OrtNativeDir.applyAtBoot();", apply + 1)).as("exactly one call").isEqualTo(-1);
        assertThat(cloud).isPositive();
        assertThat(local).isPositive();
        assertThat(firstEmbedder).isPositive();
        assertThat(apply).as("after the signal gate is installed, which Main does first thing")
                .isGreaterThan(gate);
        assertThat(apply).as("not in the cloud/Voyage branch, which never touches ONNX Runtime")
                .isGreaterThan(cloud);
        assertThat(apply).as("in the local branch, with the model-root resolution").isGreaterThan(local);
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
