// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-210 Phase 2 Step 3 (bead nexus-qhclj) — the profile variable {@code NX_EMBED_PROFILE}
 * and the per-family token constants. DB-less: the PG-backed seed rows are asserted in
 * {@link EmbedderRouterEmbeddingProfileSeedTest}; this class asserts the decisions by value
 * (parse/resolve, the token map, the degraded decision, and the router's refusal for the
 * family an unservable choice leaves unserved).
 */
class EmbeddingProfileTest {

    private static final String TENANT = "embedding-profile-test-tenant";
    private static final String BGE = "bge-base-en-v15-768";

    private static final class FakeBge implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> new float[768]).toList();
        }
        @Override public String modelToken() { return BGE; }
    }

    private static LocalModelProbe.Presence presence(boolean bge, boolean partial, boolean key) {
        List<String> missing = bge ? List.of()
                : partial ? List.of("/models/tokenizer.json")
                          : List.of("/models/model.onnx", "/models/tokenizer.json");
        return new LocalModelProbe.Presence(bge, partial, key, "detail", missing);
    }

    // ── parse / resolve ─────────────────────────────────────────────────────────

    @Test
    void parse_acceptsTheTwoValues_blankIsUnset() {
        assertThat(EmbeddingProfile.parse("voyage")).contains(EmbeddingProfile.VOYAGE);
        assertThat(EmbeddingProfile.parse("bge")).contains(EmbeddingProfile.BGE);
        assertThat(EmbeddingProfile.parse(" bge ")).as("surrounding whitespace is ignored").contains(EmbeddingProfile.BGE);
        assertThat(EmbeddingProfile.parse(null)).isEmpty();
        assertThat(EmbeddingProfile.parse("")).isEmpty();
        assertThat(EmbeddingProfile.parse("   ")).isEmpty();
    }

    @Test
    void parse_anInvalidValue_failsLoud_namingTheAllowedValues() {
        for (String bad : List.of("onnx", "Voyage", "BGE", "dual", "minilm", "true")) {
            assertThatThrownBy(() -> EmbeddingProfile.parse(bad))
                .as("value '%s'", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("NX_EMBED_PROFILE")
                .hasMessageContaining("'" + bad + "'")
                .hasMessageContaining("voyage")
                .hasMessageContaining("bge");
        }
    }

    @Test
    void resolve_aSetValueWins_whateverTheKey() {
        assertThat(EmbeddingProfile.resolve("voyage", false)).isEqualTo(EmbeddingProfile.VOYAGE);
        assertThat(EmbeddingProfile.resolve("voyage", true)).isEqualTo(EmbeddingProfile.VOYAGE);
        assertThat(EmbeddingProfile.resolve("bge", true)).as("the Q1 case: bge chosen on a keyed engine")
            .isEqualTo(EmbeddingProfile.BGE);
        assertThat(EmbeddingProfile.resolve("bge", false)).isEqualTo(EmbeddingProfile.BGE);
    }

    @Test
    void resolve_unset_keepsToday_keyedMeansVoyage_keylessMeansBge() {
        assertThat(EmbeddingProfile.resolve(null, true)).isEqualTo(EmbeddingProfile.VOYAGE);
        assertThat(EmbeddingProfile.resolve("", true)).isEqualTo(EmbeddingProfile.VOYAGE);
        assertThat(EmbeddingProfile.resolve(null, false)).isEqualTo(EmbeddingProfile.BGE);
        assertThat(EmbeddingProfile.resolve("  ", false)).isEqualTo(EmbeddingProfile.BGE);
    }

    @Test
    void resolve_invalid_failsEvenWithNoKey() {
        assertThatThrownBy(() -> EmbeddingProfile.resolve("nope", false))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> EmbeddingProfile.resolve("nope", true))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── the token constants ─────────────────────────────────────────────────────

    @Test
    void tokens_voyage_codeGetsCode3_everyOtherContentTypeGetsContext3() {
        assertThat(EmbeddingProfile.VOYAGE.contentTypeTokens()).containsExactlyInAnyOrderEntriesOf(Map.of(
            "code", "voyage-code-3", "knowledge", "voyage-context-3", "docs", "voyage-context-3",
            "rdr", "voyage-context-3", "unknown", "voyage-context-3"));
    }

    @Test
    void tokens_bge_everyContentTypeGetsBge768() {
        assertThat(EmbeddingProfile.BGE.contentTypeTokens()).containsExactlyInAnyOrderEntriesOf(Map.of(
            "code", BGE, "knowledge", BGE, "docs", BGE, "rdr", BGE, "unknown", BGE));
    }

    @Test
    void constants_equalWhatTheEmbeddersReport() {
        // The constants replace reads from built embedders, so a drift between a constant and
        // the embedder it names would silently seed a token the router cannot dispatch.
        EmbedderRouter dual = new EmbedderRouter(new FakeBge(), "dummy-key", "document");
        assertThat(dual.availableModels()).contains(
            EmbeddingProfile.BGE_TOKEN, EmbeddingProfile.VOYAGE_CODE_TOKEN, EmbeddingProfile.VOYAGE_CONTEXT_TOKEN);
        // Bge768Embedder.modelToken() returns EmbeddingProfile.BGE_TOKEN itself (no second literal).
    }

    // ── the router's tokens follow the profile, not the built embedders ─────────

    @Test
    void router_voyageProfile_noKey_seedsVoyageTokens_evenThoughOnlyBgeIsBuilt() {
        EmbedderRouter r = new EmbedderRouter(new FakeBge(), null, "document", EmbeddingProfile.VOYAGE);
        assertThat(r.modeName()).isEqualTo("onnx-local");
        assertThat(r.contentTypeModelTokens()).isEqualTo(EmbeddingProfile.VOYAGE.contentTypeTokens());
    }

    @Test
    void router_bgeProfile_withKey_seedsBgeForEveryContentType() {
        EmbedderRouter r = new EmbedderRouter(new FakeBge(), "dummy-key", "document", EmbeddingProfile.BGE);
        assertThat(r.modeName()).isEqualTo("dual");
        assertThat(r.contentTypeModelTokens()).isEqualTo(EmbeddingProfile.BGE.contentTypeTokens());
        assertThat(r.contentTypeModelTokens().values()).containsOnly(BGE);
    }

    @Test
    void router_bgeProfile_noBgeFiles_seedsBgeTokens_evenThoughOnlyVoyageIsBuilt() {
        EmbedderRouter r = new EmbedderRouter(null, "dummy-key", "document", EmbeddingProfile.BGE);
        assertThat(r.modeName()).isEqualTo("voyage");
        assertThat(r.contentTypeModelTokens().values()).containsOnly(BGE);
    }

    @Test
    void router_unsetProfile_keepsToday() {
        assertThat(new EmbedderRouter(new FakeBge(), "dummy-key", "document", null).contentTypeModelTokens())
            .isEqualTo(EmbeddingProfile.VOYAGE.contentTypeTokens());
        assertThat(new EmbedderRouter(new FakeBge(), null, "document", null).contentTypeModelTokens())
            .isEqualTo(EmbeddingProfile.BGE.contentTypeTokens());
        assertThat(new EmbedderRouter(null, "dummy-key", "document", null).contentTypeModelTokens())
            .isEqualTo(EmbeddingProfile.VOYAGE.contentTypeTokens());
    }

    @Test
    void router_exposesItsProfile() {
        assertThat(new EmbedderRouter(new FakeBge(), null, "document", EmbeddingProfile.VOYAGE).profile())
            .isEqualTo(EmbeddingProfile.VOYAGE);
        assertThat(new EmbedderRouter(new FakeBge(), null, "document").profile())
            .as("the three-argument form resolves the unset rule").isEqualTo(EmbeddingProfile.BGE);
    }

    // ── the degraded decision ───────────────────────────────────────────────────

    @Test
    void degraded_voyageProfile_withKey_isServable() {
        assertThat(EmbeddingProfile.VOYAGE.degradation(presence(true, false, true))).isEmpty();
        assertThat(EmbeddingProfile.VOYAGE.degradation(presence(false, false, true))).isEmpty();
    }

    @Test
    void degraded_voyageProfile_noKey_namesTheKey_andServesBge() {
        var d = EmbeddingProfile.VOYAGE.degradation(presence(true, false, false)).orElseThrow();
        assertThat(d.chosen()).isEqualTo(EmbeddingProfile.VOYAGE);
        assertThat(d.missingPiece()).contains("NX_VOYAGE_API_KEY");
        assertThat(d.remedy()).contains("voyage_api_key");
        assertThat(d.serving()).isEqualTo(EmbeddingProfile.BGE);
        assertThat(d.refusedCollections()).isEqualTo("Voyage");
    }

    @Test
    void degraded_bgeProfile_withFiles_isServable() {
        assertThat(EmbeddingProfile.BGE.degradation(presence(true, false, false))).isEmpty();
        assertThat(EmbeddingProfile.BGE.degradation(presence(true, false, true))).isEmpty();
    }

    @Test
    void degraded_bgeProfile_noFiles_namesTheFiles_andNxInit_andServesVoyage() {
        var d = EmbeddingProfile.BGE.degradation(presence(false, false, true)).orElseThrow();
        assertThat(d.chosen()).isEqualTo(EmbeddingProfile.BGE);
        assertThat(d.missingPiece()).contains("/models/model.onnx").contains("/models/tokenizer.json");
        assertThat(d.remedy()).contains("nx init");
        assertThat(d.serving()).isEqualTo(EmbeddingProfile.VOYAGE);
        assertThat(d.refusedCollections()).isEqualTo("bge");
    }

    @Test
    void degraded_bgeProfile_partialInstall_namesTheOneMissingFile() {
        var d = EmbeddingProfile.BGE.degradation(presence(false, true, true)).orElseThrow();
        assertThat(d.missingPiece()).contains("/models/tokenizer.json").doesNotContain("model.onnx");
        assertThat(d.remedy()).contains("nx init");
    }

    /** RDR-210 Phase 2 Step 4 (nexus-un7vj): one provisioning command in every remedy, the no-embedder text included. */
    @Test
    void degraded_bgeProfile_remedyIsTheOneProvisionCommand() {
        for (boolean partial : new boolean[] {false, true}) {
            var d = EmbeddingProfile.BGE.degradation(presence(false, partial, true)).orElseThrow();
            assertThat(d.remedy()).contains("`" + BootPlan.PROVISION_COMMAND + "`");
        }
    }

    // ── one copy of the unset rule (nexus-un7vj) ────────────────────────────────

    @Test
    void aRouterBuiltWithNoProfile_followsTheSameUnsetRuleAsResolve() {
        for (boolean key : new boolean[] {true, false}) {
            EmbedderRouter noProfile = new EmbedderRouter(new FakeBge(), key ? "dummy-key" : null, "document");
            assertThat(noProfile.profile()).as("key=%s", key).isEqualTo(EmbeddingProfile.resolve(null, key));
            noProfile.close();
        }
        EmbedderRouter voyageOnly = new EmbedderRouter("dummy-key", "document");
        assertThat(voyageOnly.profile()).isEqualTo(EmbeddingProfile.resolve(null, true));
        voyageOnly.close();
        EmbedderRouter bgeOnly = new EmbedderRouter(new FakeBge(), "document");
        assertThat(bgeOnly.profile()).isEqualTo(EmbeddingProfile.resolve(null, false));
        bgeOnly.close();
    }

    // ── an unservable choice: the servable family serves, the other gets the 422 ─

    @Test
    void voyageProfile_noKey_bgeCollectionServes_voyageCollectionIs422NamingTheKey() {
        String bgeColl = "docs__nexus__bge-base-en-v15-768__v1";
        String vColl = "docs__nexus__voyage-context-3__v1";
        CollectionRegistry.markKnown(TENANT, bgeColl, new CollectionRow("docs", TENANT, BGE, 768, "live"));
        CollectionRegistry.markKnown(TENANT, vColl, new CollectionRow("docs", TENANT, "voyage-context-3", 1024, "live"));
        EmbedderRouter r = new EmbedderRouter(new FakeBge(), null, "document", EmbeddingProfile.VOYAGE);

        assertThat(r.resolveEmbedderStrict(null, TENANT, bgeColl).modelToken()).isEqualTo(BGE);
        assertThatThrownBy(() -> r.resolveEmbedderStrict(null, TENANT, vColl))
            .isInstanceOf(EmbeddingModelUnavailableException.class)
            .hasMessageContaining("voyage-context-3")
            .hasMessageContaining("NX_VOYAGE_API_KEY");
    }

    @Test
    void bgeProfile_noBgeFiles_voyageCollectionServes_bgeCollectionIs422NamingNxInit() {
        String bgeColl = "docs__nexus2__bge-base-en-v15-768__v1";
        String vColl = "docs__nexus2__voyage-context-3__v1";
        CollectionRegistry.markKnown(TENANT, bgeColl, new CollectionRow("docs", TENANT, BGE, 768, "live"));
        CollectionRegistry.markKnown(TENANT, vColl, new CollectionRow("docs", TENANT, "voyage-context-3", 1024, "live"));
        EmbedderRouter r = new EmbedderRouter(null, "dummy-key", "document", EmbeddingProfile.BGE);

        assertThat(r.resolveEmbedderStrict(null, TENANT, vColl)).isInstanceOf(CceEmbedder.class);
        assertThatThrownBy(() -> r.resolveEmbedderStrict(null, TENANT, bgeColl))
            .isInstanceOf(EmbeddingModelUnavailableException.class)
            .hasMessageContaining(BGE)
            .hasMessageContaining("nx init");
    }
}
