// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;

/**
 * What a collection registration (or an ETL import row) must say about the collection's own attributes, decided
 * here so {@link CatalogRepository} (over its size ceiling) does not grow (RDR-229 P2.5, bead nexus-p2m1x.10).
 *
 * <p><strong>lifecycle_state.</strong> Registration takes an explicit {@code lifecycle_state} and defaults it to
 * {@code live}; it never reads the name. The import paths take the imported row's own state and fall back to the
 * {@code quarantine-} prefix only for an export that predates the column.
 *
 * <p><strong>model_version (D1, Sam 2026-11-20).</strong> A registration of a NEW row with a blank or absent
 * {@code model_version} is refused ({@link IllegalArgumentException}, a 400 on the route) and writes nothing. An
 * import takes the version the row carries, else the {@code vN} of a conformant name, else refuses. The
 * conformant-name read is the one place the engine parses a name for a version: it is a migration source (an
 * export from another install or an older era, RDR-229 item 1), and {@code CollectionParseGateTest} declares it.
 */
public final class CollectionRegistration {

    /** {@code <content_type>__<owner>__<embedding_model>__v<n>}, as the client's {@code _CONFORMANT_COLLECTION_RE}. */
    private static final Pattern CONFORMANT_VERSION =
        Pattern.compile("^[a-z][a-z0-9-]*__.+__([a-z][a-z0-9-]*)__(v[0-9]+)$");

    private CollectionRegistration() {
    }

    /**
     * The {@code vN} suffix of a conformant collection name, or {@code null} when the name is not conformant. The
     * ONE reader of a version out of a name: a conformant name, migration source (RDR-229 item 1) and the reembed
     * target's minted name; retired at P4 with the name parsers. Callers: the ETL import fallback
     * ({@link #importModelVersion}) and {@code ReembedHandler.register}.
     */
    public static String versionOfConformantName(String name) {
        if (name == null) {
            return null;
        }
        Matcher m = CONFORMANT_VERSION.matcher(name);
        return m.matches() ? m.group(2) : null;
    }

    /**
     * The embedding-model token of a conformant collection name, or {@code null} when the name is not conformant.
     * The same migration-source read as {@link #versionOfConformantName} (the one regex, so the declared parse-site
     * count is unchanged), for one caller: {@code TargetSelection.foreignOrigin}, which reads the model of a re-embed
     * job's source whose registry row is gone (a default cutover deletes it). A model never changes on a collection,
     * so the name's token is what the row held. Retired with the name parsers at RDR-229 P4; the caller then treats
     * a gone row's model as unknown, which refuses.
     *
     * <p><b>Census:</b> a migration-source name-read site ("a re-embed source row deleted by cutover; retires at
     * P4"). The engine's parse census counts string literals per file, not call sites, and this reuses the version
     * regex, so it adds no literal; {@code NameModelReadCensusTest.NAME_MODEL_READ_CALLS} pins its callers instead.
     * P4.3: delete the call, then that pin.
     */
    public static String modelOfConformantName(String name) {
        if (name == null) {
            return null;
        }
        Matcher m = CONFORMANT_VERSION.matcher(name);
        return m.matches() ? m.group(1) : null;
    }

    /** The row's {@code lifecycle_state}, or {@code null} when absent or blank; a value outside the vocabulary is refused. */
    static String explicitLifecycleState(Map<String, Object> coll) {
        String requested = CatalogRepository.s(coll, "lifecycle_state");
        if (requested == null || requested.isBlank()) {
            return null;
        }
        if (!CatalogRepository.LIFECYCLE_STATES.contains(requested)) {
            throw new IllegalArgumentException("lifecycle_state '" + requested + "' is not one of "
                + new java.util.TreeSet<>(CatalogRepository.LIFECYCLE_STATES) + " (collection '"
                + CatalogRepository.s(coll, "name") + "')");
        }
        return requested;
    }

    /**
     * The import row's {@code lifecycle_state}: its own when it carries one, else the prefix rule (migration fallback).
     * A row carrying {@code live} against prefix evidence imports as {@code quarantine} ({@link
     * #stateCorrectedOnImport}).
     */
    static String lifecycleStateForImport(Map<String, Object> coll) {
        if (stateCorrectedOnImport(coll)) {
            return "quarantine";
        }
        String explicit = explicitLifecycleState(coll);
        if (explicit != null) {
            return explicit;
        }
        return hasImportPrefixEvidence(coll) ? "quarantine" : "live";
    }

    /**
     * True for an import row that carries {@code live} while its name or content_type starts {@code quarantine-}: it
     * imports as {@code quarantine} instead of being refused, and the import names it (the route's
     * {@code state_corrected} field and one WARN line per row; linda 2026-11-20).
     *
     * <p>Where such a row can come from (read 2026-11-20, so the next reader need not redo it):
     * <ul>
     *   <li>A {@code quarantine-} content_type had one writer: hygiene-002-2's {@code gc_quarantine_orphans} took
     *       {@code v_seg[1]}, the first {@code __} segment of the sibling NAME, and inserted it with
     *       {@code 'quarantine'} (nexus-uxd2a). So that content_type only ever sat under a {@code quarantine-} name,
     *       and every registration path before P2.5 (upsertCollection, this import, the SQL sibling insert) stored
     *       {@code 'quarantine'} for a new row whose name or content_type carried the prefix.</li>
     *   <li>A plain name could only inherit a {@code quarantine-} content_type through a copying rename, and rename
     *       refuses a quarantine source (the name guard before P2.5, the row since). So the plain-name shape has no
     *       known producer; it is handled the same way so an unknown export cannot strand a batch.</li>
     *   <li>The real shape: between engine v0.1.109 and v0.1.111 the ghost sweep demoted quarantine siblings to
     *       {@code dormant} and could revive one to {@code live} (its dormant-to-live arm).
     *       hygiene-005-3 promoted back only rows still {@code dormant} ({@code AND c.lifecycle_state = 'dormant'}),
     *       so a revived sibling stayed {@code live} under its {@code quarantine-} name. P1.2's census found none on
     *       either production tenant; it cannot see local installs or their old exports.</li>
     * </ul>
     *
     * <p>Only the import route reaches this ({@code POST /v1/catalog/import/collection}): no engine path and no
     * released client calls it, and it retires with RDR-187 .11, so the WARN line is the visible record in practice.
     */
    static boolean stateCorrectedOnImport(Map<String, Object> coll) {
        return "live".equals(explicitLifecycleState(coll)) && hasImportPrefixEvidence(coll);
    }

    /**
     * The import path's prefix evidence: the row's name or its content_type starts {@code quarantine-}. It is both the
     * state fallback for a row that carries none and what {@link #requireStatesAgreeWithNames} checks a carried state
     * against (a migration source, RDR-229 item 9; a named residual of item 2).
     */
    private static boolean hasImportPrefixEvidence(Map<String, Object> coll) {
        String contentType = CatalogRepository.s(coll, "content_type");
        String name = CatalogRepository.s(coll, "name");
        return (contentType != null && contentType.startsWith(QuarantineOrigin.PREFIX))
            || (name != null && name.startsWith(QuarantineOrigin.PREFIX));
    }

    /**
     * True for the two real contradictions only: a {@code quarantine-} name with state {@code live}, and a plain name
     * with state {@code quarantine}. {@code dormant} and {@code disputed} are admin states orthogonal to the name
     * (hygiene-002 marks rows {@code disputed}, and an export holding one must still import).
     */
    private static boolean disagrees(String name, String state) {
        return disagrees(name != null && name.startsWith(QuarantineOrigin.PREFIX), state);
    }

    private static boolean disagrees(boolean prefixed, String state) {
        return prefixed ? "live".equals(state) : "quarantine".equals(state);
    }

    /**
     * Refuses a NEW registration, in every form, whose name and effective {@code lifecycle_state} contradict (review
     * N1): a {@code quarantine-} name is never {@code live}, and a plain name is never {@code quarantine}. The engine's own
     * mint passes {@code quarantine} for its prefixed sibling, so it is unaffected; the SQL movers write their rows
     * directly and do not pass through here.
     */
    static void requireNameAgreesWithState(String name, String effectiveState) {
        if (disagrees(name, effectiveState)) {
            throw new IllegalArgumentException("registering collection '" + name + "' with lifecycle_state '"
                + effectiveState + "' is refused: a quarantine- name cannot be 'live', and any other name cannot "
                + "be 'quarantine'");
        }
    }

    /**
     * Refuses an import batch holding a NEW row whose EFFECTIVE {@code lifecycle_state} ({@link
     * #lifecycleStateForImport}: the row's own, else the prefix fallback) contradicts the row's prefix evidence. On
     * this path the evidence is the name OR the content_type carrying {@code quarantine-}, the same two disjuncts the
     * fallback reads. After {@link #stateCorrectedOnImport} a prefixed row is never {@code live}, so what is refused is
     * an unprefixed row with an explicit {@code quarantine}: it has no legacy evidence behind it.
     *
     * <p>A plain name with a {@code quarantine-} content_type and no state therefore lands as {@code quarantine}. An
     * imported legacy row is a migration source, and RDR-229 item 9 forbids stranding it; P2.1's
     * {@code CatalogEtlImportNonStrandingTest} is the pre-declared check of that shape (linda 2026-11-20, reversing her
     * round-1 ruling for the import path, which had refused it). It stays safe because lifecycle is read from the
     * row: such a row behaves as quarantine everywhere. It is a named residual of RDR-229 item 2. The client route,
     * {@link #requireNameAgreesWithState} and the re-embed check keep the name-only pairing.
     *
     * <p>Loud and whole: the import's response is a bare count with no per-row report, so the batch is refused with one
     * {@link IllegalArgumentException} (a 400) that counts and names the rows, and nothing is stored, as the
     * content_type guard already refuses a batch.
     */
    static void requireStatesAgreeWithNames(List<Map<String, Object>> newRows) {
        List<String> bad = new java.util.ArrayList<>();
        for (Map<String, Object> coll : newRows) {
            String state = lifecycleStateForImport(coll);
            String name = CatalogRepository.s(coll, "name");
            if (disagrees(hasImportPrefixEvidence(coll), state)) {
                bad.add("'" + name + "' (lifecycle_state=" + state + ")");
            }
        }
        if (!bad.isEmpty()) {
            throw new IllegalArgumentException("refusing the import: " + bad.size() + " row(s) whose lifecycle_state "
                + "has no quarantine- evidence behind it (a row whose name and content_type are both plain cannot be "
                + "'quarantine'): "
                + String.join(", ", bad.size() > 10 ? bad.subList(0, 10) : bad) + (bad.size() > 10 ? ", ..." : ""));
        }
    }

    /**
     * The client route ({@code POST /v1/catalog/collections/upsert}) registers {@code live} collections only
     * (linda 2026-11-20, rule A): an explicit state other than {@code live} is refused. Engine-internal callers of
     * {@code upsertCollection} do not pass through here and may name any state.
     */
    static void requireClientState(String requestedState, String name) {
        if (requestedState != null && !"live".equals(requestedState)) {
            throw new IllegalArgumentException("lifecycle_state '" + requestedState + "' is not accepted when "
                + "registering collection '" + name + "': this route registers live collections only (send "
                + "'live' or no lifecycle_state); the engine creates quarantine rows itself");
        }
    }

    /**
     * Rule B (linda 2026-11-20): a NEW client registration may not start with the {@code quarantine-} prefix. The
     * engine mints that name (the reaper, the SQL movers); this guard stops a client minting one.
     */
    static void requireClientNameNotQuarantine(String name) {
        if (name != null && name.startsWith(QuarantineOrigin.PREFIX)) {
            throw new IllegalArgumentException("registering collection '" + name + "' is refused: the quarantine- "
                + "name prefix is the engine's own; a client cannot register a collection under it");
        }
    }

    /** The model and vector width a NEW collection row binds (see {@link #resolveNewRowModel}). */
    record ModelChoice(String model, Integer dimension) {
    }

    /**
     * The embedding model and dimension of a NEW row of {@code contentType} for {@code tenant}: the tenant's embedding
     * profile for the content type when it has one (a request naming another model is refused), else the requested
     * model with its registered dimension. {@code label} names the collection in the refusal messages. Shared by
     * {@code upsertCollection} (named) and {@link CollectionMint} (minted) so the two cannot drift.
     *
     * @throws CatalogRepository.EmbeddingProfileConflictException a 422: the request disagrees with the profile, or no
     *         model is named and no profile exists
     */
    static ModelChoice resolveNewRowModel(DSLContext ctx, String tenant, String contentType, String requestedModel,
                                          String label) {
        String profileModel = null;
        Integer profileDimension = null;
        var profileRow = ctx.select(EMBEDDING_PROFILE.EMBEDDING_MODEL, EMBEDDING_PROFILE.DIMENSION)
            .from(EMBEDDING_PROFILE)
            .where(EMBEDDING_PROFILE.TENANT_ID.eq(tenant))
            .and(EMBEDDING_PROFILE.CONTENT_TYPE.eq(contentType))
            .fetchOne();
        if (profileRow != null) {
            profileModel = profileRow.value1();
            profileDimension = profileRow.value2();
        }
        if (profileModel != null) {
            if (requestedModel != null && !requestedModel.isBlank() && !requestedModel.equals(profileModel)) {
                throw new CatalogRepository.EmbeddingProfileConflictException(
                    "the embedding profile for content_type '" + contentType + "' is '"
                    + profileModel + "'; refusing to register '" + label
                    + "' with embedding_model '" + requestedModel + "'");
            }
            return new ModelChoice(profileModel, profileDimension);
        }
        String model = requestedModel == null ? "" : requestedModel;
        // nexus-ft04v.8 review Significant 3: no embedding profile exists for this content_type and the caller supplied
        // no model either -- refuse loud, naming the missing field, rather than binding "" into the INSERT's VALUES
        // list and letting hygiene-002-1's catalog_collections_embedding_model_chk surface as an opaque 23514.
        if (model.isBlank()) {
            throw new CatalogRepository.EmbeddingProfileConflictException(
                "registering collection '" + label + "' requires embedding_model; "
                + "no embedding profile exists for content_type '" + contentType
                + "' and none was supplied");
        }
        Integer dimension = ctx.select(EMBEDDING_MODELS.DIMENSION)
            .from(EMBEDDING_MODELS)
            .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(model))
            .fetchOne(EMBEDDING_MODELS.DIMENSION);
        return new ModelChoice(model, dimension);
    }

    /** The {@code model_version} a NEW registration sends; blank or absent is refused. */
    static String requiredModelVersion(Map<String, Object> coll) {
        String v = CatalogRepository.s(coll, "model_version");
        if (v == null || v.isBlank()) {
            throw new IllegalArgumentException("registering collection '" + CatalogRepository.s(coll, "name")
                + "' requires model_version; none was supplied");
        }
        return v;
    }

    /** The {@code model_version} an import row gets: its own, else its conformant name's {@code vN}, else refused. */
    static String importModelVersion(Map<String, Object> coll) {
        String v = CatalogRepository.s(coll, "model_version");
        if (v != null && !v.isBlank()) {
            return v;
        }
        String name = CatalogRepository.s(coll, "name");
        String fromName = versionOfConformantName(name);
        if (fromName == null) {
            throw new IllegalArgumentException("importing collection '" + name + "' requires model_version: the row "
                + "carries none and its name is not a conformant collection name (content type, owner, model and version "
                + "segments) to read one from");
        }
        return fromName;
    }
}
