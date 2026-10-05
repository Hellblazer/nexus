// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.FileTime;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
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
        Path dead = ortDir("onnxruntime-java1111", OLD);
        Path other = Files.createDirectory(tmp.resolve("some-other-dir"));
        Path file = Files.writeString(tmp.resolve("onnxruntime-java-notes.txt"), "keep");
        Path near = Files.createDirectory(tmp.resolve("onnxruntime-jav"));
        Files.writeString(other.resolve("f"), "keep");
        assertThat(dead).isDirectory();

        var r = sweepWindows();

        assertThat(r.removed()).isEqualTo(1);
        assertThat(dead).doesNotExist();
        assertThat(other.resolve("f")).exists();
        assertThat(file).exists();
        assertThat(near).isDirectory();
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

    @Test
    void windowsIsRecognisedFromTheOsName() {
        assertThat(OrtTempSweep.isWindows("Windows 11")).isTrue();
        assertThat(OrtTempSweep.isWindows("Windows Server 2022")).isTrue();
        assertThat(OrtTempSweep.isWindows("Mac OS X")).isFalse();
        assertThat(OrtTempSweep.isWindows("Linux")).isFalse();
        assertThat(OrtTempSweep.isWindows(null)).isFalse();
    }
}
