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
        assertThat(src).as("the degraded decision is the pure function's, carried by the boot plan")
            .contains("plan.degradation()");
    }

    /**
     * The plan is decided from the probe's presence and the RESOLVED profile, once. A swap to a constant profile (or
     * to a re-parse of the environment) would leave every by-value test green, because they hand the plan its
     * inputs themselves; only this text proves Main hands it the right ones. Main's call ORDER stays text-pinned
     * here and in {@code MainBgePartialInstallWiringTest}; the plan's decisions are asserted by value in
     * {@code BootPlanTest}.
     */
    @Test
    void mainDecidesThePlanFromTheProbeAndTheResolvedProfile_exactlyOnce() throws Exception {
        String src = main();
        String call = "BootPlan.decide(presence, embedProfile)";
        int decide = src.indexOf(call);
        assertThat(decide).as("the exact call").isPositive();
        assertThat(src.indexOf("BootPlan.decide(")).as("no other decide call comes first").isEqualTo(decide);
        assertThat(src.indexOf("BootPlan.decide(", decide + 1)).as("once").isNegative();
        int probe = src.indexOf("var presence = dev.nexus.service.vectors.LocalModelProbe.probe(voyageKey);");
        int resolve = src.indexOf("embedProfile = dev.nexus.service.vectors.EmbeddingProfile.resolve(");
        assertThat(probe).as("`presence` is the probe's answer").isPositive().isLessThan(decide);
        assertThat(resolve).as("`embedProfile` is the resolved profile").isPositive().isLessThan(decide);
        assertThat(src.indexOf("embedProfile = ", resolve + "embedProfile = ".length())).as("assigned only by resolve").isNegative();
        assertThat(src.indexOf("presence = ", probe + "var presence = ".length())).as("never reassigned").isNegative();
    }

    /**
     * RDR-210 Phase 2 Step 4 (bead nexus-un7vj). The decisions are asserted by value in {@code BootPlanTest}
     * and {@code RerankStageBootPlanTest}; this proves Main builds the reranker through the plan, after the routers
     * and the admission gate, and reports the posture.
     */
    @Test
    void mainBuildsTheRerankerThroughThePlan_notFromTheKey_andReportsThePosture() throws Exception {
        String src = main();
        int docRouter = src.indexOf("new EmbedderRouter(docLocal, routerKey, \"document\", embedProfile)");
        int gate = src.indexOf("LocalOnnxAdmission.fromEnv()");
        int build = src.indexOf("plan.buildReranker(");
        int posture = src.indexOf("EmbedPosture.of(plan, reranker)");
        int start = src.indexOf("service.start();");

        assertThat(build).as("the reranker comes from the plan").isPositive();
        assertThat(src.indexOf("plan.buildReranker(", build + 1)).as("once").isNegative();
        assertThat(gate).as("the gate exists before the reranker that shares it").isPositive().isLessThan(build);
        assertThat(build).as("after the routers").isGreaterThan(docRouter);
        assertThat(src).as("the choice is the plan's, not a re-derivation from the key")
            .doesNotContain("if (voyagePresent) {");
        int voyageReranker = src.indexOf("new dev.nexus.service.vectors.VoyageReranker(");
        assertThat(voyageReranker).as("the Voyage reranker is only ever built inside the plan's supplier")
            .isGreaterThan(build).isLessThan(src.indexOf("localOnnxAdmission);", build));
        assertThat(src.indexOf("new dev.nexus.service.vectors.VoyageReranker(", voyageReranker + 1)).isNegative();
        assertThat(posture).as("the posture is bound before the listener starts").isPositive().isLessThan(start);
    }

    @Test
    void theAdmissionGateIsBuiltWhenThePlanSaysSo_notInsideTheBgeBlock() throws Exception {
        String src = main();
        int gate = src.indexOf("LocalOnnxAdmission.fromEnv()");
        assertThat(gate).isPositive();
        assertThat(src.indexOf("LocalOnnxAdmission.fromEnv()", gate + 1)).as("one construction site").isNegative();
        assertThat(src.substring(src.lastIndexOf("localOnnxAdmission =", gate), gate))
            .as("guarded by the plan's answer, which is bge OR the cross-encoder").contains("plan.buildsAdmissionGate()");
        assertThat(gate).as("built before the bge embedder, so the cross-encoder can have it with no bge")
            .isLessThan(src.indexOf("new Bge768Embedder()"));
        int bgeBlock = src.indexOf("if (plan.buildsBge()) {");
        assertThat(gate).as("outside the bge-present block").isLessThan(bgeBlock);
    }

    @Test
    void theBannerNamesBothFamiliesTheProfileAndTheReranker_onEveryLevel() throws Exception {
        String src = main();
        int loud = src.indexOf("log.warn(\"event=embedding_mode_banner");
        int quiet = src.indexOf("log.info(\"event=embedding_mode_banner");
        assertThat(loud).isPositive();
        assertThat(quiet).isGreaterThan(loud);
        for (String banner : new String[] {src.substring(loud, quiet), src.substring(quiet, src.indexOf(";", quiet + 400))}) {
            assertThat(banner).contains("mode={}").contains("profile={}").contains("reranker={}")
                .contains("bge={}").contains("voyage={}");
        }
        assertThat(src.substring(loud, quiet)).as("the loud banner carries the remedy").contains("plan.bannerHint()");
        assertThat(src).as("native-smoke.sh greps this prefix").contains("\"event=embedding_mode_banner mode={}");
    }

    @Test
    void anInvalidProfileClosesTheDatasourceLogsAndExitsNonZeroBeforeAnythingElse() throws Exception {
        String src = main();
        int catchAt = src.indexOf("} catch (IllegalArgumentException e) {\n            ds.close();");
        assertThat(catchAt).as("the IllegalArgumentException handler closes the datasource first").isPositive();
        String handler = src.substring(catchAt, src.indexOf("return;", catchAt) + "return;".length());
        assertThat(handler).contains("log.error(\"event=embed_profile_invalid");
        // The exit must be a non-zero System.exit followed by a return: a bare return would leave
        // the service booting with an unset profile (review of the P2.3 commit, 2026-10-19).
        assertThat(handler).contains("System.exit(1);");
        assertThat(handler.indexOf("System.exit(1);")).isLessThan(handler.indexOf("return;"));
        assertThat(handler.indexOf("ds.close();")).isLessThan(handler.indexOf("System.exit(1);"));
    }
}
