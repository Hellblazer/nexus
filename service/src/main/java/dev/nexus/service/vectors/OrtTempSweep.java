// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * nexus-f9bgu.11 -- removes the {@code onnxruntime-java<random>} directories that
 * earlier engine runs left in the temp directory on Windows.
 *
 * <p><b>Mechanism.</b> {@code OnnxRuntime.init()} (onnxruntime-java 1.20.0) calls
 * {@code Files.createTempDirectory("onnxruntime-java")} on every start before it
 * reads any property, then extracts {@code onnxruntime_providers_shared.dll} into
 * it, then extracts and loads {@code onnxruntime.dll} and
 * {@code onnxruntime4j_jni.dll}. Its only cleanup is {@code File.deleteOnExit()}.
 * On Windows a loaded DLL is mapped and cannot be deleted, so that cleanup fails
 * and the directory stays: five starts left five directories.
 *
 * <p><b>Why a sweep and not a fixed path.</b> {@code onnxruntime.native.path} does
 * not avoid the directory: the loader creates it, and extracts the providers
 * library into it, before and regardless of that property. Redirecting
 * {@code java.io.tmpdir} relocates one directory per start without removing it,
 * changes the temp location for every other user of it in the process, and
 * {@code TempFileHelper} reads the property once at class initialisation. The
 * directory has to be removed, and the first moment its DLLs are unlocked is the
 * next process. DJL's tokenizer library is not affected: it extracts once into a
 * stable cache directory ({@code ~/.djl.ai/tokenizers/<version>-...}) and reuses it.
 *
 * <p><b>Safety.</b> The sweep runs on Windows only; elsewhere {@code deleteOnExit}
 * works and there is nothing to sweep. A directory is removed only when
 * <ul>
 *   <li>its name starts with {@code onnxruntime-java} and it is a real directory
 *       holding only regular files (a symbolic link, a junction or any other
 *       reparse point, or anything nested is not ORT's and is left alone);</li>
 *   <li>it is not one an engine may be filling right now. A directory missing a
 *       loaded library is mid-extraction (or a crashed one) and is left for
 *       {@link #INCOMPLETE_MIN_AGE}; a complete one is left only for
 *       {@link #COMPLETE_MIN_AGE}, the gap between the JNI DLL being written and
 *       loaded. Age is the newest modification time of the directory or any file in
 *       it. The complete case is short on purpose: an engine restarted within a
 *       second of the last stop must still find that stop's directory sweepable,
 *       or five quick restarts leave two directories instead of one;</li>
 *   <li>its loaded libraries delete. They are tried first. A live engine's
 *       {@code onnxruntime.dll} and JNI DLL are mapped, so the delete is refused and
 *       the whole directory is left untouched, including the providers library
 *       ORT never loads on the CPU path.</li>
 * </ul>
 * Everything is best effort: no failure here reaches {@code main}.
 */
public final class OrtTempSweep {

    private static final Logger log = LoggerFactory.getLogger(OrtTempSweep.class);

    /** Prefix onnxruntime-java gives its extraction directory. */
    public static final String DIR_PREFIX = "onnxruntime-java";

    /**
     * A directory holding every loaded library, touched more recently than this, may
     * belong to an engine between writing its JNI DLL and loading it.
     */
    public static final Duration COMPLETE_MIN_AGE = Duration.ofMillis(250);

    /**
     * A directory missing a loaded library, touched more recently than this, may
     * belong to an engine part-way through extracting. Older than this it is a crashed
     * start's leftover.
     */
    public static final Duration INCOMPLETE_MIN_AGE = Duration.ofSeconds(30);

    /**
     * The libraries ORT loads; a live engine's copies are mapped and cannot be deleted. This is the
     * sweep's safety property, true for onnxruntime-java 1.20.0 only: {@code OrtTempSweepTest} ties
     * these names to the win-x64 entries of the committed jar listing, so a bump that renames one
     * fails there.
     */
    static final List<String> LOADED_LIBS =
            List.of("onnxruntime.dll", "onnxruntime4j_jni.dll");

    /**
     * Reads a path's attributes WITHOUT following links, injectable so a test can present a Windows
     * junction (a directory the JDK reports as {@code isOther()} and not as a symbolic link) on any host.
     */
    @FunctionalInterface
    public interface AttrReader {
        BasicFileAttributes read(Path path) throws IOException;
    }

    private static BasicFileAttributes readNoFollow(Path path) throws IOException {
        return Files.readAttributes(path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    }

    /** File deletion, injectable so a test can stand in for a Windows file lock. */
    @FunctionalInterface
    public interface Deleter {
        void delete(Path path) throws IOException;
    }

    /**
     * Outcome counts. {@code examined} counts every name-matching entry;
     * {@code removed + live + tooNew + foreign + failed <= examined}.
     */
    public record Result(int examined, int removed, int live, int tooNew, int foreign, int failed) {
        public static final Result NONE = new Result(0, 0, 0, 0, 0, 0);
    }

    private OrtTempSweep() {}

    /**
     * Production entry point: sweeps {@code java.io.tmpdir} when running on Windows.
     * Never throws.
     */
    public static void sweepAtBoot() {
        sweepAtBoot(System.getProperty("java.io.tmpdir"), System.getProperty("os.name"), Clock.systemUTC());
    }

    /**
     * {@link #sweepAtBoot()} with the temp directory, OS name and clock as parameters, so a test can drive
     * the boot path as Windows over a real directory on any host. Never throws.
     */
    static Result sweepAtBoot(String tmpDir, String osName, Clock clock) {
        try {
            if (tmpDir == null || tmpDir.isBlank()) {
                return Result.NONE;
            }
            Result r = sweep(Paths.get(tmpDir), WindowsHost.isWindows(osName), clock, Files::delete);
            if (r.examined() > 0) {
                log.info("event=ort_temp_sweep examined={} removed={} live={} too_new={} foreign={} failed={}",
                        r.examined(), r.removed(), r.live(), r.tooNew(), r.foreign(), r.failed());
            }
            return r;
        } catch (RuntimeException e) {
            log.warn("event=ort_temp_sweep_error error=\"{}\"", e.toString());
            return Result.NONE;
        }
    }

    /**
     * Sweeps {@code tmpRoot} for dead onnxruntime-java directories.
     *
     * @param windows false makes this a no-op returning {@link Result#NONE}
     */
    public static Result sweep(Path tmpRoot, boolean windows, Clock clock, Deleter deleter) {
        return sweep(tmpRoot, windows, clock, deleter, OrtTempSweep::readNoFollow);
    }

    /** As {@link #sweep(Path, boolean, Clock, Deleter)} with the attribute reader injected. */
    public static Result sweep(Path tmpRoot, boolean windows, Clock clock, Deleter deleter,
                               AttrReader attrs) {
        if (!windows || !Files.isDirectory(tmpRoot)) {
            return Result.NONE;
        }
        int examined = 0;
        int removed = 0;
        int live = 0;
        int tooNew = 0;
        int foreign = 0;
        int failed = 0;
        List<Path> candidates = new ArrayList<>();
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(tmpRoot, DIR_PREFIX + "*")) {
            ds.forEach(candidates::add);
        } catch (IOException | RuntimeException e) {
            log.warn("event=ort_temp_sweep_list_failed root=\"{}\" error=\"{}\"", tmpRoot, e.toString());
            return Result.NONE;
        }
        for (Path dir : candidates) {
            examined++;
            switch (sweepOne(dir, clock, deleter, attrs)) {
                case REMOVED -> removed++;
                case LIVE -> live++;
                case TOO_NEW -> tooNew++;
                case FOREIGN -> foreign++;
                case FAILED -> failed++;
            }
        }
        return new Result(examined, removed, live, tooNew, foreign, failed);
    }

    private enum Outcome { REMOVED, LIVE, TOO_NEW, FOREIGN, FAILED }

    private static Outcome sweepOne(Path dir, Clock clock, Deleter deleter, AttrReader attrs) {
        List<Path> files = new ArrayList<>();
        Instant newest;
        try {
            // A symlink, or any other reparse point (a Windows junction reads as directory + other), is
            // not ORT's directory: following it would delete files somewhere else.
            BasicFileAttributes d = attrs.read(dir);
            if (!d.isDirectory() || d.isSymbolicLink() || d.isOther()) {
                return Outcome.FOREIGN;
            }
            newest = d.lastModifiedTime().toInstant();
            try (DirectoryStream<Path> ds = Files.newDirectoryStream(dir)) {
                for (Path p : ds) {
                    BasicFileAttributes a = attrs.read(p);
                    if (!a.isRegularFile() || a.isSymbolicLink() || a.isOther()) {
                        return Outcome.FOREIGN;
                    }
                    files.add(p);
                    Instant m = a.lastModifiedTime().toInstant();
                    if (m.isAfter(newest)) {
                        newest = m;
                    }
                }
            }
        } catch (IOException | RuntimeException e) {
            log.debug("event=ort_temp_sweep_skip dir=\"{}\" error=\"{}\"", dir, e.toString());
            return Outcome.FAILED;
        }
        // Not strictly older than the bound (a future mtime included) means a starter may be mid-extract.
        boolean complete = files.stream()
                .map(p -> p.getFileName().toString())
                .collect(java.util.stream.Collectors.toSet())
                .containsAll(LOADED_LIBS);
        Duration bound = complete ? COMPLETE_MIN_AGE : INCOMPLETE_MIN_AGE;
        if (!newest.plus(bound).isBefore(clock.instant())) {
            return Outcome.TOO_NEW;
        }
        // Loaded libraries first: a live engine's are mapped, so the first refusal means the
        // directory is in use and nothing has been deleted from it yet.
        files.sort(Comparator.comparingInt(
                (Path p) -> LOADED_LIBS.contains(p.getFileName().toString()) ? 0 : 1)
                .thenComparing(Path::getFileName));
        boolean deletedAny = false;
        for (Path f : files) {
            try {
                deleter.delete(f);
                deletedAny = true;
            } catch (IOException | RuntimeException e) {
                if (!deletedAny) {
                    return Outcome.LIVE;
                }
                log.debug("event=ort_temp_sweep_partial dir=\"{}\" file=\"{}\" error=\"{}\"",
                        dir, f.getFileName(), e.toString());
                return Outcome.FAILED;
            }
        }
        try {
            deleter.delete(dir);
            return Outcome.REMOVED;
        } catch (IOException | RuntimeException e) {
            log.debug("event=ort_temp_sweep_partial dir=\"{}\" error=\"{}\"", dir, e.toString());
            return Outcome.FAILED;
        }
    }
}
