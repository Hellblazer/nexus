// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.vectors.BootPlan.Bge;
import static dev.nexus.service.vectors.BootPlan.Rerank;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-210 Phase 2 Step 4 (bead nexus-un7vj) — what a boot builds, asserted by VALUE. {@code Main.main} cannot
 * run in a test, so the three-way bge decision (nexus-ngvxb), the profile-chosen reranker (Technical Design 3),
 * the ONNX native-directory condition (critic item 2), the admission gate (nexus-e81g7) and the banner's remedy
 * text live in {@link BootPlan} and are checked here over the whole presence-by-profile table.
 */
class BootPlanTest {

    private static LocalModelProbe.Presence presence(boolean bge, boolean partial, boolean key, boolean crossEncoder) {
        List<String> missing = bge ? List.of()
                : partial ? List.of("/models/bge/tokenizer.json")
                          : List.of("/models/bge/model.onnx", "/models/bge/tokenizer.json");
        return new LocalModelProbe.Presence(bge, partial, key, "bge-detail", missing,
                crossEncoder, "ce-detail", crossEncoder ? List.of() : List.of("/models/ce/model.onnx"));
    }

    // ── the three-way bge decision, plus its failures (nexus-ngvxb) ────────────────────────────

    @Test
    void bothBgeFiles_build_whateverTheKeyAndProfile() {
        for (boolean key : new boolean[] {true, false}) {
            for (EmbeddingProfile profile : EmbeddingProfile.values()) {
                var plan = BootPlan.decide(presence(true, false, key, false), profile);
                assertThat(plan.bge()).as("key=%s profile=%s", key, profile).isEqualTo(Bge.BUILD);
                assertThat(plan.buildsBge()).isTrue();
                assertThat(plan.fails()).isFalse();
                assertThat(plan.bgeState()).isEqualTo("built");
            }
        }
    }

    @Test
    void aPartialBgeInstall_isVoyageOnlyDegradedWithAKey_andAFailureWithout() {
        var keyed = BootPlan.decide(presence(false, true, true, false), EmbeddingProfile.VOYAGE);
        assertThat(keyed.bge()).isEqualTo(Bge.PARTIAL_DEGRADED);
        assertThat(keyed.fails()).isFalse();
        assertThat(keyed.buildsBge()).isFalse();
        assertThat(keyed.bgeState()).isEqualTo("partial");

        var keyless = BootPlan.decide(presence(false, true, false, false), EmbeddingProfile.BGE);
        assertThat(keyless.bge()).as("the partial keyless box gets its own failure, not the no-embedder one")
                .isEqualTo(Bge.FAIL_PARTIAL_NO_KEY);
        assertThat(keyless.fails()).isTrue();
        assertThat(keyless.bgeState()).isEqualTo("partial");
    }

    @Test
    void neitherFamily_failsTheBoot() {
        for (EmbeddingProfile profile : EmbeddingProfile.values()) {
            var plan = BootPlan.decide(presence(false, false, false, true), profile);
            assertThat(plan.bge()).isEqualTo(Bge.FAIL_NO_EMBEDDER);
            assertThat(plan.fails()).isTrue();
            assertThat(plan.bgeState()).isEqualTo("absent");
        }
    }

    @Test
    void aKeyAndNoBgeFiles_isTheCloudShape_noBgeBuilt() {
        var plan = BootPlan.decide(presence(false, false, true, false), EmbeddingProfile.VOYAGE);
        assertThat(plan.bge()).isEqualTo(Bge.ABSENT);
        assertThat(plan.fails()).isFalse();
        assertThat(plan.buildsBge()).isFalse();
    }

    // ── the reranker follows the PROFILE, not the key (Technical Design 3) ─────────────────────

    @Test
    void theRerankerTable_isPinnedForEveryProfileAndKey() {
        assertThat(BootPlan.chooseReranker(EmbeddingProfile.VOYAGE, true)).as("voyage profile + key")
                .isEqualTo(Rerank.VOYAGE);
        assertThat(BootPlan.chooseReranker(EmbeddingProfile.VOYAGE, false)).as("voyage profile, keyless: degraded boot")
                .isEqualTo(Rerank.CROSS_ENCODER);
        assertThat(BootPlan.chooseReranker(EmbeddingProfile.BGE, true)).as("bge profile on a KEYED box")
                .isEqualTo(Rerank.CROSS_ENCODER);
        assertThat(BootPlan.chooseReranker(EmbeddingProfile.BGE, false)).as("bge profile, keyless")
                .isEqualTo(Rerank.CROSS_ENCODER);
    }

    @Test
    void theRerankerIsCarriedByThePlan_forEveryPresenceShape() {
        // key x bge files x profile, 8 shapes that pass the boot
        for (boolean key : new boolean[] {true, false}) {
            for (boolean bge : new boolean[] {true, false}) {
                for (EmbeddingProfile profile : EmbeddingProfile.values()) {
                    if (!key && !bge) continue;   // the boot failure, no reranker
                    var plan = BootPlan.decide(presence(bge, false, key, true), profile);
                    Rerank expected = profile == EmbeddingProfile.VOYAGE && key ? Rerank.VOYAGE : Rerank.CROSS_ENCODER;
                    assertThat(plan.rerank()).as("key=%s bge=%s profile=%s", key, bge, profile).isEqualTo(expected);
                    assertThat(plan.buildsCrossEncoder()).isEqualTo(expected == Rerank.CROSS_ENCODER);
                    assertThat(plan.rerankVia()).isEqualTo(expected == Rerank.VOYAGE ? "voyage" : "cross_encoder");
                }
            }
        }
    }

    // ── the admission gate is built with no bge embedder (nexus-e81g7) ─────────────────────────

    @Test
    void theAdmissionGate_isBuiltForTheCrossEncoderWithNoBgeEmbedder() {
        // The keyed bge-profile box with no bge files: cross-encoder only.
        var plan = BootPlan.decide(presence(false, false, true, true), EmbeddingProfile.BGE);
        assertThat(plan.buildsBge()).isFalse();
        assertThat(plan.buildsCrossEncoder()).isTrue();
        assertThat(plan.buildsAdmissionGate()).as("the gate needs no bge embedder").isTrue();
    }

    @Test
    void theAdmissionGate_isNotBuiltForTheVoyageOnlyCloudShape() {
        var plan = BootPlan.decide(presence(false, false, true, false), EmbeddingProfile.VOYAGE);
        assertThat(plan.buildsBge()).isFalse();
        assertThat(plan.buildsCrossEncoder()).isFalse();
        assertThat(plan.buildsAdmissionGate()).isFalse();
    }

    @Test
    void theAdmissionGate_isBuiltWheneverBgeOrTheCrossEncoderIs() {
        for (boolean key : new boolean[] {true, false}) {
            for (boolean bgeFiles : new boolean[] {true, false}) {
                for (EmbeddingProfile profile : EmbeddingProfile.values()) {
                    if (!key && !bgeFiles) continue;
                    var plan = BootPlan.decide(presence(bgeFiles, false, key, false), profile);
                    assertThat(plan.buildsAdmissionGate()).as("key=%s bge=%s profile=%s", key, bgeFiles, profile)
                            .isEqualTo(plan.buildsBge() || plan.buildsCrossEncoder());
                }
            }
        }
    }

    // ── ONNX native directory: keyed on files PRESENT (critic item 2) ──────────────────────────

    @Test
    void theOrtNativeDir_isLoadedOnPresentFiles_andNotOnTheCloudShape() {
        assertThat(BootPlan.decide(presence(true, false, false, false), EmbeddingProfile.BGE).loadsOrtNativeDir())
                .as("bge files").isTrue();
        assertThat(BootPlan.decide(presence(false, false, true, true), EmbeddingProfile.BGE).loadsOrtNativeDir())
                .as("keyed bge profile, cross-encoder files only: widened condition").isTrue();
        assertThat(BootPlan.decide(presence(false, false, true, false), EmbeddingProfile.VOYAGE).loadsOrtNativeDir())
                .as("cloud shape: key, no model files").isFalse();
        assertThat(BootPlan.decide(presence(false, false, true, false), EmbeddingProfile.BGE).loadsOrtNativeDir())
                .as("keyed bge profile with no model files at all: nothing to extract for").isFalse();
        assertThat(BootPlan.decide(presence(false, true, true, false), EmbeddingProfile.VOYAGE).loadsOrtNativeDir())
                .as("a partial bge install is not a present model").isFalse();
    }

    // ── local_embed_activity, missing cross-encoder files, banner level ────────────────────────

    @Test
    void localEmbedActivityIsReportedWheneverBgeIsBuilt() {
        // bge-only, dual (keyed voyage profile), and a keyed bge profile: all build bge.
        assertThat(BootPlan.decide(presence(true, false, false, true), EmbeddingProfile.BGE).buildsBge()).isTrue();
        assertThat(BootPlan.decide(presence(true, false, true, true), EmbeddingProfile.VOYAGE).buildsBge()).isTrue();
        assertThat(BootPlan.decide(presence(true, false, true, true), EmbeddingProfile.BGE).buildsBge()).isTrue();
        assertThat(BootPlan.decide(presence(false, false, true, true), EmbeddingProfile.VOYAGE).buildsBge())
                .as("Voyage-only reports none").isFalse();
    }

    @Test
    void missingCrossEncoderFiles_areFlaggedOnlyWhenTheCrossEncoderIsTheReranker() {
        assertThat(BootPlan.decide(presence(false, false, true, false), EmbeddingProfile.BGE)
                .crossEncoderFilesMissing()).as("bge profile, no files").isTrue();
        assertThat(BootPlan.decide(presence(false, false, true, true), EmbeddingProfile.BGE)
                .crossEncoderFilesMissing()).as("bge profile, files present").isFalse();
        assertThat(BootPlan.decide(presence(false, false, true, false), EmbeddingProfile.VOYAGE)
                .crossEncoderFilesMissing()).as("Voyage reranks, the cross-encoder is not used").isFalse();
    }

    @Test
    void theBanner_isLoudOnAKeylessBoxADegradedChoiceAPartialInstallOrNoCrossEncoder_andQuietOnTheCloudShape() {
        assertThat(BootPlan.decide(presence(false, false, true, false), EmbeddingProfile.VOYAGE).loudBanner())
                .as("cloud shape").isFalse();
        assertThat(BootPlan.decide(presence(true, false, true, true), EmbeddingProfile.VOYAGE).loudBanner())
                .as("healthy dual").isFalse();
        assertThat(BootPlan.decide(presence(true, false, false, true), EmbeddingProfile.BGE).loudBanner())
                .as("keyless: Voyage collections refused").isTrue();
        assertThat(BootPlan.decide(presence(false, false, true, true), EmbeddingProfile.BGE).loudBanner())
                .as("bge profile without bge files: degraded").isTrue();
        assertThat(BootPlan.decide(presence(false, true, true, true), EmbeddingProfile.VOYAGE).loudBanner())
                .as("partial install").isTrue();
        assertThat(BootPlan.decide(presence(true, false, true, false), EmbeddingProfile.BGE).loudBanner())
                .as("cross-encoder files absent").isTrue();
    }

    @Test
    void theRefusedFamilies_nameWhatTheBootCannotServe() {
        assertThat(BootPlan.decide(presence(true, false, true, true), EmbeddingProfile.VOYAGE).refusedFamilies())
                .as("dual").isEmpty();
        assertThat(BootPlan.decide(presence(false, false, true, false), EmbeddingProfile.VOYAGE).refusedFamilies())
                .as("cloud shape").containsExactly("bge");
        assertThat(BootPlan.decide(presence(true, false, false, true), EmbeddingProfile.BGE).refusedFamilies())
                .as("keyless").containsExactly("voyage");
    }

    // ── one remedy command everywhere ──────────────────────────────────────────────────────────

    @Test
    void everyRemedyThatNamesAProvisionCommand_namesTheSameOne() {
        assertThat(BootPlan.PROVISION_COMMAND).isEqualTo("nx init --service");

        var bgeDegraded = BootPlan.decide(presence(false, false, true, true), EmbeddingProfile.BGE);
        assertThat(bgeDegraded.degradation()).isPresent();
        assertThat(bgeDegraded.degradation().get().remedy()).contains("`nx init --service`");
        assertThat(bgeDegraded.bannerHint()).as("a degraded choice's own remedy is the banner's")
                .contains(bgeDegraded.degradation().get().remedy());

        var partial = BootPlan.decide(presence(false, true, true, true), EmbeddingProfile.VOYAGE);
        assertThat(partial.degradation()).as("a voyage profile with a key is served: no degradation").isEmpty();
        assertThat(partial.bannerHint()).contains("`nx init --service`");

        var noCrossEncoder = BootPlan.decide(presence(true, false, true, false), EmbeddingProfile.BGE);
        assertThat(noCrossEncoder.bannerHint()).contains("`nx init --service`");

        for (var plan : List.of(bgeDegraded, partial, noCrossEncoder)) {
            // No plain "nx init" without the --service flag anywhere in a hint.
            assertThat(plan.bannerHint().replace("nx init --service", "")).doesNotContain("nx init");
        }
    }

    @Test
    void aHealthyDualBox_hasNothingToFix() {
        assertThat(BootPlan.decide(presence(true, false, true, true), EmbeddingProfile.VOYAGE).bannerHint()).isEmpty();
    }

    @Test
    void aKeylessBox_hintsTheKey() {
        assertThat(BootPlan.decide(presence(true, false, false, true), EmbeddingProfile.BGE).bannerHint())
                .contains("NX_VOYAGE_API_KEY");
    }

    // ── building the reranker: only the chosen kind is constructed ─────────────────────────────

    private static final class CountingReranker implements Reranker {
        final AtomicInteger calls = new AtomicInteger();
        private final String token;
        CountingReranker(String token) { this.token = token; }
        @Override public String modelToken() { return token; }
        @Override public List<Scored> rerank(String query, List<String> documents, Integer topK) {
            calls.incrementAndGet();
            return List.of();
        }
    }

    @Test
    void aBgeProfile_neverConstructsTheVoyageReranker_evenKeyed() {
        var plan = BootPlan.decide(presence(false, false, true, true), EmbeddingProfile.BGE);
        AtomicInteger voyageBuilt = new AtomicInteger();
        var cross = new CountingReranker("ms-marco-minilm-l6-v2");
        var built = plan.buildReranker(() -> { voyageBuilt.incrementAndGet(); return new CountingReranker("rerank-2.5"); },
                () -> cross, new LocalOnnxAdmission(1, 1000));
        assertThat(voyageBuilt).as("the Voyage reranker is never built, so nothing can be sent to Voyage").hasValue(0);
        assertThat(built).isInstanceOf(AdmissionControlledReranker.class);
        assertThat(built.modelToken()).isEqualTo("ms-marco-minilm-l6-v2");
    }

    @Test
    void aVoyageProfileWithAKey_buildsOnlyTheVoyageReranker() {
        var plan = BootPlan.decide(presence(true, false, true, true), EmbeddingProfile.VOYAGE);
        AtomicInteger crossBuilt = new AtomicInteger();
        var voyage = new CountingReranker("rerank-2.5");
        var built = plan.buildReranker(() -> voyage, () -> { crossBuilt.incrementAndGet(); return voyage; }, null);
        assertThat(built).isSameAs(voyage);
        assertThat(crossBuilt).as("the cross-encoder is not built, and Voyage needs no admission gate").hasValue(0);
    }

    @Test
    void aVoyageProfileWithNoKey_buildsTheCrossEncoder() {
        var plan = BootPlan.decide(presence(true, false, false, true), EmbeddingProfile.VOYAGE);
        AtomicInteger voyageBuilt = new AtomicInteger();
        var built = plan.buildReranker(() -> { voyageBuilt.incrementAndGet(); return new CountingReranker("v"); },
                () -> new CountingReranker("ms-marco-minilm-l6-v2"), new LocalOnnxAdmission(1, 1000));
        assertThat(built.modelToken()).isEqualTo("ms-marco-minilm-l6-v2");
        assertThat(voyageBuilt).hasValue(0);
    }

    @Test
    void theCrossEncoderWithNoAdmissionGate_failsTheBootLoud_notWithANullPointerOnTheFirstRerank() {
        var plan = BootPlan.decide(presence(false, false, true, true), EmbeddingProfile.BGE);
        assertThatThrownBy(() -> plan.buildReranker(() -> new CountingReranker("v"),
                () -> new CountingReranker("c"), null))
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("admission gate");
    }
}
