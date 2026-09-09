// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;

/**
 * RDR-152 bead nexus-gmiaf.21 — Routes embedding requests to the correct embedder
 * based on collection name prefix, mirroring Python's {@code t3.py} routing.
 *
 * <p>Python routing (t3.py § _embedding_fn and _cce_embed):
 * <ul>
 *   <li>{@code knowledge__}, {@code docs__}, {@code rdr__} → CCE via
 *       {@code voyageai.Client.contextualized_embed} (model {@code voyage-context-3})</li>
 *   <li>{@code code__} → standard embed via {@code voyageai.Client.embed}
 *       (model {@code voyage-code-3})</li>
 *   <li>local mode → the local ONNX-runtime embedder (RDR-160: bge-base-en-v1.5, 768d)</li>
 * </ul>
 *
 * <p>The Java service runs in one of three modes (by constructor):
 * <ul>
 *   <li><strong>Local mode</strong> ({@link #EmbedderRouter(Embedder, String)}): all
 *       collections → the injected local embedder (RDR-160 wires bge-768); voyage
 *       collections REFUSED (422).</li>
 *   <li><strong>Pure-voyage cloud mode</strong> ({@link #EmbedderRouter(String, String)} —
 *       PRODUCTION cloud, nexus-0n7uc): {@code localEmbedder == null}, NO local ONNX;
 *       prefix/segment routing to Voyage embedders; a non-conformant or
 *       {@code minilm-l6-v2-384} collection is REFUSED (422), symmetric with local
 *       mode refusing voyage. This is what Main boots when {@code NX_VOYAGE_API_KEY}
 *       is set.</li>
 *   <li><strong>Onnx-cloud mode</strong> ({@link #EmbedderRouter(OnnxEmbedder, String,
 *       String)} — TESTS / parity-gate only, NOT production): voyage routing plus a
 *       local ONNX fallback for non-conformant names. Retained because those callers
 *       run where the MiniLM model exists on disk.</li>
 * </ul>
 *
 * <p>Thread-safe: all embedder instances are stateless per-call.
 */
public final class EmbedderRouter implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(EmbedderRouter.class);

    /**
     * Local-mode embedder (RDR-160: a {@link Bge768Embedder}); also the
     * cloud-mode fallback for non-conformant prefixes. Typed to the
     * {@link Embedder} interface so the local model can change (MiniLM → bge-768)
     * without a signature churn — the MiniLM {@link OnnxEmbedder} stays a valid
     * argument, it is simply no longer what production local mode passes.
     */
    private final Embedder       localEmbedder;
    private final VoyageEmbedder voyageCodeEmbedder;    // voyage-code-3, null in local mode
    private final CceEmbedder    cceEmbedder;           // voyage-context-3, null in local mode
    private final String         inputType;             // "document" or "query"

    /**
     * RDR-103 model-segment → embedder dispatch table (bead nexus-pebfx.2).
     * Built per mode at construction; the collection name's model segment is
     * the authority for conformant names — prefix routing is the fallback for
     * non-conformant names only. A conformant collection whose model token is
     * absent here is REFUSED ({@link EmbeddingModelUnavailableException}),
     * never silently embedded with a different model.
     */
    private final Map<String, Embedder> modelEmbedders;

    /**
     * Local-mode constructor: all collections embedded via the injected local
     * embedder. RDR-160 wires a {@link Bge768Embedder} here; the dispatch table
     * is self-keyed by {@link Embedder#modelToken()}, so a collection whose model
     * segment is not this embedder's token is REFUSED (no silent fallback).
     *
     * @param localEmbedder the local ONNX-runtime embedder (production: bge-768)
     * @param inputType     {@code "document"} for indexing, {@code "query"} for search
     */
    public EmbedderRouter(Embedder localEmbedder, String inputType) {
        // nexus-00wsf review fold: this constructor does NOT apply admission
        // control itself — see AdmissionControlledEmbedder's javadoc. A
        // caller who wants the local (bge/ONNX) embed path gated wraps
        // localEmbedder in an AdmissionControlledEmbedder BEFORE passing it
        // in (Main.java does this, sharing ONE LocalOnnxAdmission across the
        // doc and query routers so the bound is process-wide, not per-router
        // — wrapping here, once per router, produced a per-router bound
        // instead and was the exact defect this comment replaces).
        this.localEmbedder       = localEmbedder;
        this.voyageCodeEmbedder  = null;
        this.cceEmbedder         = null;
        this.inputType           = inputType;
        this.modelEmbedders      = Map.of(localEmbedder.modelToken(), localEmbedder);
    }

    /**
     * Cloud-mode constructor: routes by collection model segment (prefix for
     * non-conformant names).
     *
     * @param onnxEmbedder  ONNX fallback (used when collection prefix is unrecognised)
     * @param voyageApiKey  Voyage AI API key
     * @param inputType     {@code "document"} or {@code "query"}
     */
    public EmbedderRouter(OnnxEmbedder onnxEmbedder, String voyageApiKey, String inputType) {
        this.localEmbedder      = onnxEmbedder;
        this.voyageCodeEmbedder = new VoyageEmbedder(voyageApiKey, "voyage-code-3", inputType);
        this.cceEmbedder        = new CceEmbedder(voyageApiKey, inputType);
        this.inputType          = inputType;
        // voyage-3 gets its own standard-embed instance: prefix routing sent it
        // to CCE (voyage-context-3) — the same-dim wrong-model contamination
        // hole this dispatch table closes. Every entry is self-keyed by the
        // embedder's own modelToken() so a key can never drift from the
        // identity actually dispatched.
        VoyageEmbedder voyage3 = new VoyageEmbedder(voyageApiKey, "voyage-3", inputType);
        this.modelEmbedders     = Map.of(
                onnxEmbedder.modelToken(),       onnxEmbedder,
                voyageCodeEmbedder.modelToken(), voyageCodeEmbedder,
                cceEmbedder.modelToken(),        cceEmbedder,
                voyage3.modelToken(),            voyage3);
    }

    /**
     * Cloud-mode, VOYAGE-ONLY constructor (nexus-0n7uc): NO local ONNX embedder.
     *
     * <p>The production cloud service embeds exclusively via Voyage; it must not
     * construct any local ONNX embedder. The cloud container has no MiniLM model
     * on disk, and {@code OnnxEmbedder} loads it via {@code OrtEnvironment.
     * createSession(path)} — which onnxruntime SEGFAULTS (does not throw) on a
     * missing file, crashing the engine at boot (conexus STEP-5, conexus-qcn).
     * A voyage-1024 cloud corpus has no legitimate use for a local 384-dim
     * fallback anyway. A non-conformant or {@code minilm-l6-v2-384}-segment
     * collection is REFUSED ({@link EmbeddingModelUnavailableException} → 422),
     * symmetric with how local mode refuses voyage collections.
     *
     * <p>The {@link #EmbedderRouter(OnnxEmbedder, String, String) onnx cloud
     * constructor} is retained for tests / the parity-gate, which run where the
     * MiniLM model exists; production boot (Main) uses THIS constructor.
     *
     * @param voyageApiKey Voyage AI API key
     * @param inputType    {@code "document"} or {@code "query"}
     */
    public EmbedderRouter(String voyageApiKey, String inputType) {
        this.localEmbedder      = null;   // pure-voyage cloud: no local fallback
        this.voyageCodeEmbedder = new VoyageEmbedder(voyageApiKey, "voyage-code-3", inputType);
        this.cceEmbedder        = new CceEmbedder(voyageApiKey, inputType);
        this.inputType          = inputType;
        VoyageEmbedder voyage3 = new VoyageEmbedder(voyageApiKey, "voyage-3", inputType);
        this.modelEmbedders     = Map.of(
                voyageCodeEmbedder.modelToken(), voyageCodeEmbedder,
                cceEmbedder.modelToken(),        cceEmbedder,
                voyage3.modelToken(),            voyage3);
    }

    /**
     * Embedding mode for banners and refusal messages. {@code "onnx-local"} is a
     * cross-language SENTINEL parsed by {@code doctor.py} and
     * {@code storage_service_daemon.py} — do not change the literal. It names the
     * RUNTIME (local ONNX), not the model; the model token is in
     * {@link #availableModels()} (RDR-160 swapped MiniLM-384 → bge-768 there).
     */
    public String modeName() {
        return voyageCodeEmbedder == null ? "onnx-local" : "voyage";
    }

    /** Model tokens this router can embed for (sorted, for stable banner output). */
    public List<String> availableModels() {
        return modelEmbedders.keySet().stream().sorted().toList();
    }

    /**
     * Bead nexus-s71lr, pass 3 — live embed-activity snapshots for {@code GET
     * /v1/status}, keyed by each embedder's own {@link Embedder#modelToken()}
     * (the SAME self-keying {@link #modelEmbedders} already uses, so a key can
     * never drift from the identity actually dispatched). Iterates {@link
     * #modelEmbedders} generically — works unchanged for local mode (one
     * entry, the admission-wrapped bge768) and cloud mode (voyage-code-3 /
     * voyage-context-3 / voyage-3), because {@link Embedder#activitySnapshot()}
     * is a default interface method: an embedder that does not track activity
     * (the MiniLM {@code OnnxEmbedder} fallback, test fakes) is simply absent
     * from the returned map rather than reported with a fabricated value.
     */
    public Map<String, EmbedActivitySnapshot> embedActivitySnapshots() {
        Map<String, EmbedActivitySnapshot> out = new java.util.LinkedHashMap<>();
        for (Map.Entry<String, Embedder> e : modelEmbedders.entrySet()) {
            EmbedActivitySnapshot snap = e.getValue().activitySnapshot();
            if (snap != null) {
                out.put(e.getKey(), snap);
            }
        }
        return out;
    }

    /**
     * RDR-204 Phase 1 (bead nexus-ft04v.6) — this router's content-type to
     * embedding-model-token mapping, keyed directly on the bare content
     * types this router's mode groups together (RDR-204 Phase 2 fix round,
     * nexus-ft04v.16: these keys used to be derived from the collection-name
     * PREFIX constants {@code CCE_PREFIXES}/{@code CODE_PREFIX} via {@code
     * stripTrailingSeparator}; both constants and that helper are deleted —
     * this map is profile-seeding vocabulary, not a collection-name parse
     * site, so it names its own keys rather than reusing name-routing
     * literals that no longer exist elsewhere in this class): "knowledge",
     * "docs", "rdr" share one token (CCE), "code" gets its own, and {@code
     * "unknown"} (bead nexus-ft04v.4's walk sentinel for an unparseable
     * collection name) shares the CCE token — {@code voyage-3} is
     * deliberately absent from {@code nexus.embedding_models} (no client
     * token, no live row — {@code catalog-036-embedding-profile.xml}'s
     * header), so it is not a legal {@code embedding_profile.embedding_model}
     * value, and the CCE bucket already serves the majority (3 of 4) of this
     * router's named content types.
     *
     * <p>Local mode (no Voyage): every content type maps to the single
     * injected local embedder's token (RDR-160: bge-768) — trivially
     * including "unknown", since local mode routes everything through it
     * regardless of content type.
     *
     * @return content type → model token, keyed by "code", "docs", "rdr",
     *         "knowledge", "unknown"
     */
    public Map<String, String> contentTypeModelTokens() {
        String cceToken  = (voyageCodeEmbedder == null) ? localEmbedder.modelToken() : cceEmbedder.modelToken();
        String codeToken = (voyageCodeEmbedder == null) ? localEmbedder.modelToken() : voyageCodeEmbedder.modelToken();

        Map<String, String> tokens = new LinkedHashMap<>();
        tokens.put("knowledge", cceToken);
        tokens.put("docs",      cceToken);
        tokens.put("rdr",       cceToken);
        tokens.put("code",      codeToken);
        tokens.put("unknown",   cceToken);
        return tokens;
    }

    /**
     * RDR-204 Phase 1 (bead nexus-ft04v.6) — upsert this router's ENTIRE
     * content-type → model mapping into {@code nexus.embedding_profile} for
     * {@code tenant}, one row per content type, in a SINGLE transaction.
     *
     * <p>Called by {@code Main} right after the routers are built, for the
     * LOCAL tenant, on EVERY boot (RDR-204 Technical Design step 1a: the
     * engine is the only writer, and a service restart after {@code nx config
     * set local.embed_model}/{@code voyage_api_key} IS the trigger that
     * adopts a mode switch — so this is a real UPSERT, not an insert-once. A
     * second boot in the SAME mode changes no row's content (idempotent); a
     * boot after a mode switch overwrites every row with the new mode's
     * tokens).
     *
     * <p>Post-commit, evicts every {@link CollectionRegistry} entry cached
     * for {@code tenant} — see {@link CollectionRegistry#evictTenant}.
     *
     * @param tenantScope the RLS-stamping gateway ({@code embedding_profile}
     *                    is tenant-scoped with FORCE ROW LEVEL SECURITY)
     * @param tenant      the tenant to seed (the LOCAL tenant at boot; a cloud
     *                    tenant instead calls {@link
     *                    #seedEmbeddingProfileForContentType} lazily — see
     *                    that method's javadoc for the reusable seam)
     */
    public void seedEmbeddingProfile(TenantScope tenantScope, String tenant) {
        Map<String, String> tokens = contentTypeModelTokens();
        tenantScope.withTenant(tenant, ctx -> {
            for (Map.Entry<String, String> e : tokens.entrySet()) {
                upsertProfileRow(ctx, tenant, e.getKey(), e.getValue());
            }
            return null;
        });
        CollectionRegistry.evictTenant(tenant);
    }

    /**
     * RDR-204 Phase 1 (bead nexus-ft04v.6) — the LAZY, per-cloud-tenant SEAM:
     * upserts exactly ONE {@code nexus.embedding_profile} row, for {@code
     * contentType} only, from this router's own mapping.
     *
     * <p>The engine has no tenant-mint route (tenants exist through
     * data-token mint at the edge), so a cloud tenant's profile cannot be
     * seeded at boot the way the local tenant's is by {@link
     * #seedEmbeddingProfile}. This method is meant to be called at that
     * tenant's FIRST registration for {@code contentType} (bead
     * nexus-ft04v.8's future {@code register_collection} wiring) and is
     * idempotent by the SAME upsert shape as the boot path — no separate
     * existence check is needed before calling it; the upsert itself is the
     * idempotency check, and calling it again for a content type already
     * profiled simply overwrites it with the SAME mode-derived tokens.
     *
     * <p>THE REUSABLE SEAM: bead nexus-ft04v.3's per-tenant,
     * first-request-after-boot ghost sweep already gates its OWN one-time
     * work behind a {@code CatalogRepository.setMeta}/{@code getMeta}
     * marker for that tenant. That sweep can call {@link
     * #seedEmbeddingProfile} (the ALL-content-types boot method, not this
     * one) for the same tenant inside its OWN first-request gate, rather than
     * inventing a second marker — neither seed method needs one of its own,
     * because the UPSERT is the idempotency check, not a marker read.
     *
     * <p>Content types are free-form on the client (bead nexus-ft04v.8 fix,
     * coordinator-reported regression against the primary's Python suite,
     * 2026-09-07): a caller may register any string, including one this
     * router's mode has no dedicated mapping for (a test fixture's
     * {@code "prose"}, a {@code quarantine-<ct>} content type, or any other
     * caller-invented value). Such a content type gets the SAME CCE bucket
     * token {@code "unknown"} gets (bge-768 in ONNX mode, {@code
     * voyage-context-3} in Voyage mode) — never a hard refusal. Registration
     * is a request the engine must be able to honour; the 422 this profile
     * enables (bead nexus-ft04v.8) is reserved for a request that NAMES a
     * model disagreeing with the profile, never for an unrecognised content
     * type.
     *
     * @param tenantScope the RLS-stamping gateway
     * @param tenant      the tenant being registered for
     * @param contentType the content type of the collection being registered
     *                    — any string; one of this router's mapped types
     *                    (or {@code "unknown"}, see {@link
     *                    #contentTypeModelTokens}) gets its own token, any
     *                    other value falls back to the {@code "unknown"}
     *                    token
     */
    public void seedEmbeddingProfileForContentType(
            TenantScope tenantScope, String tenant, String contentType) {
        Map<String, String> tokens = contentTypeModelTokens();
        String modelToken = tokens.get(contentType);
        if (modelToken == null) {
            modelToken = tokens.get("unknown");
        }
        String finalModelToken = modelToken;
        tenantScope.withTenant(tenant, ctx -> {
            upsertProfileRow(ctx, tenant, contentType, finalModelToken);
            return null;
        });
        CollectionRegistry.evictTenant(tenant);
    }

    /**
     * The single upsert primitive both seed methods share. Derives {@code
     * dimension} from {@code nexus.embedding_models} (a SELECT first, then
     * the write) — the same table {@link CollectionRegistry#require} now
     * COALESCEs a NULL {@code catalog_collections.dimension} from (RDR-204
     * Phase 2, bead nexus-ft04v.16 — the model → dimension mapping this class
     * and {@code PgVectorRepository} used to each duplicate as a hand-typed
     * {@code MODEL_DIMS}/similar map is retired; this table is the one place
     * that mapping lives now). {@code ON CONFLICT (tenant_id, content_type)
     * DO UPDATE} — a real
     * upsert, not {@code DO NOTHING} — so a mode switch's next boot actually
     * overwrites a stale row rather than leaving it pinned to whichever mode
     * first wrote it.
     *
     * @throws IllegalStateException if {@code modelToken} has no {@code
     *         nexus.embedding_models} row — every token an {@link
     *         EmbedderRouter} mode can produce must be one of the four
     *         seeded models ({@code catalog-036-embedding-profile.xml})
     */
    private static void upsertProfileRow(
            DSLContext ctx, String tenant, String contentType, String modelToken) {
        Integer dimension = ctx.select(EMBEDDING_MODELS.DIMENSION)
                .from(EMBEDDING_MODELS)
                .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(modelToken))
                .fetchOne(EMBEDDING_MODELS.DIMENSION);
        if (dimension == null) {
            throw new IllegalStateException(
                "embedding_model '" + modelToken + "' has no nexus.embedding_models row — "
                + "every token an EmbedderRouter mode can produce must be one of the four "
                + "seeded models (catalog-036-embedding-profile.xml)");
        }
        ctx.insertInto(EMBEDDING_PROFILE,
                       EMBEDDING_PROFILE.TENANT_ID, EMBEDDING_PROFILE.CONTENT_TYPE,
                       EMBEDDING_PROFILE.EMBEDDING_MODEL, EMBEDDING_PROFILE.DIMENSION)
           .values(tenant, contentType, modelToken, dimension)
           .onConflict(EMBEDDING_PROFILE.TENANT_ID, EMBEDDING_PROFILE.CONTENT_TYPE)
           .doUpdate()
           .set(EMBEDDING_PROFILE.EMBEDDING_MODEL, modelToken)
           .set(EMBEDDING_PROFILE.DIMENSION, dimension)
           .execute();
    }

    /**
     * Embed texts for a specific collection — picks the correct embedder by reading
     * the collection's {@code catalog_collections} row (RDR-204 Phase 2, bead
     * nexus-ft04v.16; see {@link #resolveEmbedderStrict}).
     *
     * @param scope      the RLS-stamping gateway, used only on a {@link
     *                   CollectionRegistry} cache miss
     * @param tenant     the tenant that owns {@code collection}
     * @param collection collection name, used for routing
     * @param texts      texts to embed
     * @return embedding vectors aligned with input
     */
    public List<float[]> embedForCollection(
            TenantScope scope, String tenant, String collection, List<String> texts) {
        Embedder embedder = resolveEmbedderStrict(scope, tenant, collection);
        log.debug("event=embed_router collection={} embedder={} count={}",
                collection, embedder.getClass().getSimpleName(), texts.size());
        return embedder.embed(texts);
    }

    /**
     * Default {@link Embedder#embed} — uses ONNX (local mode default).
     * Prefer {@link #embedForCollection} when a collection name is available.
     */
    @Override
    public List<float[]> embed(List<String> texts) {
        if (localEmbedder == null) {
            // pure-voyage cloud (nexus-0n7uc): no local default embedder. Callers
            // must route by collection name (embedForCollection) so the model is
            // resolved per RDR-103, never embedded with an unintended model.
            throw new EmbeddingModelUnavailableException(
                "voyage-only cloud service has no local default embedder; embed via "
                + "a collection (embedForCollection) so the model is routed by name. "
                + "Available models: " + availableModels());
        }
        return localEmbedder.embed(texts);
    }

    /**
     * Embed a single text for a specific collection.
     */
    public float[] embedOneForCollection(
            TenantScope scope, String tenant, String collection, String text) {
        return embedForCollection(scope, tenant, collection, List.of(text)).get(0);
    }

    /**
     * Embed texts for a specific collection and return both the vectors and the
     * token count consumed by the embedding call (bead nexus-ehc4q).
     *
     * <p>Routes to the same embedder as {@link #embedForCollection}, then
     * delegates to {@link Embedder#embedWithUsage} to capture the token count.
     * No second embed call — the token count comes from the same API response.
     *
     * @param scope      the RLS-stamping gateway, used only on a {@link
     *                   CollectionRegistry} cache miss
     * @param tenant     the tenant that owns {@code collection}
     * @param collection collection name, used for routing
     * @param texts      texts to embed
     * @return {@link EmbedResult} carrying vectors aligned with input and the token count
     */
    public EmbedResult embedForCollectionWithUsage(
            TenantScope scope, String tenant, String collection, List<String> texts) {
        Embedder embedder = resolveEmbedderStrict(scope, tenant, collection);
        log.debug("event=embed_router_with_usage collection={} embedder={} count={}",
                collection, embedder.getClass().getSimpleName(), texts.size());
        return embedder.embedWithUsage(texts);
    }

    /**
     * Embed a single text for a specific collection, returning the vector and token count
     * (bead nexus-ehc4q). Convenience wrapper over {@link #embedForCollectionWithUsage}.
     */
    public EmbedResult embedOneForCollectionWithUsage(
            TenantScope scope, String tenant, String collection, String text) {
        return embedForCollectionWithUsage(scope, tenant, collection, List.of(text));
    }

    /**
     * Embed texts for a collection, preserving full double (float64) precision.
     *
     * <p>Used by the parity gate ({@code /v1/vectors/embed}) to avoid the float32
     * round-trip that causes cosine ≈ 0.9999669 instead of 1.0 exactly.
     *
     * <p>For ONNX (float32 output), converts float32 → double exactly.
     * For Voyage/CCE, calls the embedder's {@code embedDouble} method to preserve
     * the original JSON double values without float32 truncation.
     */
    public List<double[]> embedDoubleForCollection(
            TenantScope scope, String tenant, String collection, List<String> texts) {
        Embedder embedder = resolveEmbedderStrict(scope, tenant, collection);
        log.debug("event=embed_double_router collection={} embedder={} count={}",
                collection, embedder.getClass().getSimpleName(), texts.size());

        if (embedder instanceof VoyageEmbedder ve) {
            return ve.embedDouble(texts);
        }
        if (embedder instanceof CceEmbedder ce) {
            return ce.embedDouble(texts);
        }
        // ONNX: float32 → double is exact (no precision loss)
        List<float[]> floatVecs = embedder.embed(texts);
        List<double[]> result = new ArrayList<>(floatVecs.size());
        for (float[] fv : floatVecs) {
            double[] dv = new double[fv.length];
            for (int i = 0; i < fv.length; i++) dv[i] = fv[i];
            result.add(dv);
        }
        return result;
    }

    /**
     * Strict, registry-row-authoritative resolution (RDR-204 Phase 2, bead
     * nexus-ft04v.16 — supersedes bead nexus-pebfx.2's model-SEGMENT reading of
     * this same invariant, nexus-0n7uc: the model decides the embedder, never a
     * string parsed from the collection's own name).
     *
     * <p>Reads {@code collection}'s {@code catalog_collections} row via {@link
     * CollectionRegistry#lookup} and dispatches by the row's {@code
     * embedding_model} — never by a segment split out of {@code collection}
     * itself, so a collection whose NAME disagrees with its registered row (a
     * grandfathered rename, a name that predates a profile change) still routes
     * to the model the row actually says. Model identity is validated by
     * construction: the dispatched embedder's {@link Embedder#modelToken()}
     * keys the table, so a same-dimension wrong-model embed cannot happen. A
     * row whose model has no embedder in this mode is REFUSED loudly instead of
     * silently embedded with the wrong model (no-silent-fallbacks-for-correctness):
     * <ul>
     *   <li>onnx-local mode + a {@code voyage-*} row → refuse (no credentials)
     *   <li>any mode + a row with no embedder wired in this mode → refuse
     *       (e.g. {@code minilm-l6-v2-384} on the RDR-160 bge-768 local service,
     *       or {@code bge-base-en-v15-768} in a MiniLM-wired test router)
     *   <li>pure-voyage cloud (PRODUCTION) + a {@code minilm-l6-v2-384} row →
     *       refuse (422): no ONNX in cloud (nexus-0n7uc)
     *   <li>onnx-cloud (TESTS / parity-gate only) + a {@code minilm-l6-v2-384} row
     *       → ONNX (the row is the authority; prefix routing wrongly sent these
     *       to CCE)
     * </ul>
     *
     * <p>A non-null collection ALWAYS goes through the registry now — a name
     * that would previously have fallen through to prefix routing
     * (non-4-segment, or with no model segment at all) must be registered
     * exactly like any other collection (RDR-204 Phase 1's
     * universal-registration requirement); an unregistered one throws {@link
     * dev.nexus.service.db.UnregisteredCollectionException}. RDR-204 Phase 2
     * fix round (nexus-ft04v.16): a NULL {@code collection} is no longer a
     * defined input to this method — no production caller ever passed one
     * (that claim, and the legacy prefix-routing fallback it justified, were
     * both dead code; the {@code /v1/vectors/embed} route this method's
     * javadoc used to cite as "collection-less" always requires a {@code
     * collection} string). A caller with no collection to resolve an
     * embedder for — the embed-only parity route naming a model directly —
     * uses {@link #resolveEmbedderByModel} instead.
     *
     * @param scope      the RLS-stamping gateway, used only on a {@link
     *                   CollectionRegistry} cache miss
     * @param tenant     the tenant that owns {@code collection}
     * @param collection the collection to resolve an embedder for (never null)
     * @throws EmbeddingModelUnavailableException when the row's model cannot be
     *         served in the current mode (→ HTTP 422): "this install's profile
     *         names a model this mode cannot serve"
     * @throws dev.nexus.service.db.UnregisteredCollectionException when {@code
     *         collection} has no {@code catalog_collections} row
     */
    public Embedder resolveEmbedderStrict(TenantScope scope, String tenant, String collection) {
        CollectionRow row = CollectionRegistry.lookup(scope, tenant, collection);
        Embedder embedder = modelEmbedders.get(row.embeddingModel());
        if (embedder == null) {
            throw new EmbeddingModelUnavailableException(
                "this install's profile names a model this mode cannot serve — collection '"
                + collection + "' resolves to model '" + row.embeddingModel() + "', which "
                + "embedding mode " + modeName() + " has no embedder for. Available models: "
                + availableModels()
                + ("onnx-local".equals(modeName())
                   ? ". Voyage collections need NX_VOYAGE_API_KEY in the service "
                     + "environment (supervisor plumbs it from the nexus credential "
                     + "chain when set)."
                   : "."));
        }
        return embedder;
    }

    /**
     * Resolve an embedder directly by MODEL TOKEN, with no collection to look
     * a row up by (RDR-204 Phase 2 fix round, nexus-ft04v.16).
     *
     * <p>{@code POST /v1/vectors/embed} (bead nexus-gmiaf.21's embed-only
     * parity/comparison route — it never stores anything) accepts EITHER a
     * {@code collection} (routed through {@link #resolveEmbedderStrict},
     * registry-authoritative) OR a {@code model} naming the embedder
     * directly — the two are mutually exclusive at the HTTP layer ({@link
     * dev.nexus.service.http.VectorHandler#handleEmbed}). This method is the
     * dispatch for the second case: no collection exists (or none is
     * relevant) and the caller wants a SPECIFIC model's output, e.g. to
     * compare Java against a reference implementation for that model
     * directly, independent of any collection's registration state.
     *
     * @param modelToken an {@link Embedder#modelToken()} value, e.g.
     *                   {@code "voyage-code-3"} or {@code "bge-base-en-v15-768"}
     * @throws EmbeddingModelUnavailableException when {@code modelToken} has
     *         no embedder wired in this mode (→ HTTP 422)
     */
    public Embedder resolveEmbedderByModel(String modelToken) {
        Embedder embedder = modelEmbedders.get(modelToken);
        if (embedder == null) {
            throw new EmbeddingModelUnavailableException(
                "this install's profile names a model this mode cannot serve — model '"
                + modelToken + "', which embedding mode " + modeName() + " has no embedder for. "
                + "Available models: " + availableModels()
                + ("onnx-local".equals(modeName())
                   ? ". Voyage models need NX_VOYAGE_API_KEY in the service "
                     + "environment (supervisor plumbs it from the nexus credential "
                     + "chain when set)."
                   : "."));
        }
        return embedder;
    }

    @Override
    public void close() {
        // localEmbedder is null in pure-voyage cloud mode (nexus-0n7uc).
        if (localEmbedder != null) {
            try { localEmbedder.close(); } catch (Exception ignored) {}
        }
        // VoyageEmbedder instances are stateless HTTP clients; no close needed.
        // CceEmbedder (nexus-9okyk) owns a bounded virtual-thread executor for its
        // parallel per-chunk fan-out — must be shut down with this router.
        if (cceEmbedder != null) {
            try { cceEmbedder.close(); } catch (Exception ignored) {}
        }
    }
}
