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
                // Made with the host umask on purpose: a 0002 umask (Ubuntu, lab-run on chas) gives
                // rwxrwxr-x, which ensure() tightens because the current user owns it (nexus-bk7nu).
                Path root = Files.createDirectory(base.resolve("trial-" + t));
                Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
                assertThat(dir).as("trial %d: the first starter extracts", t).isNotNull();
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

    /** A sibling's rename-aside of the version directory: the directory is gone when the caller reads it. */
    private static void renameAside(Path root, Path dir) {
        try {
            Files.move(dir, root.resolve(".gone-" + System.nanoTime()), java.nio.file.StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            throw new AssertionError(e);
        }
    }

    @Test
    void aVersionDirectoryThatVanishesAfterTheExistenceCheckIsAbsentNotUnavailable(@TempDir Path root)
            throws Exception {
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        java.util.concurrent.atomic.AtomicInteger seen = new java.util.concurrent.atomic.AtomicInteger();

        // The sibling renames the directory aside between ensure()'s exists() and its attribute read.
        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small(), System.getProperty("user.name"),
                d -> {
                    seen.incrementAndGet();
                    renameAside(root, d);
                });

        assertThat(seen).as("the seam fired").hasValue(1);
        assertThat(r.outcome()).as("absent means extract, never UNAVAILABLE")
                .isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(r.dir()).isEqualTo(dir);
        assertThat(lib(dir, "onnxruntime4j_jni")).isRegularFile();
    }

    @Test
    void aVersionDirectoryThatVanishesInsideRecoveryIsAbsentNotUnavailable(@TempDir Path root) throws Exception {
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        Files.delete(lib(dir, "onnxruntime4j_jni"));
        java.util.concurrent.atomic.AtomicInteger seen = new java.util.concurrent.atomic.AtomicInteger();

        // Broken: ensure() sees it (call 1, left alone), publish loses, recover() sees it (call 2, vanishes).
        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small(), System.getProperty("user.name"),
                d -> {
                    if (seen.incrementAndGet() == 2) {
                        renameAside(root, d);
                    }
                });

        assertThat(seen).as("both seams fired").hasValue(2);
        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(r.dir()).isEqualTo(dir);
        assertThat(lib(dir, "onnxruntime4j_jni")).isRegularFile();
    }

    @Test
    void aVersionDirectoryRepublishedBetweenTheFailedReadAndTheRecheckIsReadAgainNotRefused(@TempDir Path root)
            throws Exception {
        assumePosix(root);
        Path dir = root.resolve("1.20.0-x");
        java.util.concurrent.atomic.AtomicInteger seen = new java.util.concurrent.atomic.AtomicInteger();
        var trust = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Trust>();

        // nexus-1g32a (chas, 1 in ~20 runs of concurrentStarters*): a starter's read finds nothing because a
        // sibling renamed the broken directory aside, and another sibling publishes a whole one before the
        // read is classified. That was refused as unreadable, so ensure() returned UNAVAILABLE.
        List<String> warnings = warnings(() -> trust.set(OrtNativeDir.versionDirTrust(dir,
                OrtNativeDir.Identity.current(), true, d -> {
                    if (seen.incrementAndGet() == 1) {
                        try {
                            Files.createDirectory(d);
                            Files.setPosixFilePermissions(d, PosixFilePermissions.fromString("rwx------"));
                        } catch (IOException e) {
                            throw new AssertionError(e);
                        }
                    }
                })));

        assertThat(seen).as("the seam fired once, then the republished directory was read").hasValue(1);
        assertThat(trust.get()).isEqualTo(OrtNativeDir.Trust.TRUSTED);
        assertThat(warnings).noneSatisfy(w -> assertThat(w).contains("ort_native_dir_untrusted"));
    }

    @Test
    void aWorldWritableDirectoryRepublishedInTheGapIsJudgedByTheModeRuleNotTrusted(@TempDir Path root)
            throws Exception {
        assumePosix(root);
        Path dir = root.resolve("1.20.0-x");
        var trust = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Trust>();

        // The re-read after a republish runs the unchanged owner and mode rules: a directory another user could
        // have written into is still never reused.
        warnings(() -> trust.set(OrtNativeDir.versionDirTrust(dir, OrtNativeDir.Identity.current(), true, d -> {
            if (!Files.exists(d, java.nio.file.LinkOption.NOFOLLOW_LINKS)) {
                try {
                    Files.createDirectory(d);
                    Files.setPosixFilePermissions(d, PosixFilePermissions.fromString("rwxrwxrwx"));
                } catch (IOException e) {
                    throw new AssertionError(e);
                }
            }
        })));

        assertThat(trust.get()).isEqualTo(OrtNativeDir.Trust.WRITABLE_OWNED);
    }

    @Test
    void aVersionDirectoryThatIsADanglingSymlinkStaysUntrusted(@TempDir Path root) throws Exception {
        assumePosix(root);
        Path dir = Files.createSymbolicLink(root.resolve("1.20.0-x"), root.resolve("missing"));
        var trust = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Trust>();

        List<String> warnings = warnings(() -> trust.set(
                OrtNativeDir.versionDirTrust(dir, OrtNativeDir.Identity.current(), true)));

        assertThat(trust.get()).isEqualTo(OrtNativeDir.Trust.UNTRUSTED);
        assertThat(warnings).anySatisfy(w -> assertThat(w).contains("reason=unreadable").contains(dir.toString()));
    }

    private static void assumePosix(Path p) {
        Assumptions.assumeTrue(p.getFileSystem().supportedFileAttributeViews().contains("posix"),
                "POSIX ownership and modes only");
    }

    private static String mode(Path p) throws IOException {
        return PosixFilePermissions.toString(Files.getPosixFilePermissions(p));
    }

    @Test
    void aGroupWritableRootTheCurrentUserOwnsIsTightenedAndUsed(@TempDir Path root) throws Exception {
        assumePosix(root);
        Files.setPosixFilePermissions(root, PosixFilePermissions.fromString("rwxrwxr-x"));

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(r.dir()).isNotNull();
        assertThat(mode(root)).as("group write removed, nothing else changed").isEqualTo("rwxr-xr-x");
        assertThat(lib(r.dir(), "onnxruntime")).isRegularFile();
    }

    @Test
    void aWorldWritableRootTheCurrentUserOwnsIsTightenedAndKeepsItsOtherBits(@TempDir Path root) throws Exception {
        assumePosix(root);
        Files.setPosixFilePermissions(root, PosixFilePermissions.fromString("rwxrwx-wx"));

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(mode(root)).isEqualTo("rwxr-x--x");
    }

    @Test
    void aRootSomeoneElseOwnsIsNeverTightenedEvenWhenGroupWritable(@TempDir Path root) throws Exception {
        assumePosix(root);
        Files.setPosixFilePermissions(root, PosixFilePermissions.fromString("rwxrwxr-x"));

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small(), "not-the-owner-of-this-dir");

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        assertThat(r.dir()).isNull();
        assertThat(mode(root)).as("a directory we do not own is left exactly as found").isEqualTo("rwxrwxr-x");
        assertThat(names(root)).as("nothing extracted into an untrusted root").isEmpty();
    }

    @Test
    void aSymlinkedRootToAWritableDirectoryIsRefusedAndTheTargetIsNotChanged(@TempDir Path base) throws Exception {
        assumePosix(base);
        Path target = Files.createDirectory(base.resolve("target"));
        Files.setPosixFilePermissions(target, PosixFilePermissions.fromString("rwxrwxr-x"));
        Path link = Files.createSymbolicLink(base.resolve("link"), target);

        OrtNativeDir.Result r = OrtNativeDir.ensure(link, OS, ARCH, small());

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        assertThat(mode(target)).as("never chmod through a link").isEqualTo("rwxrwxr-x");
        assertThat(names(target)).isEmpty();
    }

    @Test
    void aFailedTightenIsRefusedAndTheModeIsLeftAsFound(@TempDir Path dir) throws Exception {
        assumePosix(dir);
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwxrwxr-x"));

        boolean trusted = OrtNativeDir.trusted(dir, System.getProperty("user.name"), (d, perms) -> {
            throw new IOException("chmod refused");
        });

        assertThat(trusted).isFalse();
        assertThat(mode(dir)).isEqualTo("rwxrwxr-x");
    }

    @Test
    void aTightenThatDoesNotTakeEffectIsRefusedByTheRecheck(@TempDir Path dir) throws Exception {
        assumePosix(dir);
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwxrwxr-x"));
        int[] calls = {0};

        boolean trusted = OrtNativeDir.trusted(dir, System.getProperty("user.name"), (d, perms) -> calls[0]++);

        assertThat(calls[0]).as("tightening was attempted once").isEqualTo(1);
        assertThat(trusted).as("a chmod that reported success but changed nothing is not believed").isFalse();
    }

    @Test
    void aWorldWritableRootWithAnUnknownUserIsNotTightened(@TempDir Path dir) throws Exception {
        assumePosix(dir);
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwxrwxrwx"));

        // user == null means the owner cannot be proved; the owner check is skipped, so tightening is too.
        assertThat(OrtNativeDir.trusted(dir, null, (d, perms) -> {
            throw new AssertionError("must not tighten a directory whose owner is unproven");
        })).isFalse();
        assertThat(mode(dir)).isEqualTo("rwxrwxrwx");
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
    void aWritableFinalDirectoryIsNeverReusedEvenWithAPlantedLibraryAndAMatchingSizeManifest(@TempDir Path root)
            throws Exception {
        assumePosix(root);
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        Object oldKey = Files.readAttributes(dir, BasicFileAttributes.class).fileKey();
        // Another user (the directory is world-writable) swaps in a library and rewrites .sizes to match,
        // so the completeness check alone would call it whole.
        byte[] planted = "planted-by-another-user".getBytes(StandardCharsets.UTF_8);
        StringBuilder sizes = new StringBuilder();
        for (String name : List.of("onnxruntime", "onnxruntime4j_jni")) {
            Files.write(lib(dir, name), planted);
            sizes.append(OrtNativeDir.mapLibraryName(name)).append('=').append(planted.length).append('\n');
        }
        Files.writeString(dir.resolve(OrtNativeDir.SIZES_FILE), sizes.toString());
        Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString("rwxrwxrwx"));

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

        assertThat(r.outcome()).as("re-extracted, not reused").isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(r.dir()).isEqualTo(dir);
        assertThat(Files.readAttributes(dir, BasicFileAttributes.class).fileKey())
                .as("the old directory is gone, not tightened in place").isNotEqualTo(oldKey);
        assertThat(Files.readAllBytes(lib(dir, "onnxruntime")))
                .as("the planted bytes are not what is loaded").isNotEqualTo(planted).hasSize(64 * 1024);
        assertThat(mode(dir)).as("the replacement is the stage's private mode").isEqualTo("rwx------");
        assertThat(names(root)).as("the old directory was deleted from its .broken-* name")
                .allMatch(nm -> nm.equals(dir.getFileName().toString()) || nm.equals(".lock"));
    }

    @Test
    void aWritableFinalDirectoryIsReplacedWhateverItsOtherBitsAre(@TempDir Path root) throws Exception {
        assumePosix(root);
        for (String m : List.of("rwxrwxr-x", "rwxr-xrwx", "rwxrwxrwx")) {
            Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
            Object oldKey = Files.readAttributes(dir, BasicFileAttributes.class).fileKey();
            Files.setPosixFilePermissions(dir, PosixFilePermissions.fromString(m));

            OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

            assertThat(r.outcome()).as(m).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
            assertThat(Files.readAttributes(r.dir(), BasicFileAttributes.class).fileKey()).as(m).isNotEqualTo(oldKey);
        }
    }

    /** chmod with a symbolic or octal mode; the JDK cannot set setuid/setgid/sticky. */
    private static void chmod(String mode, Path p) throws Exception {
        Process pr = new ProcessBuilder("chmod", mode, p.toString()).redirectErrorStream(true).start();
        String out = new String(pr.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        assertThat(pr.waitFor()).as("chmod %s: %s", mode, out).isZero();
    }

    private static int special(Path p) {
        return OrtNativeDir.specialBits(p);
    }

    private static void assumeSpecialBitsReadable(Path p) throws Exception {
        assumePosix(p);
        chmod("1777", p);
        Assumptions.assumeTrue(special(p) != 0, "unix:mode attribute not available on this platform");
    }

    @Test
    void aRootWithSetgidOrStickyIsRefusedAndItsBitsAreUntouched(@TempDir Path base) throws Exception {
        Path probe = Files.createDirectory(base.resolve("probe"));
        assumeSpecialBitsReadable(probe);
        for (String m : List.of("2775", "1777", "4775")) {
            Path root = Files.createDirectory(base.resolve("root-" + m));
            chmod(m, root);
            int before = special(root);
            String modeBefore = mode(root);
            assertThat(before).as(m).isNotZero();

            List<String> warnings = warnings(() -> {
                OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());
                assertThat(r.outcome()).as(m).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
                assertThat(r.dir()).isNull();
            });

            assertThat(special(root)).as("%s: special bits untouched", m).isEqualTo(before);
            assertThat(mode(root)).as("%s: no chmod at all", m).isEqualTo(modeBefore);
            assertThat(names(root)).as("%s: nothing extracted", m).isEmpty();
            assertThat(warnings).anySatisfy(w -> assertThat(w).contains("reason=special_bits")
                    .contains("remedy=").contains(root.toString()));
        }
    }

    @Test
    void aSetgidRootThatIsNotGroupOrWorldWritableNeedsNoChmodAndStaysTrusted(@TempDir Path base) throws Exception {
        Path root = Files.createDirectory(base.resolve("root"));
        assumeSpecialBitsReadable(root);
        chmod("2755", root);
        int before = special(root);

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(special(root)).isEqualTo(before);
    }

    @Test
    void aTightenWarnsAtWarnLevelNamingTheDirTheModesAndTheRemedy(@TempDir Path root) throws Exception {
        assumePosix(root);
        Files.setPosixFilePermissions(root, PosixFilePermissions.fromString("rwxrwxr-x"));

        List<String> warnings = warnings(() ->
                assertThat(OrtNativeDir.ensure(root, OS, ARCH, small()).outcome())
                        .isEqualTo(OrtNativeDir.Outcome.EXTRACTED));

        assertThat(warnings).anySatisfy(w -> assertThat(w).contains("event=ort_native_dir_tightened")
                .contains(root.toString()).contains("old_mode=rwxrwxr-x").contains("new_mode=rwxr-xr-x")
                .contains("NX_ORT_NATIVE_DIR").contains("owner-writable only"));
    }

    @Test
    void everyRefusalSaysWhy(@TempDir Path base) throws Exception {
        assumePosix(base);
        String me = System.getProperty("user.name");
        // foreign_owner
        Path foreign = Files.createDirectory(base.resolve("foreign"));
        assertThat(warnings(() -> OrtNativeDir.trusted(foreign, "not-the-owner")))
                .anySatisfy(w -> assertThat(w).contains("reason=foreign_owner").contains("expected_owner=\"not-the-owner\""));
        // unknown_user
        Path unknown = Files.createDirectory(base.resolve("unknown"));
        Files.setPosixFilePermissions(unknown, PosixFilePermissions.fromString("rwxrwxrwx"));
        assertThat(warnings(() -> OrtNativeDir.trusted(unknown, null)))
                .anySatisfy(w -> assertThat(w).contains("reason=unknown_user").contains("expected_owner=\"unknown\"")
                        .doesNotContain("expected_owner=\"null\""));
        // symlink
        Path target = Files.createDirectory(base.resolve("target"));
        Files.setPosixFilePermissions(target, PosixFilePermissions.fromString("rwxrwxr-x"));
        Path link = Files.createSymbolicLink(base.resolve("link"), target);
        assertThat(warnings(() -> OrtNativeDir.trusted(link, me)))
                .anySatisfy(w -> assertThat(w).contains("reason=symlink"));
        // tighten_failed / tighten_ineffective
        Path failing = Files.createDirectory(base.resolve("failing"));
        Files.setPosixFilePermissions(failing, PosixFilePermissions.fromString("rwxrwxr-x"));
        assertThat(warnings(() -> OrtNativeDir.trusted(failing, me, (d, perms) -> {
            throw new IOException("chmod refused");
        }))).anySatisfy(w -> assertThat(w).contains("reason=tighten_failed").contains("chmod refused"));
        assertThat(warnings(() -> OrtNativeDir.trusted(failing, me, (d, perms) -> { })))
                .anySatisfy(w -> assertThat(w).contains("reason=tighten_ineffective"));
    }

    // ---- nexus-1g32a: bounded recovery lock ------------------------------------------------------

    /** Starts a JVM that holds {@code root/.lock} and waits (up to 30 s) until it prints LOCKED. */
    private static Process holdLockInAnotherProcess(Path root) throws Exception {
        Path java = Path.of(System.getProperty("java.home"), "bin", "java");
        Process p = new ProcessBuilder(java.toString(), "-cp", System.getProperty("java.class.path"),
                OrtLockHolderProbe.class.getName(), root.resolve(OrtNativeDir.LOCK_FILE).toString())
                .redirectErrorStream(true).start();
        var locked = new java.util.concurrent.CompletableFuture<Boolean>();
        var seen = new StringBuffer();
        Thread reader = new Thread(() -> {
            try (var in = new java.io.BufferedReader(new java.io.InputStreamReader(p.getInputStream(),
                    StandardCharsets.UTF_8))) {
                String line;
                while ((line = in.readLine()) != null) {
                    if ("LOCKED".equals(line)) {
                        locked.complete(true);
                    } else {
                        seen.append(line).append('\n');
                    }
                }
            } catch (IOException ignored) {
                // the process ended
            }
            locked.complete(false);
        }, "lock-holder-reader");
        reader.setDaemon(true);
        reader.start();
        boolean ok;
        try {
            ok = locked.get(30, TimeUnit.SECONDS);
        } catch (java.util.concurrent.TimeoutException e) {
            ok = false;
        }
        if (!ok) {
            p.destroyForcibly();
            throw new AssertionError("lock holder did not start; it printed: " + seen);
        }
        return p;
    }

    /** A root whose version directory is broken, so ensure() goes through the locked recovery. */
    private static Path rootWithBrokenVersionDir(Path root) throws Exception {
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        Files.delete(lib(dir, "onnxruntime4j_jni"));
        return dir;
    }

    private static OrtNativeDir.Result ensureWith(Path root, OrtNativeDir.Identity who, Duration bound,
                                                  OrtNativeDir.LockAcquirer acquirer) {
        return OrtNativeDir.ensure(root, OS, ARCH, small(), who, d -> { }, bound, acquirer);
    }

    @Test
    @org.junit.jupiter.api.Timeout(value = 60, unit = TimeUnit.SECONDS)
    void aRecoveryLockAnotherProcessHoldsIsWaitedForThenGivenUpOnWithAWarn(@TempDir Path root) throws Exception {
        assumePosix(root);
        Path dir = rootWithBrokenVersionDir(root);
        Process holder = holdLockInAnotherProcess(root);
        try {
            Duration bound = Duration.ofMillis(600);
            long t0 = System.nanoTime();
            var result = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Result>();
            List<String> warnings = warnings(() -> result.set(OrtNativeDir.ensure(root, OS, ARCH, small(),
                    OrtNativeDir.Identity.current(), d -> { }, bound)));
            long waitedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);

            assertThat(waitedMs).as("it waited the whole bound before giving up").isGreaterThanOrEqualTo(600);
            assertThat(result.get().outcome()).as("it carried on unlocked, as when the lock is refused")
                    .isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
            assertThat(lib(dir, "onnxruntime4j_jni")).isRegularFile();
            assertThat(warnings).anySatisfy(w -> assertThat(w).contains("event=ort_native_dir_lock_timeout")
                    .contains("timeout_ms=600"));
        } finally {
            holder.destroyForcibly();
        }
    }

    @Test
    @org.junit.jupiter.api.Timeout(value = 60, unit = TimeUnit.SECONDS)
    void aRecoveryLockReleasedWithinTheBoundIsTakenWithoutAWarn(@TempDir Path root) throws Exception {
        assumePosix(root);
        rootWithBrokenVersionDir(root);
        Process holder = holdLockInAnotherProcess(root);
        long holdMs = 1500;
        long[] releasedAt = new long[1];
        long startedAt = System.nanoTime();
        Thread releaser = new Thread(() -> {
            try {
                Thread.sleep(holdMs);
                releasedAt[0] = System.nanoTime();
                holder.getOutputStream().close();
            } catch (Exception ignored) {
                holder.destroyForcibly();
            }
        });
        releaser.start();
        try {
            var result = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Result>();
            List<String> warnings = warnings(() -> result.set(OrtNativeDir.ensure(root, OS, ARCH, small(),
                    OrtNativeDir.Identity.current(), d -> { }, Duration.ofSeconds(30))));
            long endedAt = System.nanoTime();
            releaser.join();

            assertThat(result.get().outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
            assertThat(releasedAt[0]).as("the holder was released while ensure() was running").isNotZero();
            assertThat(endedAt - releasedAt[0]).as("ensure() could not finish before the holder let go: "
                    + "the lock was contended, not free").isPositive();
            assertThat(TimeUnit.NANOSECONDS.toMillis(endedAt - startedAt)).isGreaterThanOrEqualTo(holdMs);
            assertThat(warnings).noneSatisfy(w -> assertThat(w).contains("lock_timeout"));
        } finally {
            releaser.join();
            holder.destroyForcibly();
        }
    }

    @Test
    void theProductionLockBoundIsTenSeconds() {
        assertThat(OrtNativeDir.LOCK_TIMEOUT).isEqualTo(Duration.ofSeconds(10));
    }

    @Test
    @org.junit.jupiter.api.Timeout(value = 90, unit = TimeUnit.SECONDS)
    void thePublicEnsureGivesUpOnAHeldLockAfterTheProductionBound(@TempDir Path root) throws Exception {
        assumePosix(root);
        rootWithBrokenVersionDir(root);
        Process holder = holdLockInAnotherProcess(root);
        try {
            long t0 = System.nanoTime();
            var result = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Result>();
            List<String> warnings = warnings(() -> result.set(OrtNativeDir.ensure(root, OS, ARCH, small())));
            long waitedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);

            assertThat(waitedMs).as("the 4-argument ensure waits LOCK_TIMEOUT, not forever and not nothing")
                    .isBetween(OrtNativeDir.LOCK_TIMEOUT.toMillis() - 200, 40_000L);
            assertThat(result.get().outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
            assertThat(warnings).anySatisfy(w -> assertThat(w).contains("event=ort_native_dir_lock_timeout")
                    .contains("timeout_ms=" + OrtNativeDir.LOCK_TIMEOUT.toMillis()));
        } finally {
            holder.destroyForcibly();
        }
    }

    /** An acquirer that parks on {@code gate} (as a kernel call stuck on a dead NFS lock daemon would). */
    private static OrtNativeDir.LockAcquirer parkedOn(CountDownLatch gate) {
        return (file, bound, abandoned) -> {
            gate.await();
            return null;
        };
    }

    @Test
    @org.junit.jupiter.api.Timeout(value = 60, unit = TimeUnit.SECONDS)
    void anOpenAndLockThatNeverReturnsIsAbandonedAtTheBoundAndEnsureCarriesOn(@TempDir Path root) throws Exception {
        rootWithBrokenVersionDir(root);
        CountDownLatch neverReleased = new CountDownLatch(1);
        try {
            for (int round = 0; round < 2; round++) {
                long t0 = System.nanoTime();
                var result = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Result>();
                List<String> warnings = warnings(() -> result.set(ensureWith(root,
                        OrtNativeDir.Identity.current(), Duration.ofMillis(400), parkedOn(neverReleased))));
                long waitedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);

                assertThat(waitedMs).as("round %d: bounded by the lock timeout plus slack, though the "
                        + "previous attempt is still stuck", round).isBetween(390L, 10_000L);
                assertThat(result.get().outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
                assertThat(warnings).anySatisfy(w -> assertThat(w).contains("event=ort_native_dir_lock_timeout")
                        .contains("timeout_ms=400"));
                // Break it again so the next round recovers again.
                Files.delete(lib(result.get().dir(), "onnxruntime4j_jni"));
            }
        } finally {
            neverReleased.countDown();
        }
    }

    @Test
    @org.junit.jupiter.api.Timeout(value = 60, unit = TimeUnit.SECONDS)
    void anAttemptThatCompletesAfterTheCallerGaveUpReleasesItsOwnLockAndChannel(@TempDir Path root,
                                                                              @TempDir Path other) throws Exception {
        rootWithBrokenVersionDir(root);
        CountDownLatch gate = new CountDownLatch(1);
        var late = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Held>();
        CountDownLatch returned = new CountDownLatch(1);
        OrtNativeDir.LockAcquirer lateAcquirer = (file, bound, abandoned) -> {
            gate.await();
            FileChannel ch = FileChannel.open(other.resolve("late.lock"),
                    StandardOpenOption.CREATE, StandardOpenOption.WRITE);
            var held = new OrtNativeDir.Held(ch, ch.tryLock());
            late.set(held);
            returned.countDown();
            return held;
        };

        OrtNativeDir.Result r = ensureWith(root, OrtNativeDir.Identity.current(), Duration.ofMillis(300), lateAcquirer);
        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(late.get()).as("the attempt is still parked").isNull();

        gate.countDown();
        assertThat(returned.await(10, TimeUnit.SECONDS)).isTrue();
        OrtNativeDir.Held held = late.get();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while ((held.channel().isOpen() || held.lock().isValid()) && System.nanoTime() < deadline) {
            Thread.sleep(20);
        }
        assertThat(held.lock().isValid()).as("the late lock was released, not leaked").isFalse();
        assertThat(held.channel().isOpen()).as("the late channel was closed, not leaked").isFalse();
    }

    @Test
    @org.junit.jupiter.api.Timeout(value = 60, unit = TimeUnit.SECONDS)
    void aLockTheFilesystemRefusesIsNotATimeoutAndEnsureCarriesOnUnlocked(@TempDir Path root) throws Exception {
        rootWithBrokenVersionDir(root);
        var result = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Result>();
        List<String> warnings = warnings(() -> result.set(ensureWith(root, OrtNativeDir.Identity.current(),
                Duration.ofSeconds(30), (file, bound, abandoned) -> {
                    throw new IOException("locking not supported");
                })));

        assertThat(result.get().outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(warnings).noneSatisfy(w -> assertThat(w).contains("lock_timeout"));
    }

    @Test
    @org.junit.jupiter.api.Timeout(value = 60, unit = TimeUnit.SECONDS)
    void anInterruptWhileWaitingForTheLockIsWarnedKeepsTheFlagAndCarriesOn(@TempDir Path root) throws Exception {
        rootWithBrokenVersionDir(root);
        CountDownLatch neverReleased = new CountDownLatch(1);
        // Interrupt only once the acquirer is entered: from then on the caller has nothing left to do but
        // wait on the handoff, so the interrupt cannot land in extraction I/O (a fixed sleep could, on a
        // loaded box, and fail the extraction with ClosedByInterruptException).
        CountDownLatch entered = new CountDownLatch(1);
        OrtNativeDir.LockAcquirer acquirer = (file, bound, abandoned) -> {
            entered.countDown();
            neverReleased.await();
            return null;
        };
        Thread main = Thread.currentThread();
        Thread interrupter = new Thread(() -> {
            try {
                entered.await();
            } catch (InterruptedException ignored) {
                return;
            }
            main.interrupt();
        });
        var result = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Result>();
        var interruptedAfter = new java.util.concurrent.atomic.AtomicBoolean();
        try {
            interrupter.start();
            long t0 = System.nanoTime();
            List<String> warnings = warnings(() -> {
                result.set(ensureWith(root, OrtNativeDir.Identity.current(), Duration.ofSeconds(30),
                        acquirer));
                interruptedAfter.set(Thread.currentThread().isInterrupted());
            });
            assertThat(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0)).as("woken by the interrupt, "
                    + "not the 30 s bound").isLessThan(20_000L);
            assertThat(warnings).anySatisfy(w -> assertThat(w).contains("event=ort_native_dir_lock_timeout")
                    .contains("interrupted"));
            assertThat(result.get().outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
            assertThat(interruptedAfter.get()).as("the interrupt flag is preserved for the caller").isTrue();
        } finally {
            Thread.interrupted();
            neverReleased.countDown();
            interrupter.join();
        }
    }

    @Test
    void aLockTakenInTimeIsUsedAndClosedWhenRecoveryEnds(@TempDir Path root) throws Exception {
        rootWithBrokenVersionDir(root);
        var held = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Held>();
        OrtNativeDir.Result r = ensureWith(root, OrtNativeDir.Identity.current(), Duration.ofSeconds(30),
                (file, bound, abandoned) -> {
                    FileChannel ch = FileChannel.open(file, StandardOpenOption.CREATE, StandardOpenOption.WRITE);
                    held.set(new OrtNativeDir.Held(ch, ch.tryLock()));
                    return held.get();
                });

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(held.get().lock().isValid()).as("released when recovery ended").isFalse();
        assertThat(held.get().channel().isOpen()).isFalse();
    }

    // ---- nexus-1g32a: the unlocked fallthrough does not move aside a sibling's fresh directory ----

    @Test
    void aWholeDirectoryASiblingPublishesJustBeforeTheMoveAsideIsReusedNotMoved(@TempDir Path root,
                                                                              @TempDir Path spare) throws Exception {
        Path dir = rootWithBrokenVersionDir(root);
        Path whole = OrtNativeDir.ensure(spare, OS, ARCH, small()).dir();
        Object wholeKey = Files.readAttributes(lib(whole, "onnxruntime"), BasicFileAttributes.class).fileKey();
        java.util.concurrent.atomic.AtomicInteger seen = new java.util.concurrent.atomic.AtomicInteger();

        // Call 1 is ensure() (left alone); call 2 is recover(): the sibling swaps in a whole directory there.
        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small(), OrtNativeDir.Identity.current(),
                d -> {
                    if (seen.incrementAndGet() == 2) {
                        renameAside(root, d);
                        try {
                            Files.move(whole, d);
                        } catch (IOException e) {
                            throw new AssertionError(e);
                        }
                    }
                }, Duration.ofSeconds(30));

        assertThat(seen).hasValue(2);
        assertThat(r.outcome()).as("the sibling's directory is reused").isEqualTo(OrtNativeDir.Outcome.REUSED);
        assertThat(r.dir()).isEqualTo(dir);
        assertThat(Files.readAttributes(lib(dir, "onnxruntime"), BasicFileAttributes.class).fileKey())
                .as("the very files the sibling published are still there, not moved aside and replaced")
                .isEqualTo(wholeKey);
    }

    // ---- nexus-1g32a: ownership by uid -----------------------------------------------------------

    private static int uidOfOrSkip(Path p) {
        Integer uid = OrtNativeDir.uidOf(p);
        Assumptions.assumeTrue(uid != null, "unix:uid attribute not available on this platform");
        return uid;
    }

    /** The uid {@code id -u} reports: an oracle that shares no code with the class under test. */
    private static int idDashU() throws Exception {
        Process p;
        try {
            p = new ProcessBuilder("id", "-u").redirectErrorStream(true).start();
        } catch (IOException e) {
            Assumptions.abort("no id(1) on this platform");
            return -1;
        }
        String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8).trim();
        Assumptions.assumeTrue(p.waitFor() == 0 && out.matches("\\d+"), "id -u gave no uid: " + out);
        return Integer.parseInt(out);
    }

    @Test
    void theProcessUidIsTheUidIdReports() throws Exception {
        int expected = idDashU();
        assertThat(OrtNativeDir.processUid()).isEqualTo(expected);
        assertThat(OrtNativeDir.Identity.current().uid()).isEqualTo(expected);
    }

    @Test
    void theProcessUidComesFromProcSelfStatusWhenThereIsOne(@TempDir Path dir) throws Exception {
        uidOfOrSkip(dir);
        Path status = Files.writeString(dir.resolve("status"),
                "Name:\tjava\nUmask:\t0002\nUid:\t1234\t4321\t1234\t4321\nGid:\t1\t1\t1\t1\n");
        var reader = new java.util.concurrent.atomic.AtomicInteger();

        Integer uid = OrtNativeDir.processUid(status, dir.resolve("no-such-tmp"), (p, a, o) -> {
            reader.incrementAndGet();
            throw new AssertionError("the probe file must not be created when /proc answers");
        }, new java.util.concurrent.atomic.AtomicBoolean());

        assertThat(uid).as("the EFFECTIVE uid, the second field").isEqualTo(4321);
        assertThat(reader).hasValue(0);
        assertThat(names(dir)).as("nothing was created").containsExactly("status");
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {
        "Name:\tjava\nGid:\t1\t1\t1\t1\n",            // no Uid: line
        "Uid:\t1234\n",                               // one field
        "Uid:\t1234\tabc\t1234\t1234\n",              // non-numeric effective uid
        "Uid:\t1234\t4294967295\t1234\t1234\n",       // over int range
        "Uid:\n",                                     // empty
    })
    void aMalformedProcStatusGivesNoUid(String body, @TempDir Path dir) throws Exception {
        assertThat(OrtNativeDir.uidFromProcStatus(Files.writeString(dir.resolve("status"), body))).isNull();
    }

    @Test
    void theLockAttemptRunsOnANamedDaemonThread(@TempDir Path root) throws Exception {
        var seen = new java.util.concurrent.atomic.AtomicReference<Thread>();
        OrtNativeDir.acquireBounded(root.resolve(OrtNativeDir.LOCK_FILE), Duration.ofSeconds(5),
                (file, bound, abandoned) -> {
                    seen.set(Thread.currentThread());
                    return null;
                });
        assertThat(seen.get()).isNotNull().isNotSameAs(Thread.currentThread());
        assertThat(seen.get().isDaemon()).isTrue();
        assertThat(seen.get().getName()).isEqualTo("ort-native-dir-lock");
    }

    @Test
    void withoutProcTheProbeFileIsReadWithoutFollowingLinksAndDeleted(@TempDir Path dir) throws Exception {
        int uid = uidOfOrSkip(dir);
        List<List<java.nio.file.LinkOption>> options = new ArrayList<>();

        Integer got = OrtNativeDir.processUid(dir.resolve("no-proc"), dir, (p, a, o) -> {
            options.add(List.of(o));
            return Files.getAttribute(p, a, o);
        }, new java.util.concurrent.atomic.AtomicBoolean());

        assertThat(got).isEqualTo(uid);
        assertThat(options).containsExactly(List.of(java.nio.file.LinkOption.NOFOLLOW_LINKS));
        assertThat(names(dir)).as("the probe file is gone").isEmpty();
    }

    @Test
    void whenNoUidCanBeHadTheOwnerNameFallbackIsWarnedAtWarnLevelOnce(@TempDir Path dir) throws Exception {
        uidOfOrSkip(dir);
        var warned = new java.util.concurrent.atomic.AtomicBoolean();
        List<String> warnings = warnings(() -> {
            // no /proc, and a tmp dir that does not exist: the probe cannot be created.
            assertThat(OrtNativeDir.processUid(dir.resolve("no-proc"), dir.resolve("no-tmp"), Files::getAttribute,
                    warned)).isNull();
            assertThat(OrtNativeDir.processUid(dir.resolve("no-proc"), dir.resolve("no-tmp"), Files::getAttribute,
                    warned)).isNull();
        });

        assertThat(warnings).hasSize(1).first().satisfies(w -> assertThat(w)
                .contains("event=ort_native_dir_uid_unavailable").contains("judged by owner name"));
    }

    @Test
    void aNativeImageErrorReadingTheUidFallsBackInsteadOfEscaping(@TempDir Path dir) throws Exception {
        uidOfOrSkip(dir);
        OrtNativeDir.AttributeReader linkage = (p, a, o) -> {
            throw new LinkageError("unix:uid not registered");
        };
        assertThat(OrtNativeDir.uidOf(dir, linkage)).isNull();
        assertThat(OrtNativeDir.uidOf(dir, (p, a, o) -> {
            throw new AssertionError("a registration error");
        })).isNull();
        assertThat(OrtNativeDir.processUid(dir.resolve("no-proc"), dir, linkage,
                new java.util.concurrent.atomic.AtomicBoolean())).isNull();
        org.junit.jupiter.api.Assertions.assertThrows(OutOfMemoryError.class,
                () -> OrtNativeDir.uidOf(dir, (p, a, o) -> {
                    throw new OutOfMemoryError("not ours to swallow");
                }));
    }

    @Test
    void theUidDecidesOwnershipNotTheNameSoAContainerWithNoPasswdEntryIsTrusted(@TempDir Path dir) throws Exception {
        assumePosix(dir);
        int uid = uidOfOrSkip(dir);
        // user.name says something the owner's name never matches (a container: "?" vs the bare uid).
        assertThat(OrtNativeDir.trustedBy(dir, new OrtNativeDir.Identity("?", uid),
                (d, perms) -> { })).isTrue();
        // ...and ensure() agrees all the way through extraction and reuse.
        var who = new OrtNativeDir.Identity("?", uid);
        assertThat(OrtNativeDir.ensure(dir, OS, ARCH, small(), who, d -> { }, OrtNativeDir.LOCK_TIMEOUT).outcome())
                .isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(OrtNativeDir.ensure(dir, OS, ARCH, small(), who, d -> { }, OrtNativeDir.LOCK_TIMEOUT).outcome())
                .isEqualTo(OrtNativeDir.Outcome.REUSED);
    }

    @Test
    void aGroupWritableRootIsTightenedAndRecheckedByUidWhenTheNameMatchesNothing(@TempDir Path root)
            throws Exception {
        assumePosix(root);
        int uid = uidOfOrSkip(root);
        Files.setPosixFilePermissions(root, PosixFilePermissions.fromString("rwxrwxr-x"));
        var who = new OrtNativeDir.Identity("?", uid);
        var result = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Result>();

        List<String> warnings = warnings(() -> result.set(
                OrtNativeDir.ensure(root, OS, ARCH, small(), who, d -> { }, OrtNativeDir.LOCK_TIMEOUT)));

        assertThat(result.get().outcome()).as("the post-tighten re-check compares uids too, so it passes")
                .isEqualTo(OrtNativeDir.Outcome.EXTRACTED);
        assertThat(warnings).anySatisfy(w -> assertThat(w).contains("event=ort_native_dir_tightened"));
        assertThat(warnings).noneSatisfy(w -> assertThat(w).contains("ort_native_dir_untrusted"));
        assertThat(mode(root)).isEqualTo("rwxr-xr-x");
    }

    @Test
    void theSameNameWithADifferentUidIsAForeignOwnerAndSaysBothUids(@TempDir Path dir) throws Exception {
        assumePosix(dir);
        int uid = uidOfOrSkip(dir);
        String me = System.getProperty("user.name");
        var other = new OrtNativeDir.Identity(me, uid + 1);

        List<String> warnings = warnings(() ->
                assertThat(OrtNativeDir.trustedBy(dir, other, (d, perms) -> { })).isFalse());

        assertThat(warnings).anySatisfy(w -> assertThat(w).contains("reason=foreign_owner")
                .contains("owner_uid=" + uid).contains("expected_uid=" + (uid + 1)));
        assertThat(OrtNativeDir.ensure(dir, OS, ARCH, small(), other, d -> { }, OrtNativeDir.LOCK_TIMEOUT).outcome())
                .isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        assertThat(names(dir)).as("nothing extracted into a root another uid owns").isEmpty();
    }

    @Test
    void aVersionDirectoryIsJudgedByUidTooAndItsRefusalNamesBothUids(@TempDir Path dir) throws Exception {
        assumePosix(dir);
        int uid = uidOfOrSkip(dir);
        String me = System.getProperty("user.name");
        assertThat(OrtNativeDir.versionDirTrust(dir, new OrtNativeDir.Identity("?", uid), false))
                .as("uid matches, the name does not").isEqualTo(OrtNativeDir.Trust.TRUSTED);
        var trust = new java.util.concurrent.atomic.AtomicReference<OrtNativeDir.Trust>();
        List<String> warnings = warnings(() -> trust.set(
                OrtNativeDir.versionDirTrust(dir, new OrtNativeDir.Identity(me, uid + 1), true)));
        assertThat(trust.get()).as("the name matches, the uid does not").isEqualTo(OrtNativeDir.Trust.UNTRUSTED);
        assertThat(warnings).anySatisfy(w -> assertThat(w).contains("reason=foreign_owner")
                .contains("owner_uid=" + uid).contains("expected_uid=" + (uid + 1)));
    }

    @Test
    void withNoUidTheOwnerNameIsComparedAsBefore(@TempDir Path dir) throws Exception {
        assumePosix(dir);
        String me = System.getProperty("user.name");
        assertThat(OrtNativeDir.trustedBy(dir, OrtNativeDir.Identity.named(me), (d, perms) -> { })).isTrue();
        assertThat(OrtNativeDir.trustedBy(dir, OrtNativeDir.Identity.named("not-the-owner"), (d, perms) -> { }))
                .isFalse();
    }

    /** The WARN messages {@link OrtNativeDir} logs while {@code body} runs. */
    private static List<String> warnings(ThrowingRunnable body) throws Exception {
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(OrtNativeDir.class);
        var appender = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        appender.start();
        logger.addAppender(appender);
        try {
            body.run();
        } finally {
            logger.detachAppender(appender);
            appender.stop();
        }
        return appender.list.stream()
                .filter(e -> e.getLevel() == ch.qos.logback.classic.Level.WARN)
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage).toList();
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    @Test
    void aFinalDirectoryThatIsASymlinkToAWritableDirectoryIsNeitherLoadedNorTightened(@TempDir Path root)
            throws Exception {
        assumePosix(root);
        Path dir = OrtNativeDir.ensure(root, OS, ARCH, small()).dir();
        Path moved = Files.move(dir, root.resolve("elsewhere"));
        Files.setPosixFilePermissions(moved, PosixFilePermissions.fromString("rwxrwxrwx"));
        Files.createSymbolicLink(dir, moved);

        OrtNativeDir.Result r = OrtNativeDir.ensure(root, OS, ARCH, small());

        assertThat(r.outcome()).isEqualTo(OrtNativeDir.Outcome.UNAVAILABLE);
        assertThat(mode(moved)).as("never chmod through a link").isEqualTo("rwxrwxrwx");
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
    void mainSetsThePathOnlyWhenALocalOnnxModelIsPresentBeforeAnyOnnxMemberIsBuilt() throws Exception {
        String main = withoutComments(Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java")));
        int gate = main.indexOf("OrtInitGate.process().installSignalHandlers();");
        int apply = main.indexOf("OrtNativeDir.applyAtBoot();");
        // RDR-210 P2.4 (nexus-un7vj): the condition is a BootPlan answer, "any local ONNX model file is present"
        // (bge OR cross-encoder), decided by value in BootPlanTest. The Voyage-only (cloud) shape has no such
        // file, so it never enters the guarded block: it extracts nothing and creates no ONNX session. Keyed on
        // files present, not on what will be constructed, because the cross-encoder is built lazily AFTER this.
        int probe = main.indexOf("LocalModelProbe.probe(");
        int decide = main.indexOf("BootPlan.decide(");
        int guard = main.lastIndexOf("if (plan.loadsOrtNativeDir()) {", apply);
        int local = main.indexOf("OnnxModelPaths.resolved()");
        int firstEmbedder = main.indexOf("new Bge768Embedder()");
        int crossEncoder = main.indexOf("CrossEncoderReranker::new");
        int firstRouter = main.indexOf("new EmbedderRouter(");
        assertThat(gate).isPositive();
        assertThat(apply).as("Main calls OrtNativeDir.applyAtBoot() (not in a comment)").isPositive();
        assertThat(main.indexOf("OrtNativeDir.applyAtBoot();", apply + 1)).as("exactly one call").isEqualTo(-1);
        assertThat(probe).as("Main probes presence").isPositive();
        assertThat(decide).as("and decides the boot from it").isGreaterThan(probe);
        assertThat(guard).as("applyAtBoot sits inside the files-present guard").isPositive();
        assertThat(main).as("the guard is the plan's answer, not the old bge-only condition")
                .doesNotContain("if (presence.bgePresent()) {");
        assertThat(local).isPositive();
        assertThat(firstEmbedder).isPositive();
        assertThat(crossEncoder).isPositive();
        assertThat(firstRouter).isPositive();
        assertThat(apply).as("after the signal gate is installed, which Main does first thing")
                .isGreaterThan(gate);
        assertThat(guard).as("the guard follows the plan").isGreaterThan(decide);
        assertThat(main.substring(guard, apply))
                .as("nothing between the guard and the call opens another block or builds a router")
                .doesNotContain("new EmbedderRouter(")
                .doesNotContain("} else {");
        assertThat(apply).as("with the model-root resolution").isGreaterThan(local);
        assertThat(apply).as("before ONNX Runtime is first touched, or the property is read too late")
                .isLessThan(firstEmbedder);
        assertThat(apply).as("and before the cross-encoder can lazily load it").isLessThan(crossEncoder);
        assertThat(apply).as("before any router is built from an ONNX member")
                .isLessThan(firstRouter);
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
