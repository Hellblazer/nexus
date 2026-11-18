// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.build.PgContainerRecipe;
import org.testcontainers.DockerClientFactory;
import org.testcontainers.images.builder.ImageFromDockerfile;
import org.testcontainers.utility.DockerImageName;

import java.io.IOException;
import java.io.InputStream;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.time.Duration;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

/**
 * nexus-f3yqd.2.8 (RDR-246 Phase 1 Step 3c): the test Postgres image plus pg_background at the bundle's pinned
 * commit, built from {@code docker/pg-background.Dockerfile}. These constants are test-side on purpose:
 * {@code service/src/build} is a cache-key input of three CI caches, and a test pin there would invalidate them.
 *
 * <p><b>How CI gets the image (decided in nexus-f3yqd.2.8):</b> every host builds it through the Docker daemon the
 * suite already uses, as {@code PlpgsqlCheckGateTest} does for its plpgsql_check image. Nothing is pushed to or
 * pulled from a registry, so there is no new credential and no new CI secret. The image is kept after the run
 * ({@code deleteOnExit=false}; Testcontainers deletes a built image at exit by default), so a host with the tag
 * builds nothing. The tag carries a hash of the Dockerfile, the base image name and the commit, so an edit to any of
 * them is a new tag and a rebuild; an image with that tag is never rebuilt.
 *
 * <p><b>What is pinned, and what is not:</b> the pg_background source is (fetched by commit, and the build refuses
 * any other). The Debian packages it compiles against (compiler, {@code postgresql-server-dev-17}) and the base
 * image behind the tag {@link PgContainerRecipe#IMAGE} are not, so two hosts can build different bytes from one
 * source. A host whose tag exists keeps what it built.
 *
 * <p><b>Concurrency:</b> a JVM builds it once ({@code synchronized}), and the JVMs on one host (surefire forks, a
 * second worktree) take an exclusive file lock in the temp directory around the exists-check and the build, so one
 * builds and the rest find the image. The lock does not cross hosts; a host with no route to GitHub or to the
 * Debian mirror cannot build on a cold cache, as it cannot pull {@link PgContainerRecipe#IMAGE}, and the data-move
 * tests fail there, not skip.
 *
 * <p><b>Lifecycle:</b> older tags are left in place, on purpose. The final stage is the base image plus the one
 * shared object and its SQL scripts, so a tag costs the host a few hundred kilobytes over layers it already holds,
 * while removing "older" tags would let two worktrees at different pins (or two users of one Docker daemon) delete
 * each other's tag on every run, and the daemon records no tag time to tell a stale tag from a live one. The
 * build-stage layers (the compiler) are in the daemon's build cache, which the pool's weekly prune reclaims.
 */
public final class PgBackgroundImage {

    /** The pg_background release the bundle ships (scripts/build_pg_bundle.sh {@code PG_BACKGROUND_VERSION}). */
    public static final String PG_BACKGROUND_VERSION = "v2.0.4";

    /**
     * The commit {@link #PG_BACKGROUND_VERSION} resolves to: the pin of {@code pg_background_pinned_commit} in
     * scripts/build_pg_bundle.sh and of {@code PINNED_PG_BACKGROUND_COMMITS} in scripts/build_pg_bundle_windows.py.
     * The Dockerfile fetches this commit and refuses any other.
     */
    public static final String PG_BACKGROUND_COMMIT = "c1db2540408ce4626ea21bc97572ff43b1a19ff3";

    /** What {@code pg_extension.extversion} reads after {@code CREATE EXTENSION pg_background} at that commit (its control file's default_version). */
    public static final String PG_BACKGROUND_EXTVERSION = "2.0";

    /** The image repository; the tag is {@link #imageTag()}. */
    public static final String REPOSITORY = "nexus-test/pg17-pg-background";

    /** Classpath location of the Dockerfile. */
    static final String DOCKERFILE_RESOURCE = "docker/pg-background.Dockerfile";

    /**
     * One lock file per OS user: in a sticky world-writable directory (/tmp) with {@code fs.protected_regular=2} a
     * second user cannot open another user's lock file with O_CREAT, so a shared name fails on the pool hosts that
     * have two test users. Users sharing one Docker daemon each lock their own file; the exists-check inside the lock
     * and the daemon's build cache make a simultaneous build by two users a duplicate, not a conflict.
     */
    static final Path LOCK_FILE = Path.of(System.getProperty("java.io.tmpdir"),
        "nexus-pg-background-image-" + System.getProperty("user.name", "user").replaceAll("[^A-Za-z0-9_.-]", "_") + ".lock");

    /** How long a build waits for the lock a peer JVM holds: a cold build is about two minutes. */
    static final Duration LOCK_WAIT = Duration.ofMinutes(15);

    private static DockerImageName built;

    private PgBackgroundImage() {}

    /** {@code <version>-<commit prefix>-<hash of the Dockerfile, the base image and the commit>}, e.g. {@code 2.0.4-c1db2540-0123456789ab}. */
    static String imageTag() {
        try (InputStream in = PgBackgroundImage.class.getClassLoader().getResourceAsStream(DOCKERFILE_RESOURCE)) {
            if (in == null) {
                throw new IllegalStateException("classpath resource " + DOCKERFILE_RESOURCE + " not found");
            }
            MessageDigest sha = MessageDigest.getInstance("SHA-256");
            sha.update(in.readAllBytes());
            sha.update(("\n" + PgContainerRecipe.IMAGE + "\n" + PG_BACKGROUND_COMMIT).getBytes(StandardCharsets.UTF_8));
            return PG_BACKGROUND_VERSION.substring(1) + "-" + PG_BACKGROUND_COMMIT.substring(0, 8) + "-"
                + HexFormat.of().formatHex(sha.digest()).substring(0, 12);
        } catch (IOException | NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The image's name with its tag; the image may not exist on this host yet. */
    static String imageName() {
        return REPOSITORY + ":" + imageTag();
    }

    /** Build the image if this host lacks it, and return its name. */
    public static synchronized DockerImageName dockerImageName() {
        if (built != null) {
            return built;
        }
        String name = imageName();
        try (FileChannel ch = FileChannel.open(LOCK_FILE, StandardOpenOption.CREATE, StandardOpenOption.WRITE);
             FileLock ignored = lock(ch, LOCK_WAIT)) {
            if (!exists(name)) {
                new ImageFromDockerfile(name, false)
                    .withFileFromClasspath("Dockerfile", DOCKERFILE_RESOURCE)
                    .withBuildArg("BASE", PgContainerRecipe.IMAGE)
                    .withBuildArg("PG_BACKGROUND_COMMIT", PG_BACKGROUND_COMMIT)
                    .get();
            }
        } catch (IOException e) {
            throw new IllegalStateException("could not lock " + LOCK_FILE, e);
        }
        built = DockerImageName.parse(name);
        return built;
    }

    /** An exclusive lock on {@code ch}, polled until {@code wait} is spent; a peer that holds it that long is a failure, not a wait. */
    static FileLock lock(FileChannel ch, Duration wait) throws IOException {
        long deadline = System.nanoTime() + wait.toNanos();
        while (true) {
            try {
                FileLock l = ch.tryLock();
                if (l != null) {
                    return l;
                }
            } catch (java.nio.channels.OverlappingFileLockException e) {
                // held by another thread of this JVM; dockerImageName() is synchronized, so only a direct caller gets here
            }
            if (System.nanoTime() - deadline >= 0) {
                throw new IllegalStateException("another process has held " + LOCK_FILE + " for " + wait
                    + " (building the pg_background image?); the data-move tests cannot continue");
            }
            try {
                Thread.sleep(500);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("interrupted waiting for " + LOCK_FILE, e);
            }
        }
    }

    private static boolean exists(String name) {
        try {
            DockerClientFactory.instance().client().inspectImageCmd(name).exec();
            return true;
        } catch (com.github.dockerjava.api.exception.NotFoundException e) {
            return false;
        }
    }
}
