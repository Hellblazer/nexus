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
 * <p><b>Lifecycle:</b> after a build, the older tags of this repository are removed ({@link #removeOlderTags}), so a
 * moved pin does not leave the previous image on the host's disk.
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

    private static final Path LOCK_FILE = Path.of(System.getProperty("java.io.tmpdir"), "nexus-pg-background-image.lock");

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
             FileLock ignored = ch.lock()) {
            if (!exists(name)) {
                new ImageFromDockerfile(name, false)
                    .withFileFromClasspath("Dockerfile", DOCKERFILE_RESOURCE)
                    .withBuildArg("BASE", PgContainerRecipe.IMAGE)
                    .withBuildArg("PG_BACKGROUND_COMMIT", PG_BACKGROUND_COMMIT)
                    .get();
            }
            removeOlderTags(name);
        } catch (IOException e) {
            throw new IllegalStateException("could not lock " + LOCK_FILE, e);
        }
        built = DockerImageName.parse(name);
        return built;
    }

    private static boolean exists(String name) {
        try {
            DockerClientFactory.instance().client().inspectImageCmd(name).exec();
            return true;
        } catch (com.github.dockerjava.api.exception.NotFoundException e) {
            return false;
        }
    }

    /**
     * Remove this repository's tags other than {@code keep}. Best effort: a tag in use by a running container is
     * refused by the daemon and stays; nothing here can fail a test.
     */
    static void removeOlderTags(String keep) {
        try {
            var docker = DockerClientFactory.instance().client();
            for (var img : docker.listImagesCmd().withReferenceFilter(REPOSITORY).exec()) {
                for (String tag : img.getRepoTags() == null ? new String[0] : img.getRepoTags()) {
                    if (tag.startsWith(REPOSITORY + ":") && !tag.equals(keep)) {
                        try {
                            docker.removeImageCmd(tag).exec();
                        } catch (RuntimeException e) {
                            System.out.println("PG_BACKGROUND_IMAGE kept older image " + tag + ": " + e.getMessage());
                        }
                    }
                }
            }
        } catch (RuntimeException e) {
            System.out.println("PG_BACKGROUND_IMAGE could not list older images: " + e.getMessage());
        }
    }
}
