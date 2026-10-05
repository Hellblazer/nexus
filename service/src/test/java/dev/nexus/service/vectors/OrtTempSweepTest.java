// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledOnOs;
import org.junit.jupiter.api.condition.OS;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.file.attribute.FileTime;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.function.Predicate;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f9bgu.11 -- onnxruntime-java creates {@code <tmp>/onnxruntime-java<random>}
 * on every start ({@code OnnxRuntime.init}, ORT 1.20.0, unconditionally) and, on
 * Windows, cannot delete it at exit because the loaded DLLs are locked. The sweep
 * removes the dead ones at the next boot.
 *
 * <p>Every test runs on every platform: the OS is a parameter, and the Windows
 * file lock (a loaded DLL refuses deletion) is a {@link OrtTempSweep.Deleter}
 * that throws for the locked paths. Each removal assertion first proves the
 * directories existed, so none of them passes on an empty temp root.
 */
class OrtTempSweepTest {

    private static final Instant NOW = Instant.parse("2026-10-05T12:00:00Z");
    private static final Clock CLOCK = Clock.fixed(NOW, ZoneOffset.UTC);
    private static final FileTime OLD = FileTime.from(NOW.minusSeconds(3600));

    @TempDir
    Path tmp;

    /** A directory shaped like ORT's extraction, with every mtime set to {@code mtime}. */
    private Path ortDir(String name, FileTime mtime) throws IOException {
        Path dir = Files.createDirectory(tmp.resolve(name));
        for (String f : List.of("onnxruntime_providers_shared.dll", "onnxruntime.dll",
                "onnxruntime4j_jni.dll")) {
            Path p = Files.writeString(dir.resolve(f), "x");
            Files.setLastModifiedTime(p, mtime);
        }
        Files.setLastModifiedTime(dir, mtime);
        return dir;
    }

    private static long count(Path root) throws IOException {
        try (Stream<Path> s = Files.list(root)) {
            return s.count();
        }
    }

    private OrtTempSweep.Result sweepWindows() {
        return OrtTempSweep.sweep(tmp, true, CLOCK, Files::delete);
    }

    @Test
    void removesDeadDirectoriesOnWindows() throws IOException {
        Path a = ortDir("onnxruntime-java1111", OLD);
        Path b = ortDir("onnxruntime-java2222", OLD);
        Path c = ortDir("onnxruntime-java3333", OLD);
        assertThat(List.of(a, b, c)).allSatisfy(d -> assertThat(d).isDirectory());

        var r = sweepWindows();

        assertThat(r.examined()).isEqualTo(3);
        assertThat(r.removed()).isEqualTo(3);
        assertThat(count(tmp)).isZero();
    }

    @Test
    void doesNothingOffWindows() throws IOException {
        Path a = ortDir("onnxruntime-java1111", OLD);
        assertThat(a).isDirectory();

        var r = OrtTempSweep.sweep(tmp, false, CLOCK, Files::delete);

        assertThat(r).isEqualTo(OrtTempSweep.Result.NONE);
        assertThat(a).isDirectory();
        assertThat(a.resolve("onnxruntime.dll")).exists();
    }

    @Test
    void leavesUnrelatedEntriesAlone() throws IOException {
        // Every entry is OLD against the fixed clock, so the age guard cannot protect it: only the
        // name filter (the onnxruntime-java* glob) stands between these and deletion.
        Path dead = ortDir("onnxruntime-java1111", OLD);
        Path other = Files.createDirectory(tmp.resolve("some-other-dir"));
        Path otherFile = Files.writeString(other.resolve("f"), "keep");
        Path file = Files.writeString(tmp.resolve("onnxruntime-java-notes.txt"), "keep");
        Path near = Files.createDirectory(tmp.resolve("onnxruntime-jav"));
        Path nearFile = Files.writeString(near.resolve("g"), "keep");
        for (Path p : List.of(otherFile, other, file, nearFile, near)) {
            Files.setLastModifiedTime(p, OLD);
        }
        assertThat(dead).isDirectory();

        var r = sweepWindows();

        assertThat(r.examined()).as("only names starting with onnxruntime-java are examined").isEqualTo(2);
        assertThat(r.removed()).isEqualTo(1);
        assertThat(r.foreign()).as("the plain file named like ORT's directory is not ours").isEqualTo(1);
        assertThat(dead).doesNotExist();
        assertThat(otherFile).exists();
        assertThat(file).exists();
        assertThat(near).isDirectory();
        assertThat(nearFile).exists();
    }

    @Test
    void aDirectoryWhoseLoadedLibraryRefusesDeletionIsLiveAndUntouched() throws IOException {
        Path live = ortDir("onnxruntime-java1111", OLD);
        Path dead = ortDir("onnxruntime-java2222", OLD);
        // Windows: a mapped DLL cannot be deleted. Only the live dir's loaded libs refuse.
        OrtTempSweep.Deleter lock = p -> {
            if (p.startsWith(live) && p.getFileName().toString().equals("onnxruntime.dll")) {
                throw new IOException("The process cannot access the file (locked)");
            }
            Files.delete(p);
        };

        var r = OrtTempSweep.sweep(tmp, true, CLOCK, lock);

        assertThat(r.examined()).isEqualTo(2);
        assertThat(r.live()).isEqualTo(1);
        assertThat(r.removed()).isEqualTo(1);
        assertThat(dead).doesNotExist();
        // The live dir keeps EVERY file, including the unloaded providers_shared one:
        // the loaded libraries are tried first, so a refusal stops before anything goes.
        assertThat(live.resolve("onnxruntime.dll")).exists();
        assertThat(live.resolve("onnxruntime4j_jni.dll")).exists();
        assertThat(live.resolve("onnxruntime_providers_shared.dll")).exists();
    }

    @Test
    void aCompleteDirectoryTouchedWithinTheMinimumAgeIsLeftForItsStarter() throws IOException {
        Path fresh = ortDir("onnxruntime-java1111", FileTime.from(NOW.minusMillis(100)));
        Path future = ortDir("onnxruntime-java2222", FileTime.from(NOW.plusSeconds(60)));
        // 600 ms old: a restart within a second of the last stop must still sweep it.
        Path justStopped = ortDir("onnxruntime-java3333", FileTime.from(NOW.minusMillis(600)));
        Path old = ortDir("onnxruntime-java4444", OLD);
        assertThat(List.of(fresh, future, justStopped, old))
                .allSatisfy(d -> assertThat(d).isDirectory());

        var r = sweepWindows();

        assertThat(r.tooNew()).isEqualTo(2);
        assertThat(r.removed()).isEqualTo(2);
        assertThat(fresh).isDirectory();
        assertThat(future).isDirectory();
        assertThat(justStopped).doesNotExist();
        assertThat(old).doesNotExist();
    }

    @Test
    void aDirectoryMissingALoadedLibraryIsMidExtractionUntilItIsThirtySecondsOld() throws IOException {
        Path filling = ortDir("onnxruntime-java1111", FileTime.from(NOW.minusSeconds(5)));
        Files.delete(filling.resolve("onnxruntime.dll"));
        Files.delete(filling.resolve("onnxruntime4j_jni.dll"));
        Files.setLastModifiedTime(filling, FileTime.from(NOW.minusSeconds(5)));
        Path crashed = ortDir("onnxruntime-java2222", FileTime.from(NOW.minusSeconds(60)));
        Files.delete(crashed.resolve("onnxruntime4j_jni.dll"));
        Files.setLastModifiedTime(crashed, FileTime.from(NOW.minusSeconds(60)));
        Path empty = Files.createDirectory(tmp.resolve("onnxruntime-java3333"));
        Files.setLastModifiedTime(empty, FileTime.from(NOW.minusMillis(50)));
        assertThat(List.of(filling, crashed, empty)).allSatisfy(d -> assertThat(d).isDirectory());

        var r = sweepWindows();

        assertThat(r.tooNew()).isEqualTo(2);
        assertThat(r.removed()).isEqualTo(1);
        assertThat(filling.resolve("onnxruntime_providers_shared.dll")).exists();
        assertThat(empty).isDirectory();
        assertThat(crashed).doesNotExist();
    }

    @Test
    void anOldDirectoryWithOneFreshFileIsStillTooNew() throws IOException {
        Path d = ortDir("onnxruntime-java1111", OLD);
        Files.setLastModifiedTime(d.resolve("onnxruntime_providers_shared.dll"),
                FileTime.from(NOW.minusMillis(100)));

        var r = sweepWindows();

        assertThat(r.tooNew()).isEqualTo(1);
        assertThat(d).isDirectory();
    }

    @Test
    void theAgeBoundIsStrictAtExactlyTheMinimumAge() throws IOException {
        // Not strictly older than the bound is too new: at exactly the bound a starter may still be
        // mid-extract; one millisecond past it the directory is sweepable. Both bounds.
        Path completeAt = ortDir("onnxruntime-java1111",
                FileTime.from(NOW.minus(OrtTempSweep.COMPLETE_MIN_AGE)));
        Path completePast = ortDir("onnxruntime-java2222",
                FileTime.from(NOW.minus(OrtTempSweep.COMPLETE_MIN_AGE).minusMillis(1)));
        Path incompleteAt = ortDir("onnxruntime-java3333",
                FileTime.from(NOW.minus(OrtTempSweep.INCOMPLETE_MIN_AGE)));
        Files.delete(incompleteAt.resolve("onnxruntime.dll"));
        Files.setLastModifiedTime(incompleteAt, FileTime.from(NOW.minus(OrtTempSweep.INCOMPLETE_MIN_AGE)));
        Path incompletePast = ortDir("onnxruntime-java4444",
                FileTime.from(NOW.minus(OrtTempSweep.INCOMPLETE_MIN_AGE).minusMillis(1)));
        Files.delete(incompletePast.resolve("onnxruntime.dll"));
        Files.setLastModifiedTime(incompletePast,
                FileTime.from(NOW.minus(OrtTempSweep.INCOMPLETE_MIN_AGE).minusMillis(1)));
        assertThat(List.of(completeAt, completePast, incompleteAt, incompletePast))
                .allSatisfy(d -> assertThat(d).isDirectory());

        var r = sweepWindows();

        assertThat(r.tooNew()).isEqualTo(2);
        assertThat(r.removed()).isEqualTo(2);
        assertThat(completeAt).isDirectory();
        assertThat(incompleteAt).isDirectory();
        assertThat(completePast).doesNotExist();
        assertThat(incompletePast).doesNotExist();
    }

    // ── the boot path (sweepAtBoot and Main's call) ───────────────────────────

    @Test
    void theBootPathOnWindowsSweepsTheGivenTempDirectory() throws IOException {
        Path a = ortDir("onnxruntime-java1111", OLD);
        Path b = ortDir("onnxruntime-java2222", OLD);
        assertThat(List.of(a, b)).allSatisfy(d -> assertThat(d).isDirectory());

        var r = OrtTempSweep.sweepAtBoot(tmp.toString(), "Windows 11", CLOCK);

        assertThat(r.examined()).as("the boot path examined the directories it was pointed at").isEqualTo(2);
        assertThat(r.removed()).isEqualTo(2);
        assertThat(a).doesNotExist();
        assertThat(b).doesNotExist();
    }

    @Test
    void theBootPathOffWindowsDoesNothing() throws IOException {
        Path a = ortDir("onnxruntime-java1111", OLD);
        assertThat(a).isDirectory();

        assertThat(OrtTempSweep.sweepAtBoot(tmp.toString(), "Mac OS X", CLOCK))
                .isEqualTo(OrtTempSweep.Result.NONE);
        assertThat(OrtTempSweep.sweepAtBoot(tmp.toString(), null, CLOCK)).isEqualTo(OrtTempSweep.Result.NONE);
        assertThat(a).isDirectory();
    }

    @Test
    void theBootPathNeverThrowsOnAnUnusableTempDirectory() {
        assertThat(OrtTempSweep.sweepAtBoot(null, "Windows 11", CLOCK)).isEqualTo(OrtTempSweep.Result.NONE);
        assertThat(OrtTempSweep.sweepAtBoot("  ", "Windows 11", CLOCK)).isEqualTo(OrtTempSweep.Result.NONE);
        assertThat(OrtTempSweep.sweepAtBoot("bad\0path", "Windows 11", CLOCK))
                .isEqualTo(OrtTempSweep.Result.NONE);
    }

    @Test
    void theNoArgumentBootEntryReadsJavaIoTmpdirAndOsName() throws IOException {
        // The production entry point reads two system properties; drive it with both set. Forks are
        // sequential within a JVM (surefire forkCount, no same-JVM parallelism), and both are restored.
        Path a = ortDir("onnxruntime-java1111", FileTime.from(Instant.now().minusSeconds(3600)));
        assertThat(a).isDirectory();
        String tmpProp = System.getProperty("java.io.tmpdir");
        String osProp = System.getProperty("os.name");
        try {
            System.setProperty("java.io.tmpdir", tmp.toString());
            System.setProperty("os.name", "Windows 11");
            OrtTempSweep.sweepAtBoot();
        } finally {
            System.setProperty("java.io.tmpdir", tmpProp);
            System.setProperty("os.name", osProp);
        }
        assertThat(a).as("sweepAtBoot() swept java.io.tmpdir as Windows").doesNotExist();
    }

    @Test
    void mainSweepsAtBootAfterTheSignalGateIsInstalled() throws IOException {
        String main = stripComments(Files.readString(
                Path.of("src", "main", "java", "dev", "nexus", "service", "Main.java")));
        int gate = main.indexOf("OrtInitGate.process().installSignalHandlers()");
        int sweep = main.indexOf("OrtTempSweep.sweepAtBoot()");
        assertThat(gate).as("Main installs the OrtInitGate handlers").isNotEqualTo(-1);
        assertThat(sweep).as("Main calls OrtTempSweep.sweepAtBoot()").isNotEqualTo(-1);
        assertThat(sweep).as("the sweep runs after the signal gate is installed").isGreaterThan(gate);
        assertThat(main.indexOf("OrtTempSweep.sweepAtBoot()", sweep + 1))
                .as("and is called once").isEqualTo(-1);
    }

    private static String stripComments(String source) {
        return source.replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
    }

    @Test
    void aDirectoryHoldingAnythingButFilesIsNotOurs() throws IOException {
        Path d = ortDir("onnxruntime-java1111", OLD);
        Files.createDirectory(d.resolve("nested"));
        Files.setLastModifiedTime(d, OLD);

        var r = sweepWindows();

        assertThat(r.foreign()).isEqualTo(1);
        assertThat(r.removed()).isZero();
        assertThat(d.resolve("onnxruntime.dll")).exists();
    }

    @Test
    void aLateDeletionFailureIsCountedAndNeverThrown() throws IOException {
        Path d = ortDir("onnxruntime-java1111", OLD);
        OrtTempSweep.Deleter stubborn = p -> {
            if (p.getFileName().toString().equals("onnxruntime_providers_shared.dll")) {
                throw new IOException("denied");
            }
            Files.delete(p);
        };

        var r = OrtTempSweep.sweep(tmp, true, CLOCK, stubborn);

        assertThat(r.failed()).isEqualTo(1);
        assertThat(r.removed()).isZero();
        assertThat(d).isDirectory();
    }

    @Test
    void aMissingTempRootIsNotAnError() {
        var r = OrtTempSweep.sweep(tmp.resolve("absent"), true, CLOCK, Files::delete);
        assertThat(r).isEqualTo(OrtTempSweep.Result.NONE);
    }

    @Test
    void fiveStartsLeaveAtMostOneDirectory() throws IOException {
        // Each "start" sweeps, then ORT creates its own dir; each "stop" leaves it behind
        // (the loaded DLLs were locked at exit, so deleteOnExit lost).
        for (int start = 1; start <= 5; start++) {
            sweepWindows();
            ortDir("onnxruntime-java" + start + start + start, OLD);
        }
        assertThat(count(tmp)).isEqualTo(1);
    }

    @Test
    void fiveStartsBesideALiveEngineLeaveItsDirectoryAndOne() throws IOException {
        Path peer = ortDir("onnxruntime-javaPEER", OLD);
        OrtTempSweep.Deleter lock = p -> {
            if (p.startsWith(peer) && p.getFileName().toString().startsWith("onnxruntime")
                    && !p.getFileName().toString().contains("providers")) {
                throw new IOException("locked");
            }
            Files.delete(p);
        };
        for (int start = 1; start <= 5; start++) {
            OrtTempSweep.sweep(tmp, true, CLOCK, lock);
            ortDir("onnxruntime-java" + start + start + start, OLD);
        }
        assertThat(count(tmp)).isEqualTo(2);
        assertThat(peer.resolve("onnxruntime.dll")).exists();
    }

    // ── reparse points (nexus-f9bgu.27, review m2) ──────────────────────────────

    /**
     * Real attributes, except that paths selected by {@code other} read as a Windows junction does under
     * NOFOLLOW_LINKS: {@code isOther()} true, not a symbolic link, not a regular file.
     */
    private static OrtTempSweep.AttrReader reparsePoints(Predicate<Path> other) {
        return p -> {
            BasicFileAttributes real = Files.readAttributes(p, BasicFileAttributes.class,
                    LinkOption.NOFOLLOW_LINKS);
            if (!other.test(p)) {
                return real;
            }
            return new BasicFileAttributes() {
                public FileTime lastModifiedTime() { return real.lastModifiedTime(); }
                public FileTime lastAccessTime() { return real.lastAccessTime(); }
                public FileTime creationTime() { return real.creationTime(); }
                public boolean isRegularFile() { return false; }
                public boolean isDirectory() { return real.isDirectory(); }
                public boolean isSymbolicLink() { return false; }
                public boolean isOther() { return true; }
                public long size() { return real.size(); }
                public Object fileKey() { return real.fileKey(); }
            };
        };
    }

    @Test
    void aCandidateDirectoryThatIsAReparsePointIsForeignAndItsFilesSurvive() throws IOException {
        Path junction = ortDir("onnxruntime-java1111", OLD);
        Path dead = ortDir("onnxruntime-java2222", OLD);
        assertThat(List.of(junction, dead)).allSatisfy(d -> assertThat(d).isDirectory());

        var r = OrtTempSweep.sweep(tmp, true, CLOCK, Files::delete,
                reparsePoints(p -> p.equals(junction)));

        assertThat(r.examined()).isEqualTo(2);
        assertThat(r.foreign()).isEqualTo(1);
        assertThat(r.removed()).isEqualTo(1);
        assertThat(dead).doesNotExist();
        assertThat(junction.resolve("onnxruntime.dll")).exists();
        assertThat(junction.resolve("onnxruntime4j_jni.dll")).exists();
        assertThat(junction.resolve("onnxruntime_providers_shared.dll")).exists();
    }

    @Test
    void anEntryThatIsAReparsePointMakesTheDirectoryForeignBeforeAnythingIsDeleted() throws IOException {
        Path d = ortDir("onnxruntime-java1111", OLD);
        Path odd = d.resolve("onnxruntime_providers_shared.dll");
        List<Path> deleted = new ArrayList<>();

        var r = OrtTempSweep.sweep(tmp, true, CLOCK, p -> {
            deleted.add(p);
            Files.delete(p);
        }, reparsePoints(odd::equals));

        assertThat(r.foreign()).isEqualTo(1);
        assertThat(r.removed()).isZero();
        assertThat(deleted).as("nothing is deleted from a directory holding a reparse point").isEmpty();
        assertThat(d.resolve("onnxruntime.dll")).exists();
    }

    /**
     * The real thing, where the OS has junctions: {@code mklink /J}, which the JDK reads as a directory
     * that is "other" and not a symbolic link. Skipped on other hosts; the injected-attribute tests above
     * carry the logic everywhere. Run on native Windows for nexus-f9bgu.27.
     */
    @Test
    @EnabledOnOs(OS.WINDOWS)
    void aRealWindowsJunctionNamedLikeOrtsDirectoryIsLeftAlone() throws Exception {
        Path target = Files.createDirectory(tmp.resolve("junction-target"));
        for (String f : List.of("onnxruntime_providers_shared.dll", "onnxruntime.dll",
                "onnxruntime4j_jni.dll")) {
            Path p = Files.writeString(target.resolve(f), "x");
            Files.setLastModifiedTime(p, OLD);
        }
        Files.setLastModifiedTime(target, OLD);
        Path root = Files.createDirectory(tmp.resolve("sweep-root"));
        Path junction = root.resolve("onnxruntime-javaJUNC");
        Process mk = new ProcessBuilder("cmd", "/c", "mklink", "/J", junction.toString(), target.toString())
                .redirectErrorStream(true).start();
        String out = new String(mk.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        assertThat(mk.waitFor()).as("mklink /J: " + out).isZero();
        try {
            BasicFileAttributes attrs = Files.readAttributes(junction, BasicFileAttributes.class,
                    LinkOption.NOFOLLOW_LINKS);
            assertThat(attrs.isSymbolicLink()).as("premise: the JDK does not call a junction a symlink")
                    .isFalse();
            assertThat(attrs.isOther()).as("premise: the JDK reads a junction as 'other'").isTrue();

            // The files are OLD against the fixed clock, so only the reparse check can stop the sweep.
            var r = OrtTempSweep.sweep(root, true, CLOCK, Files::delete);

            assertThat(r.examined()).isEqualTo(1);
            assertThat(r.foreign()).isEqualTo(1);
            assertThat(r.removed()).isZero();
            assertThat(target.resolve("onnxruntime.dll")).exists();
            assertThat(target.resolve("onnxruntime4j_jni.dll")).exists();
            assertThat(target.resolve("onnxruntime_providers_shared.dll")).exists();
        } finally {
            // rmdir on a junction removes the link only, never the target's contents
            new ProcessBuilder("cmd", "/c", "rmdir", junction.toString()).start().waitFor();
        }
    }

    // ── LOADED_LIBS is a property of the pinned ORT jar (RDR-224 critique, Observation 1) ─────

    private static final Path LISTING = Path.of("..", "tests", "fixtures", "native_jar_listings.txt");

    /** Libraries named in {@code loaded} that {@code shippedDlls} does not carry. */
    private static List<String> missingFrom(List<String> loaded, Set<String> shippedDlls) {
        return loaded.stream().filter(n -> !shippedDlls.contains(n)).toList();
    }

    @Test
    void theLoadedLibrariesAreTheWindowsLibrariesOfThePinnedOrtJar() throws Exception {
        List<String> win = new ArrayList<>();
        String version = null;
        boolean inOrt = false;
        for (String line : Files.readAllLines(LISTING)) {
            if (line.startsWith("# jar ")) {
                inOrt = line.contains(" com.microsoft.onnxruntime:onnxruntime:");
                if (inOrt) {
                    version = line.split(" ")[2].split(":")[2];
                }
            } else if (inOrt && line.startsWith("ai/onnxruntime/native/win-x64/")) {
                win.add(line.substring(line.lastIndexOf('/') + 1));
            }
        }
        assertThat(version).as("the committed listing has an onnxruntime section").isNotNull();
        assertThat(win).as("that section has win-x64 entries").isNotEmpty();

        // The listing is only evidence for the version the build pins.
        Path jar = Path.of(ai.onnxruntime.OrtEnvironment.class.getProtectionDomain()
                .getCodeSource().getLocation().toURI());
        assertThat(jar.getFileName().toString())
                .as("listing is for onnxruntime %s; a bump must re-list (scripts/native_jar_listing.py)", version)
                .isEqualTo("onnxruntime-" + version + ".jar");

        // The committed listing is only a fixture; the jar on this test's own classpath is the evidence.
        // Its win-x64 entries must be exactly the listing's, and carry the libraries the sweep keys on.
        Set<String> inJar = new TreeSet<>();
        try (java.util.zip.ZipFile z = new java.util.zip.ZipFile(jar.toFile())) {
            z.stream().map(java.util.zip.ZipEntry::getName)
                    .filter(n -> n.startsWith("ai/onnxruntime/native/win-x64/") && !n.endsWith("/"))
                    .forEach(n -> inJar.add(n.substring(n.lastIndexOf('/') + 1)));
        }
        assertThat(inJar).as("win-x64 entries of the real %s", jar.getFileName())
                .containsExactlyInAnyOrderElementsOf(win);
        assertThat(inJar).as("the real jar carries every library the sweep treats as loaded")
                .containsAll(OrtTempSweep.LOADED_LIBS);

        // The sweep's safety: a live peer's directory refuses deletion at the first of these, because
        // ORT keeps them mapped. A bump that renames one makes a live peer's directory look incomplete
        // for 30 s and then deletes an unlocked file in it.
        Set<String> dlls = new TreeSet<>();
        win.stream().filter(n -> n.endsWith(".dll")).forEach(dlls::add);
        assertThat(OrtTempSweep.LOADED_LIBS).isNotEmpty();
        assertThat(missingFrom(OrtTempSweep.LOADED_LIBS, dlls))
                .as("OrtTempSweep.LOADED_LIBS names DLLs absent from onnxruntime %s win-x64: %s", version, dlls)
                .isEmpty();
    }

    @Test
    void theMembershipCheckSeesARenamedLibrary() {
        Set<String> shipped = Set.of("onnxruntime.dll", "onnxruntime4j_jni_v2.dll");
        assertThat(missingFrom(List.of("onnxruntime.dll", "onnxruntime4j_jni.dll"), shipped))
                .containsExactly("onnxruntime4j_jni.dll");
    }
}
