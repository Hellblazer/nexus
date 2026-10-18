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
import java.nio.file.FileSystems;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
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
import java.util.EnumSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;
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
 * version directory must be owned by the current user and not writable by group or others. Only the
 * ROOT is ever tightened: a root the current user owns that is group- or world-writable (a
 * {@code mkdir} under umask 0002, Ubuntu's default) gets {@code g-w,o-w} (nothing else), is re-read
 * without following links and re-checked, and is used if the re-check passes; that is logged at WARN
 * as {@code ort_native_dir_tightened} with the old and new modes. A root with a setuid, setgid or sticky
 * bit is never chmodded (Java's chmod would clear the bit): it is refused with
 * {@code reason=special_bits}. A root someone else owns, a symbolic link, an unknown user and a failed
 * or ineffective chmod are refused too, each with its own {@code reason}, and the engine falls back to
 * ORT's old temp-directory path ({@link Outcome#UNAVAILABLE}). A group- or world-writable VERSION
 * directory is never tightened and reused, because another user could already have planted a library
 * in it: once the root is trusted it is treated as untrusted content, renamed aside to
 * {@code .broken-*} and extracted afresh (see recovery below); one that cannot be replaced is refused.
 * The stage keeps the mode {@code createTempDirectory} gave it (0700), so a directory one user
 * extracted is not reusable by another: a cache shared across users falls back per user. Windows skips
 * the check and changes no permissions.
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
 * and a file lock on {@code <root>/.lock}, with the check repeated inside the lock and again just before
 * the move, so a sibling that has just published a whole directory is not moved out from under its loader.
 * The lock is opened and taken on a daemon thread bounded by {@link #LOCK_TIMEOUT}; past that recovery goes
 * on unlocked (the pre-move re-check is then the guard, narrowing the race, not closing it).
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
    static final String LOCK_FILE = ".lock";

    /**
     * How long broken-directory recovery waits to open and lock {@code <root>/.lock} before going on
     * without it (as when the filesystem refuses the lock). The WHOLE acquisition, the open and every
     * {@code tryLock}, runs on a daemon thread the caller abandons after this long, because on a hard
     * NFS mount with a dead lock daemon either call can block in the kernel where no deadline of ours
     * runs. The wait is only on the recovery path.
     */
    static final Duration LOCK_TIMEOUT = Duration.ofSeconds(10);

    /** Pause between {@code tryLock} attempts while waiting for the file lock. */
    private static final Duration LOCK_POLL = Duration.ofMillis(50);

    /** Serialises broken-directory recovery within this JVM (a {@link FileLock} is per-JVM). */
    private static final Object RECOVERY = new Object();

    /** Libraries ORT cannot start without. */
    private static final List<String> REQUIRED = List.of("onnxruntime", "onnxruntime4j_jni");

    /** Extracted when the jar carries it (1.20.0 does not); ORT looks for it beside the main library. */
    private static final List<String> OPTIONAL = List.of("onnxruntime_providers_shared");

    /**
     * Who the root and version directory must be owned by. {@code uid} is the process's unix uid where
     * the platform has one: ownership is then the directory's {@code unix:uid}, so a container with no
     * passwd entry (where the owner name is a bare number and {@code user.name} is not that name) and a
     * same-name-different-uid account are judged correctly. With no uid (non-unix platform, or the
     * directory's uid unreadable) it falls back to comparing {@code name} with the owner's name.
     * Both null: unknown, ownership is unproven.
     */
    record Identity(String name, Integer uid) {
        /** Name only: the owner is compared by name. */
        static Identity named(String name) {
            return new Identity(name, null);
        }

        /** This process: {@code user.name} and, where there is a unix view, the uid it runs as. */
        static Identity current() {
            return new Identity(System.getProperty("user.name"), processUid());
        }

        boolean known() {
            return name != null || uid != null;
        }

        /** True when {@code dir}, whose owner's name is {@code ownerName}, belongs to this identity. */
        boolean owns(Path dir, String ownerName) {
            if (uid != null) {
                Integer dirUid = uidOf(dir);
                if (dirUid != null) {
                    return dirUid.intValue() == uid.intValue();
                }
                return name != null && name.equals(ownerName);
            }
            return name == null || name.equals(ownerName);
        }

        /** The identity as the warnings print it; null when unknown. */
        String describe() {
            if (uid == null) {
                return name;
            }
            return name == null ? "uid=" + uid : name + " uid=" + uid;
        }

        /** Extra warning text naming the two uids when the verdict came from them; empty otherwise. */
        String ownerNote(Path dir) {
            Integer dirUid = uid == null ? null : uidOf(dir);
            return dirUid == null ? "" : " owner_uid=" + dirUid + " expected_uid=" + uid;
        }
    }

    /** Reads one file attribute; the test seam for {@link #uidOf(Path, AttributeReader, LinkOption...)}. */
    @FunctionalInterface
    interface AttributeReader {
        Object read(Path path, String attribute, LinkOption... options) throws IOException;
    }

    /**
     * The {@code unix:uid} of {@code dir}, following links exactly as the owner-name read does (a
     * different rule here would let a link owned by us vouch for a foreign directory), or null when the
     * platform has no such attribute.
     */
    static Integer uidOf(Path dir) {
        return uidOf(dir, Files::getAttribute);
    }

    /**
     * {@link #uidOf(Path)} through {@code reader}. Null on any failure to read, including an
     * {@link Error} a native image throws for an attribute it did not register (a
     * {@code LinkageError} or a missing-registration error): the caller then judges by name instead of
     * the boot failing. A {@link VirtualMachineError} is never swallowed.
     */
    static Integer uidOf(Path dir, AttributeReader reader, LinkOption... options) {
        try {
            return reader.read(dir, "unix:uid", options) instanceof Integer i ? i : null;
        } catch (VirtualMachineError e) {
            throw e;
        } catch (IOException | RuntimeException | Error e) {
            return null;
        }
    }

    /** Set once the "no process uid" warning has been logged, so a boot says it once. */
    private static final AtomicBoolean UID_WARNED = new AtomicBoolean();

    /**
     * The uid this process runs as. On Linux it is the effective uid in {@code /proc/self/status}
     * (a file read: nothing is created, no link can be raced; {@code unix:uid} of {@code /proc/self}
     * would be root for a process that is not dumpable). Elsewhere, or when that is unreadable, it is
     * the uid of a file this process creates in {@code java.io.tmpdir} and deletes at once (a new file
     * is owned by the effective uid), read without following links. Null when there is no unix view
     * (silent: there is no uid to have) or when a unix view exists but no uid could be had (WARN once:
     * ownership is then judged by name, which refuses the user's own directory in a container with no
     * passwd entry).
     */
    static Integer processUid() {
        return processUid(Path.of("/proc/self/status"), Path.of(System.getProperty("java.io.tmpdir")),
                Files::getAttribute, UID_WARNED);
    }

    static Integer processUid(Path procStatus, Path tmpDir, AttributeReader reader, AtomicBoolean warned) {
        try {
            if (!FileSystems.getDefault().supportedFileAttributeViews().contains("unix")) {
                return null;
            }
            Integer fromProc = uidFromProcStatus(procStatus);
            if (fromProc != null) {
                return fromProc;
            }
            Integer fromProbe = uidFromProbeFile(tmpDir, reader);
            if (fromProbe != null) {
                return fromProbe;
            }
        } catch (VirtualMachineError e) {
            throw e;
        } catch (RuntimeException | Error e) {
            log.debug("event=ort_native_dir_uid_error error=\"{}\"", e.toString());
        }
        if (warned.compareAndSet(false, true)) {
            log.warn("event=ort_native_dir_uid_unavailable note=\"this platform has a unix view but the process "
                    + "uid could not be read; directory ownership is judged by owner name only, which can "
                    + "refuse your own directory when the user has no passwd entry\"");
        }
        return null;
    }

    /** The effective uid (second field of the {@code Uid:} line), or null when absent or malformed. */
    static Integer uidFromProcStatus(Path status) {
        try {
            if (!Files.isRegularFile(status)) {
                return null;
            }
            for (String line : Files.readAllLines(status)) {
                if (line.startsWith("Uid:")) {
                    String[] f = line.substring(4).trim().split("\\s+");
                    return f.length >= 2 ? Integer.valueOf(f[1]) : null;
                }
            }
        } catch (IOException | RuntimeException e) {
            log.debug("event=ort_native_dir_uid_proc_unreadable error=\"{}\"", e.toString());
        }
        return null;
    }

    private static Integer uidFromProbeFile(Path tmpDir, AttributeReader reader) {
        Path probe = null;
        try {
            probe = Files.createTempFile(tmpDir, "nx-ort-uid", null);
            return uidOf(probe, reader, LinkOption.NOFOLLOW_LINKS);
        } catch (IOException e) {
            log.debug("event=ort_native_dir_uid_probe_failed error=\"{}\"", e.toString());
            return null;
        } finally {
            if (probe != null) {
                try {
                    Files.deleteIfExists(probe);
                } catch (IOException ignored) {
                    // a leftover empty file in tmp is harmless
                }
            }
        }
    }

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
        return ensure(root, osName, osArchName, resources, Identity.current(), d -> { }, LOCK_TIMEOUT);
    }

    /**
     * Makes {@code <root>/<version>-<os-arch>} exist and hold the libraries, extracting at most once.
     * Never throws; returns a null {@link Result#dir()} when nothing usable could be prepared or the
     * root or the version directory is not trusted (see the class comment). {@code user} is the name
     * the root and version directory must be owned by (POSIX only; null skips the owner check).
     */
    static Result ensure(Path root, String osName, String osArchName, Resources resources, String user) {
        return ensure(root, osName, osArchName, resources, user, d -> { });
    }

    /** {@link #ensure(Path, String, String, Resources, String)} with a test seam; the owner is judged by name only. */
    static Result ensure(Path root, String osName, String osArchName, Resources resources, String user,
                         Consumer<Path> afterSeen) {
        return ensure(root, osName, osArchName, resources, Identity.named(user), afterSeen, LOCK_TIMEOUT);
    }

    /** The full seam set minus the lock acquirer: the real open-and-lock. */
    static Result ensure(Path root, String osName, String osArchName, Resources resources, Identity user,
                         Consumer<Path> afterSeen, Duration lockTimeout) {
        return ensure(root, osName, osArchName, resources, user, afterSeen, lockTimeout,
                OrtNativeDir::openAndPollLock);
    }

    /**
     * {@link #ensure(Path, String, String, Resources, String)} with a test seam: {@code afterSeen} runs
     * with the version directory right after each existence check that found it (in {@code ensure}
     * and again in {@code recover}), so a test can make a sibling's rename-aside land exactly there.
     */
    static Result ensure(Path root, String osName, String osArchName, Resources resources, Identity user,
                         Consumer<Path> afterSeen, Duration lockTimeout, LockAcquirer acquirer) {
        String platform = osArch(osName, osArchName);
        if (platform == null) {
            return new Result(null, Outcome.UNAVAILABLE);
        }
        Path finalDir = root.resolve(ORT_VERSION + "-" + platform);
        try {
            createRoot(root);
            if (!trustedBy(root, user, OrtNativeDir::chmodNoGroupOtherWrite)) {
                return new Result(null, Outcome.UNAVAILABLE);
            }
            // Before the reuse return: a stage left by a killed starter is removed by ANY later start.
            sweepStale(root, Instant.now());
            if (Files.exists(finalDir)) {
                afterSeen.accept(finalDir);
                // A writable version dir is not tightened and reused: it goes through recover() below.
                // ABSENT (a sibling renamed it aside after the check above) falls through to extraction.
                Trust t = versionDirTrust(finalDir, user, true);
                if (t == Trust.UNTRUSTED) {
                    return new Result(null, Outcome.UNAVAILABLE);
                }
                if (t == Trust.TRUSTED && isComplete(finalDir)) {
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
                // Lost a race (or a broken directory sits there): use the winner's if it is whole and trusted.
                if (reusable(finalDir, user)) {
                    return new Result(finalDir, Outcome.REUSED);
                }
                return recover(root, stage, finalDir, user, failure, afterSeen, lockTimeout, acquirer);
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
    private static Result recover(Path root, Path stage, Path finalDir, Identity user, IOException lastFailure,
                                  Consumer<Path> afterSeen, Duration lockTimeout, LockAcquirer acquirer)
            throws IOException {
        synchronized (RECOVERY) {
            try (Held ignored = acquireBounded(root.resolve(LOCK_FILE), lockTimeout, acquirer)) {
                for (int attempt = 0; attempt < 3; attempt++) {
                    // Immediately before acting: a sibling may have published a whole directory.
                    if (reusable(finalDir, user)) {
                        return new Result(finalDir, Outcome.REUSED);
                    }
                    if (Files.exists(finalDir, LinkOption.NOFOLLOW_LINKS)) {
                        afterSeen.accept(finalDir);
                        Trust t = versionDirTrust(finalDir, user, true);
                        if (t == Trust.UNTRUSTED) {
                            return new Result(null, Outcome.UNAVAILABLE);
                        }
                        if (t != Trust.ABSENT) {
                            // Immediately before the move: an unlocked racer must not move aside a
                            // directory a sibling published since the check at the top of this pass.
                            if (reusable(finalDir, user)) {
                                return new Result(finalDir, Outcome.REUSED);
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
        if (reusable(finalDir, user)) {
            return new Result(finalDir, Outcome.REUSED);
        }
        log.warn("event=ort_native_dir_publish_failed target=\"{}\" error=\"{}\"", finalDir, lastFailure);
        return new Result(null, Outcome.UNAVAILABLE);
    }

    /** The open lock file and the lock on it; closing releases both. Null stands for "no lock". */
    record Held(FileChannel channel, FileLock lock) implements AutoCloseable {
        @Override
        public void close() {
            try {
                if (lock != null && lock.isValid()) {
                    lock.release();
                }
            } catch (IOException | RuntimeException e) {
                log.debug("event=ort_native_dir_lock_release_failed error=\"{}\"", e.toString());
            }
            try {
                channel.close();
            } catch (IOException | RuntimeException e) {
                log.debug("event=ort_native_dir_lock_close_failed error=\"{}\"", e.toString());
            }
        }
    }

    /**
     * Opens {@code lockFile} and takes the lock on it, returning null when the filesystem refuses (an
     * {@link IOException} is thrown for that) or the attempt was {@code abandoned}, and throwing
     * {@link TimeoutException} when another process holds it for {@code timeout}. It runs on its own
     * thread, so it may block as long as the kernel makes it; it must close what it opened unless it
     * returns it.
     */
    @FunctionalInterface
    interface LockAcquirer {
        Held acquire(Path lockFile, Duration timeout, BooleanSupplier abandoned)
                throws IOException, InterruptedException, TimeoutException;
    }

    /** The real acquisition: open, then a {@code tryLock} loop (never {@code lock()}) until the deadline. */
    private static Held openAndPollLock(Path lockFile, Duration timeout, BooleanSupplier abandoned)
            throws IOException, InterruptedException, TimeoutException {
        long deadline = System.nanoTime() + timeout.toNanos();
        FileChannel ch = FileChannel.open(lockFile, StandardOpenOption.CREATE, StandardOpenOption.WRITE);
        boolean handedOver = false;
        try {
            while (!abandoned.getAsBoolean()) {
                FileLock lock = ch.tryLock();
                if (lock != null) {
                    handedOver = true;
                    return new Held(ch, lock);
                }
                if (System.nanoTime() - deadline >= 0) {
                    throw new TimeoutException("recovery lock held by another process");
                }
                Thread.sleep(LOCK_POLL.toMillis());
            }
            return null;
        } finally {
            if (!handedOver) {
                try {
                    ch.close();
                } catch (IOException ignored) {
                    // nothing to do
                }
            }
        }
    }

    /**
     * The recovery lock, or null when the filesystem will not give one, another process still holds it
     * after {@code timeout}, or the open and lock did not finish by then (the caller goes on unlocked,
     * as when the lock is refused). The whole acquisition runs on a named daemon thread of its own, never
     * a shared pool: a kernel call stuck on a dead NFS lock daemon strands only that thread, and a later
     * recovery does not wait behind it. Exactly one side owns what the attempt opened: if the caller has
     * given up (the future is cancelled) when the attempt finishes, the attempt closes it itself.
     */
    static Held acquireBounded(Path lockFile, Duration timeout, LockAcquirer acquirer) {
        CompletableFuture<Held> handoff = new CompletableFuture<>();
        Thread t = new Thread(() -> {
            Held held;
            try {
                held = acquirer.acquire(lockFile, timeout, handoff::isDone);
            } catch (TimeoutException e) {
                handoff.completeExceptionally(e);
                return;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                handoff.complete(null);
                return;
            } catch (IOException | RuntimeException e) {
                log.debug("event=ort_native_dir_lock_unavailable error=\"{}\"", e.toString());
                handoff.complete(null);
                return;
            } catch (Error e) {
                log.debug("event=ort_native_dir_lock_unavailable error=\"{}\"", e.toString());
                handoff.complete(null);
                if (e instanceof VirtualMachineError) {
                    throw e; // as uidOf and processUid: the caller goes on unlocked, the VM error is not swallowed
                }
                return;
            }
            if (!handoff.complete(held) && held != null) {
                held.close();
            }
        }, "ort-native-dir-lock");
        t.setDaemon(true);
        t.start();
        try {
            return handoff.get(timeout.toNanos(), TimeUnit.NANOSECONDS);
        } catch (TimeoutException e) {
            return givenUp(handoff, timeout, "did not finish opening and locking it");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return givenUp(handoff, timeout, "interrupted while waiting for it");
        } catch (ExecutionException | CancellationException e) {
            if (e.getCause() instanceof TimeoutException) {
                warnLockTimeout(timeout, "another process held it for the whole wait");
            }
            return null;
        }
    }

    /** The caller stops waiting: cancel the handoff, or take what finished in the instant before. */
    private static Held givenUp(CompletableFuture<Held> handoff, Duration timeout, String why) {
        if (handoff.cancel(true)) {
            warnLockTimeout(timeout, why);
            return null;
        }
        try {
            return handoff.getNow(null);
        } catch (CompletionException | CancellationException e) {
            warnLockTimeout(timeout, why);
            return null;
        }
    }

    private static void warnLockTimeout(Duration timeout, String why) {
        log.warn("event=ort_native_dir_lock_timeout timeout_ms={} note=\"the recovery lock: {}; "
                + "continuing without it\"", timeout.toMillis(), why);
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

    /** Removes group and other write from a directory; the test seam for {@link #trusted}. */
    @FunctionalInterface
    interface Tightener {
        void tighten(Path dir, Set<PosixFilePermission> current) throws IOException;
    }

    /** The real chmod: {@code g-w,o-w}, never through a symbolic link. */
    private static void chmodNoGroupOtherWrite(Path dir, Set<PosixFilePermission> current) throws IOException {
        PosixFileAttributeView view = Files.getFileAttributeView(dir, PosixFileAttributeView.class,
                LinkOption.NOFOLLOW_LINKS);
        if (view == null) {
            throw new IOException("no POSIX attribute view");
        }
        Set<PosixFilePermission> tightened = EnumSet.copyOf(current);
        tightened.remove(PosixFilePermission.GROUP_WRITE);
        tightened.remove(PosixFilePermission.OTHERS_WRITE);
        view.setPermissions(tightened);
    }

    /** Setuid, setgid and sticky: the bits {@code PosixFilePermission} cannot carry and a chmod would clear. */
    private static final int SPECIAL_BITS = 07000;

    /**
     * The setuid/setgid/sticky bits of {@code dir} itself (never through a link), or 0 where the
     * platform's {@code unix:mode} attribute is not available (then behaviour is as before this check).
     */
    static int specialBits(Path dir) {
        try {
            Object mode = Files.getAttribute(dir, "unix:mode", LinkOption.NOFOLLOW_LINKS);
            return mode instanceof Integer i ? i & SPECIAL_BITS : 0;
        } catch (IOException | RuntimeException e) {
            return 0;
        }
    }

    /** {@link #trusted(Path, String, Tightener)} with the real chmod. */
    static boolean trusted(Path dir, String user) {
        return trusted(dir, user, OrtNativeDir::chmodNoGroupOtherWrite);
    }

    /** {@link #trustedBy} judging the owner by name only. */
    static boolean trusted(Path dir, String user, Tightener tightener) {
        return trustedBy(dir, Identity.named(user), tightener);
    }

    /**
     * Trust for the ROOT. POSIX: {@code dir} must be owned by {@code user} and not writable by group
     * or others, or a planted library could be loaded into the engine. A directory the current user
     * owns that is group- or world-writable (a {@code mkdir} under umask 0002, Ubuntu's default) is
     * tightened to remove those two write bits, then re-read (without following links) and
     * re-checked; it is trusted only if the re-check passes. Refused, each with a {@code reason}: a
     * directory someone else owns ({@code foreign_owner}), an unknown user whose ownership is unproven
     * ({@code unknown_user}), a symbolic link ({@code symlink}; never chmod through one), a directory
     * with setuid, setgid or sticky set ({@code special_bits}; a chmod would clear them), a chmod that
     * failed ({@code tighten_failed}) and one that did not take effect ({@code tighten_ineffective}).
     * Other platforms: true. A false return warns.
     */
    static boolean trustedBy(Path dir, Identity who, Tightener tightener) {
        PosixFileAttributeView view = Files.getFileAttributeView(dir, PosixFileAttributeView.class);
        if (view == null) {
            return true;
        }
        try {
            PosixFileAttributes a = view.readAttributes();
            String owner = a.owner().getName();
            Set<PosixFilePermission> perms = a.permissions();
            boolean ownerOk = !who.known() || who.owns(dir, owner);
            if (ownerOk && !writableByGroupOrOthers(perms)) {
                return true;
            }
            String mode = PosixFilePermissions.toString(perms);
            String user = who.describe();
            if (who.known() && !ownerOk) {
                return refuse(dir, "foreign_owner", owner, user, mode, who.ownerNote(dir));
            }
            if (!who.known()) {
                return refuse(dir, "unknown_user", owner, user, mode, "");
            }
            if (Files.isSymbolicLink(dir)) {
                return refuse(dir, "symlink", owner, user, mode, "");
            }
            int special = specialBits(dir);
            if (special != 0) {
                return refuse(dir, "special_bits", owner, user, mode, String.format(
                        " special_mode=0%o remedy=\"the engine does not chmod a directory with setuid, setgid or "
                                + "sticky set; run chmod g-w,o-w on it yourself or point %s elsewhere\"",
                        special, ROOT_ENV));
            }
            PosixFileAttributeView noFollow = Files.getFileAttributeView(dir, PosixFileAttributeView.class,
                    LinkOption.NOFOLLOW_LINKS);
            try {
                tightener.tighten(dir, perms);
                PosixFileAttributes after = (noFollow != null ? noFollow : view).readAttributes();
                Set<PosixFilePermission> now = after.permissions();
                if (who.owns(dir, after.owner().getName()) && !writableByGroupOrOthers(now)) {
                    log.warn("event=ort_native_dir_tightened dir=\"{}\" old_mode={} new_mode={} "
                                    + "note=\"group and other write were removed; to keep a shared directory, "
                                    + "point {} elsewhere or make it owner-writable only\"",
                            dir, mode, PosixFilePermissions.toString(now), ROOT_ENV);
                    return true;
                }
                return refuse(dir, "tighten_ineffective", owner, user, mode, "");
            } catch (IOException | RuntimeException e) {
                return refuse(dir, "tighten_failed", owner, user, mode, " tighten_error=\"" + e + "\"");
            }
        } catch (IOException e) {
            log.warn("event=ort_native_dir_untrusted dir=\"{}\" reason=unreadable error=\"{}\"", dir, e.toString());
            return false;
        }
    }

    /** {@link #refuse(Path, String, String, String, String, String)} that stays silent when not announcing. */
    private static boolean refuse(boolean announce, Path dir, String reason, String owner, String user,
                                  String mode, String extra) {
        return announce ? refuse(dir, reason, owner, user, mode, extra) : false;
    }

    /** Logs why a directory is not trusted and returns false. */
    private static boolean refuse(Path dir, String reason, String owner, String user, String mode, String extra) {
        log.warn("event=ort_native_dir_untrusted dir=\"{}\" reason={} owner=\"{}\" expected_owner=\"{}\" "
                        + "mode={}{} note=\"must be owned by the current user and not group- or world-writable; "
                        + "onnxruntime-java will extract into the temp directory on this start\"",
                dir, reason, owner, user == null ? "unknown" : user, mode, extra);
        return false;
    }

    /** What the version directory is, for {@link #versionDirTrust}. */
    enum Trust { TRUSTED, WRITABLE_OWNED, UNTRUSTED, ABSENT }

    /**
     * Trust for the VERSION directory, which is never tightened: another user may already have written
     * into a group- or world-writable one. {@code TRUSTED} (owned by {@code user}, not writable by
     * group or others, or a non-POSIX platform); {@code WRITABLE_OWNED} (the current user owns it but
     * group or others can write: untrusted content, to be renamed aside and extracted afresh; warns);
     * {@code UNTRUSTED} (foreign owner, unknown user, symbolic link: refused, warns); {@code ABSENT} (gone
     * by the time its attributes were read, which a concurrent starter's rename-aside causes: not a refusal).
     */
    static Trust versionDirTrust(Path dir, Identity who, boolean announce) {
        PosixFileAttributeView view = Files.getFileAttributeView(dir, PosixFileAttributeView.class);
        if (view == null) {
            return Trust.TRUSTED;
        }
        try {
            PosixFileAttributes a = view.readAttributes();
            String owner = a.owner().getName();
            String mode = PosixFilePermissions.toString(a.permissions());
            boolean writable = writableByGroupOrOthers(a.permissions());
            String user = who.describe();
            if (who.known() && !who.owns(dir, owner)) {
                refuse(announce, dir, "foreign_owner", owner, user, mode, who.ownerNote(dir));
                return Trust.UNTRUSTED;
            }
            if (!writable) {
                return Trust.TRUSTED;
            }
            if (!who.known()) {
                refuse(announce, dir, "unknown_user", owner, user, mode, "");
                return Trust.UNTRUSTED;
            }
            if (Files.isSymbolicLink(dir)) {
                refuse(announce, dir, "symlink", owner, user, mode, "");
                return Trust.UNTRUSTED;
            }
            if (announce) log.warn("event=ort_native_dir_writable_replaced dir=\"{}\" owner=\"{}\" mode={} "
                            + "note=\"a group- or world-writable version directory is not trusted and is not "
                            + "tightened; it is replaced by a fresh extraction\"",
                    dir, owner, mode);
            return Trust.WRITABLE_OWNED;
        } catch (NoSuchFileException e) {
            // Vanished since the caller saw it (a sibling renamed it aside): absent, to be re-evaluated, not
            // unreadable. A dangling symbolic link is still there, and stays untrusted.
            if (!Files.exists(dir, LinkOption.NOFOLLOW_LINKS)) {
                return Trust.ABSENT;
            }
            if (announce) log.warn("event=ort_native_dir_untrusted dir=\"{}\" reason=unreadable error=\"{}\"", dir, e.toString());
            return Trust.UNTRUSTED;
        } catch (IOException e) {
            if (announce) log.warn("event=ort_native_dir_untrusted dir=\"{}\" reason=unreadable error=\"{}\"", dir, e.toString());
            return Trust.UNTRUSTED;
        }
    }

    /** A complete version directory that is also trusted as it stands (never a writable one). */
    private static boolean reusable(Path finalDir, Identity user) {
        return isComplete(finalDir) && versionDirTrust(finalDir, user, false) == Trust.TRUSTED;
    }

    private static boolean writableByGroupOrOthers(Set<PosixFilePermission> perms) {
        return perms.contains(PosixFilePermission.GROUP_WRITE) || perms.contains(PosixFilePermission.OTHERS_WRITE);
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
