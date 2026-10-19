// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 2 Step 3 (bead nexus-qhclj): {@code Main.main} cannot run in a test, so the
 * wiring of {@code NX_EMBED_PROFILE} is pinned on its text. The DECISIONS (parse, resolve,
 * degraded) are asserted by value in {@code EmbeddingProfileTest}; this only proves Main
 * calls them, in order, and hands the result to BOTH routers.
 */
class MainEmbedProfileWiringTest {

    private static String main() throws Exception {
        return Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
    }

    @Test
    void mainResolvesTheProfileBeforeAnyEmbedderIsBuilt_andPassesItToBothRouters() throws Exception {
        String src = main();
        int resolve = src.indexOf("EmbeddingProfile.resolve(");
        int noEmbedder = src.indexOf("event=no_embedder_available");
        int degraded = src.indexOf("event=embed_profile_degraded");
        int docRouter = src.indexOf("new EmbedderRouter(docLocal, routerKey, \"document\", embedProfile)");
        int qryRouter = src.indexOf("new EmbedderRouter(qryLocal, routerKey, \"query\", embedProfile)");
        int seed = src.indexOf("docEmbedRouter.seedEmbeddingProfile(");

        assertThat(resolve).as("Main resolves the profile").isPositive();
        assertThat(src).contains("EmbeddingProfile.ENV");
        assertThat(src).as("an invalid value exits the boot").contains("event=embed_profile_invalid");
        assertThat(resolve).as("resolved before the no-embedder boot failure").isLessThan(noEmbedder);
        assertThat(degraded).as("the degraded WARN follows the both-absent failure").isGreaterThan(noEmbedder);
        assertThat(docRouter).as("doc router carries the profile").isGreaterThan(degraded);
        assertThat(qryRouter).as("query router carries the profile").isGreaterThan(degraded);
        assertThat(seed).as("seed runs after the routers").isGreaterThan(qryRouter);
        assertThat(src).as("the degraded decision is the pure function's")
            .contains("embedProfile.degradation(presence)");
    }
}
