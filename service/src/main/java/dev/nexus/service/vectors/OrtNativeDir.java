// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.channels.Channels;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFileAttributeView;
import java.nio.file.attribute.PosixFileAttributes;
import java.nio.file.attribute.PosixFilePermission;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.function.Function;
import java.util.function.UnaryOperator;

/**
 * nexus-8ool4 -- makes onnxruntime-java load its native libraries from one fixed directory that is
 * extracted once and reused, instead of unpacking about 16 MB into a fresh
 * {@code /tmp/onnxruntime-java<random>} on every JVM start.
 *
 * <p><b>The leak.</b> {@code OnnxRuntime.init()} (onnxruntime-java 1.20.0) extracts
 * {@code libonnxruntime} and {@code libonnxruntime4j_jni} from the jar into a new temp directory on
 * every start, and its only cleanup is {@code File.deleteOnExit()}. A killed engine (a test that
 * SIGKILLs the service, a SIGKILLed daemon) never runs it. labrun reported the leak from chas, where
 * {@code /tmp} is RAM and the directories pile up until its reaper removes them; at about 16 MB a
 * directory, thousands of them are tens of gigabytes.
 *
 * <p><b>The mechanism.</b> {@code OnnxRuntime.load} tries {@code onnxruntime.native.path} before it
 * tries the jar resources: when the property names a directory it {@code System.load}s
 * {@code <dir>/<mapped name>} for both libraries and never extracts them. This class extracts the two
 * libraries (and the shared-providers library when a jar carries one; 1.20.0 does not) into
 * {@code <root>/<ORT version>-<os-arch>} once, then sets the property. The loader still calls
 * {@code Files.createTempDirectory("onnxruntime-java")} unconditionally, before it reads the property
 * (see {@link OrtTempSweep}), so a killed start still leaves a directory in the temp dir; with the
 * property set that directory is EMPTY, because nothing is extracted into it (measured by
 * {@code OrtNativeDirProcessTest}: no file of any size under the temp dir).
 *
 * <p><b>Where.</b> {@code NX_ORT_NATIVE_DIR} if set, else {@code $HOME/.cache/nexus/ort_native}, else
 * {@code user.home} plus the same suffix: the cache tree the engine's ONNX model root and the DJL
 * tokenizer cache already live in. None of the defaults is the temp directory. If that location cannot
 * be created or written, the property is left unset and ORT behaves exactly as before.
 *
 * <p><b>Trust (POSIX).</b> A shared native library is code the engine loads, so the root and the
 * version directory must be owned by the current user and not writable by group or others; otherwise
 * this warns and returns {@link Outcome#UNAVAILABLE} (ORT's old temp-directory path). The stage keeps
 * the mode {@code createTempDirectory} gave it (0700), so a directory one user extracted is not
 * reusable by another: a cache shared across users falls back per user. Windows skips the check.
 *
 * <p><b>Integrity.</b> Each library's size is recorded in {@value #SIZES_FILE} when it is extracted;
 * a directory is complete only when every required library is present at its recorded size, so a
 * truncated library counts as broken. Files and the stage directory are fsynced before the rename
 * where the platform allows.
 *
 * <p><b>Safe under concurrent first starts.</b> Each starter extracts into its own private
 * {@code .stage-*} directory under the root and publishes it with one directory rename onto the
 * final name. The rename is atomic and fails if the target exists, so the final directory is either
 * absent or complete; the loser of a race deletes its stage and uses the winner's directory. Every start, including one
 * that reuses a complete directory, first removes {@code .stage-*} and {@code .broken-*} leftovers of
 * killed starters once they are over {@link #STALE_STAGE_AGE} old.
 *
 * <p><b>A published directory is never destroyed in place.</b> A directory that fails the completeness
 * check is renamed aside to {@code .broken-<random>} and deleted from there, under an in-JVM monitor
 * and a file lock on {@code <root>/.lock}, with the check repeated inside the lock, so a sibling that
 * has just published a whole directory is never moved out from under its loader.
 *
 * <p><b>Operator wins.</b> If {@code onnxruntime.native.path} is already set (a {@code -D} flag, a
 * native-image runtime option), nothing here runs and ORT loads from that directory as it always has.
 *
 * <p><b>Version coupling.</b> {@link #ORT_VERSION} names the directory, so a changed jar can never
 * reuse an older jar's libraries. {@code OrtNativeDirTest} ties it to the pom's
 * {@code onnxruntime.version} and to the jar on the classpath, so a bump that forgets it fails there.
 */
public final class OrtNativeDir {

    private static final Logger log = LoggerFactory.getLogger(OrtNativeDir.class);

    /** The system property onnxruntime-java reads for the directory holding its native libraries. */
    public static final String PROPERTY = "onnxruntime.native.path";

    /** Env var naming the extraction ROOT (the version-and-platform directory goes under it). */
    public static final String ROOT_ENV = "NX_ORT_NATIVE_DIR";

    /** The onnxruntime-java version this extracts; tied to the pom by {@code OrtNativeDirTest}. */
    public static final String ORT_VERSION = "1.20.0";

    /** Path under the home-directory rungs. */
    static final String HOME_SUFFIX = "/.cache/nexus/ort_native";

    /** A {@code .stage-*} directory older than this belongs to a starter that was killed. */
    public static final Duration STALE_STAGE_AGE = Duration.ofMinutes(10);

    private static final String STAGE_PREFIX = ".stage-";
    private static final String BROKEN_PREFIX = ".broken-";

    /** Records each extracted library's size, so a truncated file is told from a whole one. */
    static final String SIZES_FILE = ".sizes";
    private static final String LOCK_FILE = ".lock";

    /** Serialises broken-directory recovery within this JVM (a {@link FileLock} is per-JVM). */
    private static final Object RECOVERY = new Object();

    /** Libraries ORT cannot start without. */
    private static final List<String> REQUIRED = List.of("onnxruntime", "onnxruntime4j_jni");

    /** Extracted when the jar carries it (1.20.0 does not); ORT looks for it beside the main library. */
    private static final List<String> OPTIONAL = List.of("onnxruntime_providers_shared");

    /** What {@link #ensure} did. {@code dir} is null when nothing usable could be prepared. */
    public record Result(Path dir, Outcome outcome) {}

    public enum Outcome { REUSED, EXTRACTED, OPERATOR_SET, UNAVAILABLE }

    /** Opens a classpath resource by absolute name, or returns null when it is absent. */
    @FunctionalInterface
    public interface Resources {
        InputStream open(String name) throws IOException;
    }

    private OrtNativeDir() {}

    /**
     * Production entry point, called from {@code Main} before anything touches ONNX Runtime: points
     * {@value #PROPERTY} at the shared directory. Never throws.
     */
    public static void applyAtBoot() {
        try {
            applyAtBoot(System::getenv, System.getProperty("user.home"),
                    System.getProperty("os.name"), System.getProperty("os.arch"));
        } catch (RuntimeException e) {
            log.warn("event=ort_native_dir_error error=\"{}\"", e.toString());
        }
    }

    /** {@link #applyAtBoot()} with its inputs as parameters; sets the system property on success. */
    static Result applyAtBoot(UnaryOperator<String> env, String userHome, String osName, String osArch) {
        String operator = System.getProperty(PROPERTY);
        if (operator != null && !operator.isBlank()) {
            log.info("event=ort_native_dir source=operator dir=\"{}\"", operator);
            return new Result(Paths.get(operator), Outcome.OPERATOR_SET);
        }
        Path root = resolveRoot(env, userHome);
        Result r = ensure(root, osName, osArch,
                name -> ai.onnxruntime.OrtEnvironment.class.getResourceAsStream(name));
        if (r.dir() != null) {
            System.setProperty(PROPERTY, r.dir().toString());
            log.info("event=ort_native_dir source={} dir=\"{}\"",
                    r.outcome() == Outcome.EXTRACTED ? "extracted" : "reused", r.dir());
        } else {
            log.warn("event=ort_native_dir_unavailable root=\"{}\" note=\"onnxruntime-java will extract "
                    + "into the temp directory on this start\"", root);
        }
        return r;
    }

    /** The extraction root: env override, else {@code $HOME}, else {@code user.home}. */
    static Path resolveRoot(UnaryOperator<String> env, String userHome) {
        String explicit = env.apply(ROOT_ENV);
        if (explicit != null && !explicit.isBlank()) {
            return Paths.get(explicit.trim());
        }
        String home = env.apply("HOME");
        if (home != null && !home.isBlank()) {
            return Paths.get(home.trim() + HOME_SUFFIX);
        }
        return Paths.get(userHome + HOME_SUFFIX);
    }

    /** The {@code os-arch} string onnxruntime-java uses in its resource paths, or null if unsupported. */
    static String osArch(String osName, String osArch) {
        String os = osName == null ? "" : osName.toLowerCase(Locale.ENGLISH);
        String arch = osArch == null ? "" : osArch.toLowerCase(Locale.ENGLISH);
        String detectedOs;
        if (os.contains("mac") || os.contains("darwin")) {
            detectedOs = "osx";
        } else if (os.contains("win")) {
            detectedOs = "win";
        } else if (os.contains("nux")) {
            detectedOs = "linux";
        } else {
            return null;
        }
        String detectedArch;
        if (arch.startsWith("amd64") || arch.startsWith("x86_64")) {
            detectedArch = "x64";
        } else if (arch.startsWith("aarch64")) {
            detectedArch = "aarch64";
        } else {
            return null;
        }
        return detectedOs + '-' + detectedArch;
    }

    /** onnxruntime-java's mapping: {@link System#mapLibraryName} with macOS {@code jnilib} as {@code dylib}. */
    static String mapLibraryName(String library) {
        return System.mapLibraryName(library).replace("jnilib", "dylib");
    }

    /** {@link #ensure(Path, String, String, Resources, String)} for the current user. */
    public static Result ensure(Path root, String osName, String osArchName, Resources resources) {
        return ensure(root, osName, osArchName, resources, System.getProperty("user.name"));
    }

    /**
     * Makes {@code <root>/<version>-<os-arch>} exist and hold the libraries, extracting at most once.
     * Never throws; returns a null {@link Result#dir()} when nothing usable could be prepared or the
     * root or the version directory is not trusted (see the class comment). {@code user} is the name
     * the root and version directory must be owned by (POSIX only; null skips the owner check).
     */
    static Result ensure(Path root, String osName, String osArchName, Resources resources, String user) {
        String platform = osArch(osName, osArchName);
        if (platform == null) {
            return new Result(null, Outcome.UNAVAILABLE);
        }
        Path finalDir = root.resolve(ORT_VERSION + "-" + platform);
        try {
            createRoot(root);
            if (!trusted(root, user)) {
                return new Result(null, Outcome.UNAVAILABLE);
            }
            // Before the reuse return: a stage left by a killed starter is removed by ANY later start.
            sweepStale(root, Instant.now());
            if (Files.exists(finalDir)) {
                if (!trusted(finalDir, user)) {
                    return new Result(null, Outcome.UNAVAILABLE);
                }
                if (isComplete(finalDir)) {
                    return new Result(finalDir, Outcome.REUSED);
                }
            }
            Path stage = Files.createTempDirectory(root, STAGE_PREFIX);
            try {
                if (!extractInto(stage, platform, resources)) {
                    return new Result(null, Outcome.UNAVAILABLE);
                }
                fsyncDirectory(stage);
                IOException failure = publish(stage, finalDir);
                if (failure == null) {
                    fsyncDirectory(root);
                    return new Result(finalDir, Outcome.EXTRACTED);
                }
                // Lost a race (or a broken directory sits there): use the winner's if it is whole.
                if (isComplete(finalDir)) {
                    return new Result(finalDir, Outcome.REUSED);
                }
                return recover(root, stage, finalDir, user, failure);
            } finally {
                deleteTree(stage);
            }
        } catch (IOException | RuntimeException e) {
            log.warn("event=ort_native_dir_failed root=\"{}\" error=\"{}\"", root, e.toString());
            return new Result(null, Outcome.UNAVAILABLE);
        }
    }

    /**
     * {@code finalDir} exists and is not whole (or a publish failed for another reason): replace it
     * without ever destroying a directory a sibling just published. Serialised by an in-JVM monitor and
     * a file lock; the check is repeated inside the lock, and a broken directory is renamed aside, not
     * deleted in place.
     */
    private static Result recover(Path root, Path stage, Path finalDir, String user, IOException lastFailure)
            throws IOException {
        synchronized (RECOVERY) {
            try (FileChannel ch = FileChannel.open(root.resolve(LOCK_FILE),
                    StandardOpenOption.CREATE, StandardOpenOption.WRITE);
                 FileLock ignored = lockOrNull(ch)) {
                for (int attempt = 0; attempt < 3; attempt++) {
                    // Immediately before acting: a sibling may have published a whole directory.
                    if (isComplete(finalDir)) {
                        return new Result(finalDir, Outcome.REUSED);
                    }
                    if (Files.exists(finalDir, LinkOption.NOFOLLOW_LINKS)) {
                        if (!trusted(finalDir, user)) {
                            return new Result(null, Outcome.UNAVAILABLE);
                        }
                        Path aside = root.resolve(BROKEN_PREFIX + UUID.randomUUID().toString().substring(0, 8));
                        try {
                            Files.move(finalDir, aside, StandardCopyOption.ATOMIC_MOVE);
                        } catch (IOException e) {
                            lastFailure = e;
                            continue;
                        }
                        deleteTree(aside);
                    }
                    IOException failure = publish(stage, finalDir);
                    if (failure == null) {
                        fsyncDirectory(root);
                        return new Result(finalDir, Outcome.EXTRACTED);
                    }
                    lastFailure = failure;
                }
            }
        }
        if (isComplete(finalDir)) {
            return new Result(finalDir, Outcome.REUSED);
        }
        log.warn("event=ort_native_dir_publish_failed target=\"{}\" error=\"{}\"", finalDir, lastFailure);
        return new Result(null, Outcome.UNAVAILABLE);
    }

    /** A file lock for cross-process exclusion, or null when the filesystem will not give one. */
    private static FileLock lockOrNull(FileChannel ch) {
        try {
            return ch.lock();
        } catch (IOException | RuntimeException e) {
            log.debug("event=ort_native_dir_lock_unavailable error=\"{}\"", e.toString());
            return null;
        }
    }

    /** Creates the root, mode 0755 at most (the umask can only narrow it) where the platform has modes. */
    private static void createRoot(Path root) throws IOException {
        if (Files.isDirectory(root)) {
            return;
        }
        if (root.getFileSystem().supportedFileAttributeViews().contains("posix")) {
            Files.createDirectories(root, PosixFilePermissions.asFileAttribute(
                    PosixFilePermissions.fromString("rwxr-xr-x")));
        } else {
            Files.createDirectories(root);
        }
    }

    /**
     * POSIX: {@code dir} must be owned by {@code user} and not writable by group or others, or a
     * planted library could be loaded into the engine. Other platforms: true. A false return warns.
     */
    static boolean trusted(Path dir, String user) {
        PosixFileAttributeView view = Files.getFileAttributeView(dir, PosixFileAttributeView.class);
        if (view == null) {
            return true;
        }
        try {
            PosixFileAttributes a = view.readAttributes();
            String owner = a.owner().getName();
            Set<PosixFilePermission> perms = a.permissions();
            boolean ownerOk = user == null || user.equals(owner);
            boolean writable = perms.contains(PosixFilePermission.GROUP_WRITE)
                    || perms.contains(PosixFilePermission.OTHERS_WRITE);
            if (ownerOk && !writable) {
                return true;
            }
            log.warn("event=ort_native_dir_untrusted dir=\"{}\" owner=\"{}\" expected_owner=\"{}\" mode={} "
                            + "note=\"must be owned by the current user and not group- or world-writable; "
                            + "onnxruntime-java will extract into the temp directory on this start\"",
                    dir, owner, user, PosixFilePermissions.toString(perms));
            return false;
        } catch (IOException e) {
            log.warn("event=ort_native_dir_untrusted dir=\"{}\" error=\"{}\"", dir, e.toString());
            return false;
        }
    }

    /**
     * Whole means: every required library is a regular file whose size equals the size recorded
     * when it was extracted (the jar resource's length), so a truncated library is broken.
     */
    private static boolean isComplete(Path dir) {
        if (!Files.isDirectory(dir)) {
            return false;
        }
        Map<String, Long> recorded = readSizes(dir.resolve(SIZES_FILE));
        if (recorded == null) {
            return false;
        }
        for (String lib : REQUIRED) {
            if (!matches(dir, mapLibraryName(lib), recorded, true)) {
                return false;
            }
        }
        for (String lib : OPTIONAL) {
            if (!matches(dir, mapLibraryName(lib), recorded, false)) {
                return false;
            }
        }
        return true;
    }

    private static boolean matches(Path dir, String file, Map<String, Long> recorded, boolean required) {
        Long want = recorded.get(file);
        if (want == null) {
            return !required;
        }
        Path f = dir.resolve(file);
        try {
            return Files.isRegularFile(f) && want > 0 && Files.size(f) == want;
        } catch (IOException e) {
            return false;
        }
    }

    private static Map<String, Long> readSizes(Path file) {
        try {
            Map<String, Long> m = new LinkedHashMap<>();
            for (String line : Files.readAllLines(file)) {
                int eq = line.lastIndexOf('=');
                if (eq > 0) {
                    m.put(line.substring(0, eq), Long.parseLong(line.substring(eq + 1).trim()));
                }
            }
            return m;
        } catch (IOException | RuntimeException e) {
            return null;
        }
    }

    /** Copies the libraries out of the jar into {@code stage}; false if a required one is absent. */
    private static boolean extractInto(Path stage, String platform, Resources resources) throws IOException {
        Map<String, Long> sizes = new LinkedHashMap<>();
        for (String lib : REQUIRED) {
            if (!copy(stage, platform, lib, resources, sizes)) {
                log.warn("event=ort_native_dir_resource_missing library=\"{}\" platform=\"{}\"", lib, platform);
                return false;
            }
        }
        for (String lib : OPTIONAL) {
            copy(stage, platform, lib, resources, sizes);
        }
        StringBuilder manifest = new StringBuilder();
        sizes.forEach((name, size) -> manifest.append(name).append('=').append(size).append('\n'));
        Path sizesFile = stage.resolve(SIZES_FILE);
        try (FileChannel ch = FileChannel.open(sizesFile, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
            ch.write(java.nio.ByteBuffer.wrap(manifest.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8)));
            ch.force(true);
        }
        return true;
    }

    private static boolean copy(Path stage, String platform, String lib, Resources resources,
                                Map<String, Long> sizes) throws IOException {
        String file = mapLibraryName(lib);
        try (InputStream in = resources.open("/ai/onnxruntime/native/" + platform + "/" + file)) {
            if (in == null) {
                return false;
            }
            try (FileChannel ch = FileChannel.open(stage.resolve(file),
                    StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
                 OutputStream out = Channels.newOutputStream(ch)) {
                long n = in.transferTo(out);
                out.flush();
                ch.force(true);
                sizes.put(file, n);
            }
            return true;
        }
    }

    /** Best-effort fsync of a directory's entries; platforms that cannot open a directory skip it. */
    private static void fsyncDirectory(Path dir) {
        try (FileChannel ch = FileChannel.open(dir, StandardOpenOption.READ)) {
            ch.force(true);
        } catch (IOException | RuntimeException e) {
            log.debug("event=ort_native_dir_fsync_skipped dir=\"{}\" error=\"{}\"", dir, e.toString());
        }
    }

    /** Renames {@code stage} onto {@code finalDir}; null on success, else why not. */
    private static IOException publish(Path stage, Path finalDir) {
        try {
            Files.move(stage, finalDir, StandardCopyOption.ATOMIC_MOVE);
            return null;
        } catch (AtomicMoveNotSupportedException e) {
            // Same-directory rename; not expected to be unsupported. Do not fall back to a copying move:
            // it would publish a half-copied directory.
            log.warn("event=ort_native_dir_rename_unsupported error=\"{}\"", e.toString());
            return e;
        } catch (IOException e) {
            log.debug("event=ort_native_dir_publish_lost target=\"{}\" error=\"{}\"", finalDir, e.toString());
            return e;
        }
    }

    /** Removes {@code .stage-*} and {@code .broken-*} directories whose owner was killed. */
    private static void sweepStale(Path root, Instant now) {
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(root, p -> {
            String n = p.getFileName().toString();
            return n.startsWith(STAGE_PREFIX) || n.startsWith(BROKEN_PREFIX);
        })) {
            for (Path p : ds) {
                try {
                    Instant m = Files.getLastModifiedTime(p).toInstant();
                    if (m.plus(STALE_STAGE_AGE).isBefore(now)) {
                        deleteTree(p);
                    }
                } catch (IOException | RuntimeException e) {
                    log.debug("event=ort_native_dir_stage_skip dir=\"{}\" error=\"{}\"", p, e.toString());
                }
            }
        } catch (IOException | RuntimeException e) {
            log.debug("event=ort_native_dir_stage_sweep_failed root=\"{}\" error=\"{}\"", root, e.toString());
        }
    }

    /** Best-effort recursive delete of a directory this class made; never follows a link. */
    private static void deleteTree(Path dir) {
        if (!Files.exists(dir, LinkOption.NOFOLLOW_LINKS)) {
            return;
        }
        try (var walk = Files.walk(dir)) {
            walk.sorted(java.util.Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.deleteIfExists(p);
                } catch (IOException e) {
                    log.debug("event=ort_native_dir_delete_failed path=\"{}\" error=\"{}\"", p, e.toString());
                }
            });
        } catch (IOException | RuntimeException e) {
            log.debug("event=ort_native_dir_delete_failed path=\"{}\" error=\"{}\"", dir, e.toString());
        }
    }
}
