// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import dev.nexus.service.db.UnregisteredCollectionException;
import dev.nexus.service.vectors.CceEmbedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.EmbeddingModelUnavailableException;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.VoyageEmbedder;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-pebfx.2 — embedding-mode fail-loud + model-identity dispatch.
 * RDR-204 Phase 2 (bead nexus-ft04v.16) rewrite: the routing AUTHORITY moved
 * from a collection-name model SEGMENT to the collection's {@code
 * catalog_collections} ROW, read through {@link CollectionRegistry}.
 *
 * <p>Background (2026-06-10 production migration): the service silently fell
 * back to ONNX-384 without {@code NX_VOYAGE_API_KEY}, surfacing only as
 * per-collection dim-mismatch 400s — and ONLY because voyage models happen to
 * be 1024-dim. A same-dimension wrong-model would have contaminated silently.
 *
 * <p>Contract pinned here: {@link EmbedderRouter#resolveEmbedderStrict} reads
 * {@code collection}'s registered row and dispatches by its {@code
 * embedding_model} — never a token parsed from the name itself. A row whose
 * model the current mode cannot embed is refused with {@link
 * EmbeddingModelUnavailableException} (→ HTTP 422: "this install's profile
 * names a model this mode cannot serve"), never silently embedded with a
 * different model. An UNREGISTERED collection fails loud with {@link
 * UnregisteredCollectionException} — there is no more name-shape escape hatch
 * into legacy prefix routing for a non-conformant NAME; {@link
 * EmbedderRouter#resolveEmbedder}'s prefix routing survives only for a
 * {@code null} collection (the truly collection-less {@code
 * /v1/vectors/embed} parity path) — see {@code
 * nonConformantName_stillRoutesByRegistryRow_neverByName} /
 * {@code nullCollection_stillFallsBackToLegacyPrefixRouting} below.
 *
 * <p>Pure in-process mechanism test — no PG substrate. {@link
 * CollectionRegistry#markKnown} seeds the (tenant, collection) → row mapping
 * directly (a pure {@code ConcurrentHashMap} write, no I/O), so {@link
 * EmbedderRouter#resolveEmbedderStrict}'s {@link CollectionRegistry#lookup}
 * call is always a cache HIT here and the {@code TenantScope} argument it
 * takes for the cache-miss fallback is never dereferenced — {@code null} is
 * passed deliberately to prove that.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class EmbeddingModeFailLoudTest {

    /** Distinct from any other test class's tenant — {@link CollectionRegistry}'s
     *  cache is a process-static map shared across the whole test JVM. */
    private static final String TENANT = "embedding-mode-fail-loud-tenant";

    private static OnnxEmbedder onnx;

    @BeforeAll
    static void setUp() {
        onnx = new OnnxEmbedder();
    }

    @AfterAll
    static void tearDown() {
        onnx.close();
    }

    /** Seed {@code collection}'s {@link CollectionRow} directly in the in-process
     *  cache — no DB, no transaction. {@code dimension} is not load-bearing for
     *  any assertion in this class (routing dispatches by {@code embeddingModel}
     *  alone); passed for a realistic row shape only. */
    private static void mark(String collection, String embeddingModel, int dimension) {
        CollectionRegistry.markKnown(TENANT, collection,
            new CollectionRow("unknown", TENANT, embeddingModel, dimension, "live"));
    }

    // ── Model-token identity (item 4 precondition) ───────────────────────────

    @Test
    void modelTokens_matchRdr103Segments() {
        assertThat(onnx.modelToken()).isEqualTo("minilm-l6-v2-384");
        assertThat(new VoyageEmbedder("k", "voyage-code-3", "document").modelToken())
                .isEqualTo("voyage-code-3");
        assertThat(new VoyageEmbedder("k", "voyage-3", "document").modelToken())
                .isEqualTo("voyage-3");
        assertThat(new CceEmbedder("k", "document").modelToken())
                .isEqualTo("voyage-context-3");
    }

    // ── ONNX-local mode refuses voyage-model collections (item 3) ────────────

    @Test
    void localMode_refusesVoyageModelCollection_withExplicitMessage() {
        String collection = "knowledge__nexus__voyage-context-3__v1";
        mark(collection, "voyage-context-3", 1024);
        EmbedderRouter router = new EmbedderRouter(onnx, "document");
        assertThatThrownBy(() -> router.embedForCollection(
                null, TENANT, collection, List.of("text")))
                .isInstanceOf(EmbeddingModelUnavailableException.class)
                .hasMessageContaining("this install's profile names a model this mode cannot serve")
                .hasMessageContaining("onnx-local")
                .hasMessageContaining("voyage-context-3")
                .hasMessageContaining(collection)
                .hasMessageContaining("NX_VOYAGE_API_KEY");
    }

    @Test
    void localMode_refusesVoyageCodeModel() {
        String collection = "code__nexus__voyage-code-3__v1";
        mark(collection, "voyage-code-3", 1024);
        EmbedderRouter router = new EmbedderRouter(onnx, "query");
        assertThatThrownBy(() -> router.resolveEmbedderStrict(null, TENANT, collection))
                .isInstanceOf(EmbeddingModelUnavailableException.class)
                .hasMessageContaining("voyage-code-3");
    }

    @Test
    void localMode_stillServesMinilmModel() {
        String collection = "knowledge__dualrun__minilm-l6-v2-384__v1";
        mark(collection, "minilm-l6-v2-384", 384);
        EmbedderRouter router = new EmbedderRouter(onnx, "document");
        assertThat(router.resolveEmbedderStrict(null, TENANT, collection)).isSameAs(onnx);
    }

    // ── The row is the authority, not the name (item 4) ──────────────────────

    @Test
    void cloudMode_minilmModelRow_routesToOnnx_evenThoughNameNamesNothingElse() {
        // The live nexus-pebfx.8 failure class: knowledge__seam-b-test__minilm…
        // was prefix-routed to CCE (1024) and 400'd on the chunks_384 table.
        // Row-authoritative dispatch makes it servable regardless of the name.
        String collection = "knowledge__seam-b-test__minilm-l6-v2-384__v1";
        mark(collection, "minilm-l6-v2-384", 384);
        EmbedderRouter router = new EmbedderRouter(onnx, "dummy-key", "query");
        assertThat(router.resolveEmbedderStrict(null, TENANT, collection)).isSameAs(onnx);
    }

    @Test
    void cloudMode_voyage3Row_routesToPlainVoyage_notCce() {
        // Same-dim wrong-model hole: prefix routing sent knowledge__*__voyage-3
        // to CCE (voyage-context-3) — both 1024-dim, silent contamination.
        String collection = "knowledge__x__voyage-3__v1";
        mark(collection, "voyage-3", 1024);
        EmbedderRouter router = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThat(router.resolveEmbedderStrict(null, TENANT, collection).modelToken())
                .isEqualTo("voyage-3");
    }

    @Test
    void cloudMode_cceAndCodeRows_routeByRowsModel() {
        String cceCollection  = "docs__nexus__voyage-context-3__v1";
        String codeCollection = "code__nexus__voyage-code-3__v1";
        mark(cceCollection, "voyage-context-3", 1024);
        mark(codeCollection, "voyage-code-3", 1024);
        EmbedderRouter router = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThat(router.resolveEmbedderStrict(null, TENANT, cceCollection).modelToken())
                .isEqualTo("voyage-context-3");
        assertThat(router.resolveEmbedderStrict(null, TENANT, codeCollection).modelToken())
                .isEqualTo("voyage-code-3");
    }

    @Test
    void unknownModelRow_refusedInBothModes() {
        // Mechanism test: the routers built here are MiniLM-wired (onnx), so a
        // bge-base-en-v15-768 row has no embedder and must REFUSE, not
        // ONNX-embed into a 768-dim table. (Production local mode now wires bge
        // per RDR-160 P2 — see EmbedderRouterBge768Test for that path, where it
        // is the minilm-l6-v2-384 row that is refused.)
        String collection = "knowledge__x__bge-base-en-v15-768__v1";
        mark(collection, "bge-base-en-v15-768", 768);
        EmbedderRouter local = new EmbedderRouter(onnx, "document");
        EmbedderRouter cloud = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThatThrownBy(() -> local.resolveEmbedderStrict(null, TENANT, collection))
                .isInstanceOf(EmbeddingModelUnavailableException.class);
        assertThatThrownBy(() -> cloud.resolveEmbedderStrict(null, TENANT, collection))
                .isInstanceOf(EmbeddingModelUnavailableException.class)
                .hasMessageContaining("bge-base-en-v15-768");
    }

    // ── Only a null collection keeps legacy prefix routing ───────────────────

    @Test
    void nonConformantName_stillRoutesByRegistryRow_neverByName() {
        // RDR-204 Phase 1's universal-registration requirement: even an
        // unparseable, non-conformant name goes through the registry now —
        // there is no more name-shape escape hatch into prefix routing for a
        // NON-NULL collection. The row wins regardless of what the name looks
        // like (here, a name with no model segment at all).
        String collection = "knowledge__test";
        mark(collection, "minilm-l6-v2-384", 384);
        EmbedderRouter local = new EmbedderRouter(onnx, "document");
        assertThat(local.resolveEmbedderStrict(null, TENANT, collection)).isSameAs(onnx);
    }

    // NOTE: a genuinely UNREGISTERED (tenant, collection) pair — no CollectionRegistry
    // cache entry at all — cannot be exercised in THIS class: the cache-miss branch of
    // CollectionRegistry#lookup needs a real TenantScope backed by a real DataSource to
    // reach the SELECT that proves absence, and this class is deliberately DB-less (see
    // the class javadoc). That contract — UnregisteredCollectionException, no row ever
    // written — is already pinned against a real Testcontainers substrate by
    // CollectionRegistryTest#require_throwsWithNoRowCached_whenCollectionNeverRegistered.

    @Test
    void nullCollection_stillFallsBackToLegacyPrefixRouting() {
        // The ONLY surviving resolveEmbedderStrict entry point into
        // resolveEmbedder's prefix routing: no collection name at all to look a
        // row up by — the truly collection-less /v1/vectors/embed parity path.
        EmbedderRouter cloud = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThat(cloud.resolveEmbedderStrict(null, TENANT, null)).isSameAs(onnx);
    }

    // ── Banner surface ────────────────────────────────────────────────────────

    @Test
    void modeNameAndAvailableModels_reflectConstruction() {
        // MiniLM-wired router → availableModels reports its token. Production
        // local mode wires bge-768 (modeName stays "onnx-local"; availableModels
        // = ["bge-base-en-v15-768"]) per RDR-160 — see EmbedderRouterBge768Test.
        EmbedderRouter local = new EmbedderRouter(onnx, "document");
        assertThat(local.modeName()).isEqualTo("onnx-local");
        assertThat(local.availableModels()).containsExactly("minilm-l6-v2-384");

        EmbedderRouter cloud = new EmbedderRouter(onnx, "dummy-key", "document");
        assertThat(cloud.modeName()).isEqualTo("voyage");
        assertThat(cloud.availableModels()).containsExactly(
                "minilm-l6-v2-384", "voyage-3", "voyage-code-3", "voyage-context-3");
    }
}
