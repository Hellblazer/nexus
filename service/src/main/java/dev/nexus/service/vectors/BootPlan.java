// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.function.Supplier;

/**
 * RDR-210 Phase 2 Step 4 (bead nexus-un7vj) — what this boot builds, decided by value.
 *
 * <p>{@code Main.main} cannot run in a test, so every decision that used to live in its text
 * (the three-way bge decision of nexus-ngvxb, which reranker the profile picks, whether the ONNX
 * native-library directory is set up, whether the shared admission gate is built) is made here
 * from two inputs the boot already has: the {@link LocalModelProbe.Presence} and the resolved
 * {@link EmbeddingProfile}. {@code Main} reads the answers and does the building; the tests
 * assert the answers (and the reranker this plan builds) by value.
 *
 * <p><b>Reranker (Technical Design 3).</b> One reranker per engine, chosen by the PROFILE, never
 * by which key happens to be present: a {@code voyage} profile with a key reranks with Voyage;
 * a {@code voyage} profile without one reranks with the local cross-encoder; a {@code bge}
 * profile reranks with the cross-encoder even on a keyed box, so no rerank text goes to Voyage
 * for a user who did not choose Voyage. The cross-encoder takes no embedder, constructs without
 * I/O, and has its own files, so it is built whether or not bge is: its missing files skip the
 * rerank per request with a loud warning (the stage's degrade), never a boot failure.
 *
 * <p><b>The admission gate</b> the cross-encoder and bge share is built when either of them is,
 * and not for the Voyage-only cloud shape. <b>ONNX native-library setup</b> is keyed on the FILES
 * that are present (bge or cross-encoder), not on what will be constructed, so a box with the
 * cross-encoder files and a bge profile that lacks the bge files still sets the directory before
 * the cross-encoder's first lazy init, and the cloud shape (key, no model files) sets nothing.
 *
 * <p>Pure: no I/O, no ONNX runtime, no logging. Must not load an embedder or reranker class that
 * imports the runtime; the reranker is built through the suppliers {@code Main} passes.
 */
public final class BootPlan {

    /**
     * The one command that provisions the local models, named in every remedy text so the boot
     * banner, the degraded-boot description and the failure messages never disagree.
     */
    public static final String PROVISION_COMMAND = "nx init --service";

    /** What the boot does about the local bge embedder (the three-way decision, plus its failures). */
    public enum Bge {
        /** Both bge files are present: build {@code Bge768Embedder}. */
        BUILD,
        /** No bge files and no partial install, and the engine can still serve (a key): Voyage only. */
        ABSENT,
        /** Exactly one bge file, with a Voyage key: boot Voyage-only, loudly (nexus-ngvxb). */
        PARTIAL_DEGRADED,
        /** Exactly one bge file and no key: nothing is servable, fail the boot naming the file. */
        FAIL_PARTIAL_NO_KEY,
        /** Neither family can be built: fail the boot naming both remedies. */
        FAIL_NO_EMBEDDER
    }

    /** Which reranker serves every search on this engine. */
    public enum Rerank {
        /** Voyage {@code rerank-*}: only for a {@code voyage} profile holding a key. */
        VOYAGE,
        /** The local ms-marco cross-encoder. */
        CROSS_ENCODER
    }

    private final EmbeddingProfile profile;
    private final LocalModelProbe.Presence presence;
    private final Bge bge;
    private final Rerank rerank;
    private final Optional<EmbeddingProfile.Degradation> degradation;

    private BootPlan(EmbeddingProfile profile, LocalModelProbe.Presence presence, Bge bge, Rerank rerank,
                     Optional<EmbeddingProfile.Degradation> degradation) {
        this.profile = profile;
        this.presence = presence;
        this.bge = bge;
        this.rerank = rerank;
        this.degradation = degradation;
    }

    /** Decide the boot from the file/key presence and the resolved profile. */
    public static BootPlan decide(LocalModelProbe.Presence presence, EmbeddingProfile profile) {
        Objects.requireNonNull(presence, "presence");
        Objects.requireNonNull(profile, "profile");
        Bge bge;
        if (presence.bgePartial()) {
            // Decided before the none-present check: a partial, keyless box gets its own message.
            bge = presence.voyageKeyPresent() ? Bge.PARTIAL_DEGRADED : Bge.FAIL_PARTIAL_NO_KEY;
        } else if (!presence.any()) {
            bge = Bge.FAIL_NO_EMBEDDER;
        } else if (presence.bgePresent()) {
            bge = Bge.BUILD;
        } else {
            bge = Bge.ABSENT;
        }
        return new BootPlan(profile, presence, bge, chooseReranker(profile, presence.voyageKeyPresent()),
                profile.degradation(presence));
    }

    /**
     * The reranker the profile picks (Technical Design 3, Sam 2026-10-17, Q2 and gate round 2).
     * The key only matters to a {@code voyage} profile: a {@code bge} profile never sends rerank
     * text to Voyage, keyed or not.
     */
    public static Rerank chooseReranker(EmbeddingProfile profile, boolean voyageKeyPresent) {
        return profile == EmbeddingProfile.VOYAGE && voyageKeyPresent ? Rerank.VOYAGE : Rerank.CROSS_ENCODER;
    }

    public EmbeddingProfile profile() { return profile; }

    public Bge bge() { return bge; }

    public Rerank rerank() { return rerank; }

    /** The profile's degraded-boot description, empty when its family can be served. */
    public Optional<EmbeddingProfile.Degradation> degradation() { return degradation; }

    /** True when the boot cannot proceed at all (partial and keyless, or no family present). */
    public boolean fails() {
        return bge == Bge.FAIL_PARTIAL_NO_KEY || bge == Bge.FAIL_NO_EMBEDDER;
    }

    /** True when {@code Bge768Embedder} is built (and so {@code local_embed_activity} is reported). */
    public boolean buildsBge() {
        return bge == Bge.BUILD;
    }

    /**
     * nexus-mrsgp: what to do after provisioning absent cross-encoder files. Measured on the native image
     * (linux-arm64, 2026-10-21): an engine that built the bge embedder at boot loads cross-encoder files
     * provisioned while it runs on the next rerank (real scores, no restart; ONNX Runtime was set up at boot).
     * An engine that built no local model is unmeasured, so it keeps the restart.
     */
    public String crossEncoderProvisionHint() {
        return buildsBge()
                ? "run `" + PROVISION_COMMAND + "` to provision them; the next rerank loads them, no restart"
                : "run `" + PROVISION_COMMAND + "` to provision them, then restart the engine (no local ONNX model was loaded at boot)";
    }

    /** True when the local cross-encoder is the reranker. */
    public boolean buildsCrossEncoder() {
        return rerank == Rerank.CROSS_ENCODER;
    }

    /**
     * True when the process-wide {@link LocalOnnxAdmission} is built: for bge, for the cross-encoder,
     * or both, and never for the Voyage-only cloud shape. It needs no bge embedder.
     */
    public boolean buildsAdmissionGate() {
        return buildsBge() || buildsCrossEncoder();
    }

    /**
     * True when {@code OrtNativeDir.applyAtBoot()} runs: any local ONNX model file is PRESENT. Keyed
     * on files present, not on what will be constructed, so the cloud shape stays clean.
     */
    public boolean loadsOrtNativeDir() {
        return presence.bgePresent() || presence.crossEncoderPresent();
    }

    /** True when the cross-encoder is the reranker but its files are not on disk (per-request skip). */
    public boolean crossEncoderFilesMissing() {
        return buildsCrossEncoder() && !presence.crossEncoderPresent();
    }

    /** Families whose collections this boot refuses with a 422 ({@code bge}, {@code voyage}). */
    public List<String> refusedFamilies() {
        List<String> refused = new ArrayList<>(2);
        if (!presence.bgePresent()) refused.add("bge");
        if (!presence.voyageKeyPresent()) refused.add("voyage");
        return List.copyOf(refused);
    }

    /**
     * True when the boot banner is a WARN: a Voyage key is missing (Voyage collections are refused),
     * the choice is degraded, the bge install is partial, or the reranker's files are absent. The
     * cloud shape (key, no model files, Voyage profile, no cross-encoder) is an INFO.
     */
    public boolean loudBanner() {
        return !presence.voyageKeyPresent() || degradation.isPresent() || bge == Bge.PARTIAL_DEGRADED
                || crossEncoderFilesMissing();
    }

    /** The bge install state as the banner names it: {@code built}, {@code partial} or {@code absent}. */
    public String bgeState() {
        return switch (bge) {
            case BUILD -> "built";
            case PARTIAL_DEGRADED, FAIL_PARTIAL_NO_KEY -> "partial";
            case ABSENT, FAIL_NO_EMBEDDER -> "absent";
        };
    }

    /** The reranker as the banner names it: {@code voyage} or {@code cross_encoder}. */
    public String rerankVia() {
        return rerank == Rerank.VOYAGE ? "voyage" : "cross_encoder";
    }

    /**
     * The remedies the boot banner carries, joined, or empty when there is nothing to fix. Every
     * command in it is {@link #PROVISION_COMMAND}, and a degraded choice's own remedy is used as is,
     * so the banner, the degraded-boot description and the failure messages say the same thing.
     */
    public String bannerHint() {
        List<String> parts = new ArrayList<>(3);
        if (degradation.isPresent()) {
            parts.add(degradation.get().remedy());
        } else {
            if (!presence.voyageKeyPresent()) {
                parts.add("set NX_VOYAGE_API_KEY (or let the supervisor plumb it from VOYAGE_API_KEY / config.yml "
                        + "credentials) to serve voyage-* collections");
            }
            if (bge == Bge.PARTIAL_DEGRADED) {
                parts.add("the local bge install is incomplete; run `" + PROVISION_COMMAND
                        + "` to repair it, then restart the engine");
            }
        }
        if (crossEncoderFilesMissing()) {
            parts.add("the cross-encoder reranker's model files are absent, so rerank is skipped per request "
                    + "(and nothing is sent to Voyage in its place); " + crossEncoderProvisionHint());
        }
        return String.join("; ", parts);
    }

    /**
     * Build this plan's reranker. Only the chosen kind's supplier is called: a {@code bge} profile
     * never constructs the Voyage reranker, so there is nothing to send text to.
     *
     * @param voyage       builds the Voyage reranker (called only for {@link Rerank#VOYAGE})
     * @param crossEncoder builds the bare cross-encoder (called only for {@link Rerank#CROSS_ENCODER})
     * @param admission    the shared gate the cross-encoder runs under (never null for the cross-encoder;
     *                     it needs no bge embedder)
     * @throws IllegalStateException the cross-encoder is chosen and no admission gate was built
     */
    public Reranker buildReranker(Supplier<? extends Reranker> voyage, Supplier<? extends Reranker> crossEncoder,
                                  LocalOnnxAdmission admission) {
        if (rerank == Rerank.VOYAGE) {
            return voyage.get();
        }
        if (admission == null) {
            throw new IllegalStateException("the local cross-encoder is the reranker but no admission gate "
                    + "was built: the gate must exist whenever the cross-encoder does");
        }
        // Rerank only runs on the search/query response path, never bulk indexing: interactive=true.
        return new AdmissionControlledReranker(crossEncoder.get(), admission, true);
    }
}
