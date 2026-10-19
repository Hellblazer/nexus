// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * RDR-210 Phase 2 Step 3 (bead nexus-qhclj) — which embedding FAMILY new collections
 * get: the value of {@code NX_EMBED_PROFILE} ({@code voyage} or {@code bge}).
 *
 * <p>The profile is a choice, not a fact about what was built. The supervisor passes it
 * from the client's {@code local_embed_profile()}; the engine seeds
 * {@code nexus.embedding_profile} from it by the per-family token CONSTANTS below, so a
 * {@code voyage} profile on an engine with no key still seeds the Voyage tokens, and a
 * {@code bge} profile on a keyed engine seeds bge-768 for every content type (Technical
 * Design 2). Which embedders were built decides only what can be SERVED
 * ({@link #degradation}).
 *
 * <p>This class must not touch the ONNX runtime or any embedder class: {@code Main}
 * resolves the profile before it builds anything, and the bge token is a literal here
 * for the same reason {@link LocalModelProbe} derives its paths without loading the
 * embedder ({@code Bge768Embedder#modelToken} returns this constant).
 */
public enum EmbeddingProfile {

    /** {@code voyage-code-3} for {@code code}, {@code voyage-context-3} for every other content type. */
    VOYAGE("voyage", "Voyage"),
    /** {@code bge-base-en-v15-768} for every content type. */
    BGE("bge", "bge");

    /** The environment variable the engine reads (unset or blank means "no choice recorded"). */
    public static final String ENV = "NX_EMBED_PROFILE";

    public static final String BGE_TOKEN = "bge-base-en-v15-768";
    public static final String VOYAGE_CODE_TOKEN = "voyage-code-3";
    public static final String VOYAGE_CONTEXT_TOKEN = "voyage-context-3";

    private final String value;
    private final String label;

    EmbeddingProfile(String value, String label) {
        this.value = value;
        this.label = label;
    }

    /** The wire value of {@link #ENV}. */
    public String value() {
        return value;
    }

    /**
     * Content type to model token for this profile, keyed by {@code code}, {@code docs},
     * {@code rdr}, {@code knowledge} and {@code unknown} (the walk sentinel for an
     * unparseable collection name; it shares the CCE bucket, since {@code voyage-3} is
     * deliberately not a legal profile value). Constants, never read from an embedder.
     */
    public Map<String, String> contentTypeTokens() {
        String context = this == VOYAGE ? VOYAGE_CONTEXT_TOKEN : BGE_TOKEN;
        String code = this == VOYAGE ? VOYAGE_CODE_TOKEN : BGE_TOKEN;
        Map<String, String> tokens = new LinkedHashMap<>();
        tokens.put("knowledge", context);
        tokens.put("docs", context);
        tokens.put("rdr", context);
        tokens.put("code", code);
        tokens.put("unknown", context);
        return tokens;
    }

    /**
     * Parse {@link #ENV}'s value. Blank or {@code null} is unset (empty result). Anything
     * else must be exactly {@code voyage} or {@code bge} (after trimming): case is not
     * folded, so a typo fails the boot rather than silently choosing a family.
     *
     * @throws IllegalArgumentException for any other value, naming the allowed values
     */
    public static Optional<EmbeddingProfile> parse(String raw) {
        if (raw == null || raw.isBlank()) {
            return Optional.empty();
        }
        String v = raw.trim();
        for (EmbeddingProfile p : values()) {
            if (p.value.equals(v)) {
                return Optional.of(p);
            }
        }
        throw new IllegalArgumentException(
            ENV + " is '" + v + "'; the allowed values are 'voyage' and 'bge' (leave it unset to "
            + "keep the default: a keyed engine seeds Voyage, a keyless one bge)");
    }

    /**
     * The profile this boot seeds. A set value wins. Unset keeps today's rule: keyed means
     * Voyage, keyless means bge (an engine started without the supervisor). This is the ONE copy
     * of that rule: {@link EmbedderRouter}'s constructors that take no profile call this method.
     *
     * @throws IllegalArgumentException for an invalid value, whatever the key
     */
    public static EmbeddingProfile resolve(String raw, boolean voyageKeyPresent) {
        return parse(raw).orElse(voyageKeyPresent ? VOYAGE : BGE);
    }

    /**
     * What an unservable choice leaves unserved.
     *
     * @param chosen             the profile that cannot be served
     * @param missingPiece       what is absent, named by role (the key, "the bge-768 model and tokenizer
     *                           files"), never a path or a probe error: this text goes on the wire
     * @param detail             the same, with the absolute paths or the probe error, for the boot log only
     * @param remedy             the command or setting that supplies it
     * @param serving            the family the engine still serves
     * @param refusedCollections which family's collections get the 422
     */
    public record Degradation(EmbeddingProfile chosen, String missingPiece, String detail, String remedy,
                              EmbeddingProfile serving, String refusedCollections) {}

    /**
     * Empty when this profile's family can be served on the given presence; otherwise the
     * degraded-boot description (Sam, gate round 1: boot with the family that can be
     * served, loud, never refused). Neither family present is not handled here; it stays
     * the existing boot failure.
     */
    public Optional<Degradation> degradation(LocalModelProbe.Presence presence) {
        if (this == VOYAGE) {
            if (presence.voyageKeyPresent()) {
                return Optional.empty();
            }
            String missing = "the Voyage key is not set (NX_VOYAGE_API_KEY)";
            return Optional.of(new Degradation(VOYAGE, missing, missing,
                "persist the key with `nx config set voyage_api_key --stdin` (or set NX_VOYAGE_API_KEY) "
                    + "and restart the engine",
                BGE, VOYAGE.label));
        }
        if (presence.bgePresent()) {
            return Optional.empty();
        }
        String missing;
        String detail;
        if (presence.bgeMissing().isEmpty()) {
            missing = "the bge-768 model files could not be checked";
            detail = "the bge model files are not usable (" + presence.bgeDetail() + ")";
        } else {
            List<String> roles = presence.bgeMissing().stream().map(EmbeddingProfile::fileRole).distinct().toList();
            missing = "the bge-768 " + String.join(" and ", roles)
                + (presence.bgeMissing().size() == 1 ? " file is missing" : " files are missing");
            detail = "the bge model files are missing: " + String.join(", ", presence.bgeMissing());
        }
        return Optional.of(new Degradation(BGE, missing, detail,
            "run `" + BootPlan.PROVISION_COMMAND + "` to provision the bge model, then restart the engine "
                + "(the ONNX runtime is set up at boot)",
            VOYAGE, BGE.label));
    }

    /** {@code tokenizer} or {@code model}: the role of a model artifact, from its file name. */
    private static String fileRole(String path) {
        return Path.of(path).getFileName().toString().contains("tokenizer") ? "tokenizer" : "model";
    }
}
