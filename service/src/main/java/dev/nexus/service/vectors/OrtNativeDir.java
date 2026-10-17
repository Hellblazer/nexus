// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Locale;
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
 * SIGKILLs the service, a SIGKILLed daemon) never runs it. On chas, where {@code /tmp} is RAM, about
 * 6,000 such directories held 36 GB in four days.
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
 * tokenizer cache already live in, never the temp directory. If that location cannot be created or
 * written, the property is left unset and ORT behaves exactly as before.
 *
 * <p><b>Safe under concurrent first starts.</b> Each starter extracts into its own private
 * {@code .stage-*} directory under the root and publishes it with one directory rename onto the
 * final name. The rename is atomic and fails if the target exists, so the final directory is either
 * absent or complete; the loser of a race deletes its stage and uses the winner's directory. A stage
 * left by a killed starter is removed by the next start once it is over {@link #STALE_STAGE_AGE} old.
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

    /**
     * Makes {@code <root>/<version>-<os-arch>} exist and hold the libraries, extracting at most once.
     * Never throws; returns a null {@link Result#dir()} when nothing usable could be prepared.
     */
    public static Result ensure(Path root, String osName, String osArchName, Resources resources) {
        String platform = osArch(osName, osArchName);
        if (platform == null) {
            return new Result(null, Outcome.UNAVAILABLE);
        }
        Path finalDir = root.resolve(ORT_VERSION + "-" + platform);
        try {
            if (isComplete(finalDir)) {
                return new Result(finalDir, Outcome.REUSED);
            }
            Files.createDirectories(root);
            sweepStaleStages(root, Instant.now());
            Path stage = Files.createTempDirectory(root, STAGE_PREFIX);
            try {
                if (!extractInto(stage, platform, resources)) {
                    return new Result(null, Outcome.UNAVAILABLE);
                }
                if (publish(stage, finalDir)) {
                    return new Result(finalDir, Outcome.EXTRACTED);
                }
                // Lost a race (or a broken directory sits there): use the winner's if it is whole.
                if (isComplete(finalDir)) {
                    return new Result(finalDir, Outcome.REUSED);
                }
                deleteTree(finalDir);
                if (publish(stage, finalDir)) {
                    return new Result(finalDir, Outcome.EXTRACTED);
                }
                return isComplete(finalDir)
                        ? new Result(finalDir, Outcome.REUSED)
                        : new Result(null, Outcome.UNAVAILABLE);
            } finally {
                deleteTree(stage);
            }
        } catch (IOException | RuntimeException e) {
            log.warn("event=ort_native_dir_failed root=\"{}\" error=\"{}\"", root, e.toString());
            return new Result(null, Outcome.UNAVAILABLE);
        }
    }

    private static boolean isComplete(Path dir) {
        if (!Files.isDirectory(dir)) {
            return false;
        }
        for (String lib : REQUIRED) {
            Path f = dir.resolve(mapLibraryName(lib));
            try {
                if (!Files.isRegularFile(f) || Files.size(f) == 0) {
                    return false;
                }
            } catch (IOException e) {
                return false;
            }
        }
        return true;
    }

    /** Copies the libraries out of the jar into {@code stage}; false if a required one is absent. */
    private static boolean extractInto(Path stage, String platform, Resources resources) throws IOException {
        for (String lib : REQUIRED) {
            if (!copy(stage, platform, lib, resources)) {
                log.warn("event=ort_native_dir_resource_missing library=\"{}\" platform=\"{}\"", lib, platform);
                return false;
            }
        }
        for (String lib : OPTIONAL) {
            copy(stage, platform, lib, resources);
        }
        return true;
    }

    private static boolean copy(Path stage, String platform, String lib, Resources resources) throws IOException {
        String file = mapLibraryName(lib);
        try (InputStream in = resources.open("/ai/onnxruntime/native/" + platform + "/" + file)) {
            if (in == null) {
                return false;
            }
            Files.copy(in, stage.resolve(file), StandardCopyOption.REPLACE_EXISTING);
            return true;
        }
    }

    /** Renames {@code stage} onto {@code finalDir}; false when the target exists or the rename failed. */
    private static boolean publish(Path stage, Path finalDir) {
        try {
            Files.move(stage, finalDir, StandardCopyOption.ATOMIC_MOVE);
            return true;
        } catch (AtomicMoveNotSupportedException e) {
            // Same-directory rename; not expected to be unsupported. Do not fall back to a copying move:
            // it would publish a half-copied directory.
            log.warn("event=ort_native_dir_rename_unsupported error=\"{}\"", e.toString());
            return false;
        } catch (IOException e) {
            log.debug("event=ort_native_dir_publish_lost target=\"{}\" error=\"{}\"", finalDir, e.toString());
            return false;
        }
    }

    /** Removes {@code .stage-*} directories whose starter was killed. */
    private static void sweepStaleStages(Path root, Instant now) {
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(root, STAGE_PREFIX + "*")) {
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
        if (!Files.exists(dir, java.nio.file.LinkOption.NOFOLLOW_LINKS)) {
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
