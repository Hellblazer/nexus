// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.build.PgContainerRecipe;
import org.testcontainers.images.builder.ImageFromDockerfile;
import org.testcontainers.utility.DockerImageName;

/**
 * nexus-f3yqd.2.8 (RDR-246 Phase 1 Step 3c): builds {@link PgContainerRecipe#DATA_MOVE_IMAGE}, the test Postgres
 * image plus pg_background at the bundle's pinned commit, from {@code docker/pg-background.Dockerfile}.
 *
 * <p><b>How CI gets the image (decided here, nexus-f3yqd.2.8):</b> every run builds it through the Docker daemon
 * it already uses for the suite, as {@code PlpgsqlCheckGateTest} does for its plpgsql_check image. Nothing is
 * pushed to or pulled from a registry, so there is no new credential and no new CI secret. The build is kept after
 * the run ({@code deleteOnExit=false}, Testcontainers deletes a built image at exit by default), so the daemon's
 * layer cache answers every later call on that host: the pinned commit makes the build deterministic, and a warm
 * call costs a build-context upload and a cache hit. A cold host pays the apt download and the compile once
 * (measured in the bead's comment). The cache keys on the base image's ID and the instruction text, so a changed
 * Dockerfile, build argument or base image rebuilds without any tag handling here.
 *
 * <p>A host with no route to GitHub or to the Debian mirror cannot build it on a cold cache, as it cannot pull
 * {@link PgContainerRecipe#IMAGE} either; data-move tests fail there, not skip.
 */
public final class PgBackgroundImage {

    /** Classpath location of the Dockerfile. */
    static final String DOCKERFILE_RESOURCE = "docker/pg-background.Dockerfile";

    private PgBackgroundImage() {}

    /**
     * Build the image (a cache hit when it is already there) and return its name. Concurrent builds of one tag by
     * several surefire forks resolve in the daemon: each build produces the same layers.
     */
    public static DockerImageName dockerImageName() {
        String name = new ImageFromDockerfile(PgContainerRecipe.DATA_MOVE_IMAGE, false)
            .withFileFromClasspath("Dockerfile", DOCKERFILE_RESOURCE)
            .withBuildArg("BASE", PgContainerRecipe.IMAGE)
            .withBuildArg("PG_BACKGROUND_COMMIT", PgContainerRecipe.PG_BACKGROUND_COMMIT)
            .get();
        return DockerImageName.parse(name);
    }
}
