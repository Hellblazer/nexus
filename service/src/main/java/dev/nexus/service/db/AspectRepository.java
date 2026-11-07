package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import static dev.nexus.service.db.JsonbSupport.jsonbOrNull;
import static dev.nexus.service.jooq.nexus.Tables.*;
import static org.jooq.impl.DSL.*;

/**
 * RDR-152 bead nexus-gmiaf.15 — repository for aspects, highlights and promotion-log.
 *
 * <p>Mirrors three SQLite stores in the Postgres service tier:
 * <ul>
 *   <li>{@code document_aspects} — per-document structured aspect records (RDR-089)</li>
 *   <li>{@code document_highlights} — DEVONthink highlight/mention notes (RDR-139 Layer E)</li>
 *   <li>{@code aspect_promotion_log} — extras→column promotion audit log</li>
 * </ul>
 *
 * <p>All methods route through {@link TenantScope#withTenant} for RLS enforcement.
 *
 * <p>The durable extraction queue is not here: the table {@code aspect_extraction_queue} was retired
 * (nexus-hbnml, RDR-235 step B, changeset {@code aspects-005-2}); extraction work rides the engine's
 * {@code aspects} tuple subspace.
 */
public final class AspectRepository {

    private static final Logger log = LoggerFactory.getLogger(AspectRepository.class);

    static final DateTimeFormatter UTC_SECOND =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSSSS'Z'")
                             .withZone(ZoneOffset.UTC);

    /** Fallback formatter for second-precision ISO strings without fractional seconds. */
    static final DateTimeFormatter UTC_SECOND_NO_FRAC =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                             .withZone(ZoneOffset.UTC);

    /** Minimum confidence threshold (mirrors Python _MIN_CONFIDENCE = 0.3). */
    private static final double MIN_CONFIDENCE = 0.3;

    // EXCLUDED pseudo-table fields used in ON CONFLICT DO UPDATE SET, typed via
    // DSL.excluded(Field) (nexus-mzuj9: replaces the hand-built field("EXCLUDED.x", ...)
    // raw-string fragments — jOOQ codegen now type-checks every column reference here
    // against the generated DOCUMENT_ASPECTS / DOCUMENT_HIGHLIGHTS
    // tables, so a Liquibase column rename fails at COMPILE time instead of at runtime).
    private static final Field<String>          EX_PROBLEM_FORMULATION    = excluded(DOCUMENT_ASPECTS.PROBLEM_FORMULATION);
    private static final Field<String>          EX_PROPOSED_METHOD        = excluded(DOCUMENT_ASPECTS.PROPOSED_METHOD);
    private static final Field<String>          EX_EXPERIMENTAL_DATASETS  = excluded(DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS);
    private static final Field<String>          EX_EXPERIMENTAL_BASELINES = excluded(DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES);
    private static final Field<String>          EX_EXPERIMENTAL_RESULTS   = excluded(DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS);
    private static final Field<JSONB>           EX_EXTRAS                 = excluded(DOCUMENT_ASPECTS.EXTRAS);
    private static final Field<Double>          EX_CONFIDENCE             = excluded(DOCUMENT_ASPECTS.CONFIDENCE);
    private static final Field<OffsetDateTime>  EX_EXTRACTED_AT           = excluded(DOCUMENT_ASPECTS.EXTRACTED_AT);
    private static final Field<String>          EX_MODEL_VERSION          = excluded(DOCUMENT_ASPECTS.MODEL_VERSION);
    private static final Field<String>          EX_EXTRACTOR_NAME         = excluded(DOCUMENT_ASPECTS.EXTRACTOR_NAME);
    private static final Field<String>          EX_SOURCE_URI             = excluded(DOCUMENT_ASPECTS.SOURCE_URI);
    private static final Field<JSONB>           EX_SALIENT_SENTENCES      = excluded(DOCUMENT_ASPECTS.SALIENT_SENTENCES);
    private static final Field<String>          EX_DOC_ID                 = excluded(DOCUMENT_ASPECTS.DOC_ID);

    // COALESCE(EXCLUDED.x, table.x) fields for importAspect path
    private static final Field<String>          EX_SOURCE_URI_COALESCE =
        coalesce(excluded(DOCUMENT_ASPECTS.SOURCE_URI), DOCUMENT_ASPECTS.SOURCE_URI);
    private static final Field<JSONB>           EX_SALIENT_COALESCE =
        coalesce(excluded(DOCUMENT_ASPECTS.SALIENT_SENTENCES), DOCUMENT_ASPECTS.SALIENT_SENTENCES);
    private static final Field<String>          EX_DOC_ID_COALESCE =
        coalesce(excluded(DOCUMENT_ASPECTS.DOC_ID), DOCUMENT_ASPECTS.DOC_ID);

    // document_highlights EXCLUDED fields
    private static final Field<String>          EX_HL_SOURCE_URI   = excluded(DOCUMENT_HIGHLIGHTS.SOURCE_URI);
    private static final Field<String>          EX_HL_COLLECTION   = excluded(DOCUMENT_HIGHLIGHTS.COLLECTION);
    private static final Field<String>          EX_HL_HIGHLIGHTS   = excluded(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD);
    private static final Field<String>          EX_HL_MENTIONS     = excluded(DOCUMENT_HIGHLIGHTS.MENTIONS_MD);
    private static final Field<OffsetDateTime>  EX_HL_INGESTED_AT  = excluded(DOCUMENT_HIGHLIGHTS.INGESTED_AT);

    private final TenantScope tenantScope;

    public AspectRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ── Timestamp helpers ──────────────────────────────────────────────────────

    static OffsetDateTime parseTs(String iso) {
        if (iso == null || iso.isBlank()) return OffsetDateTime.now(ZoneOffset.UTC);
        // Try fractional-seconds form first; fall back to second-precision.
        try {
            return OffsetDateTime.parse(iso, DateTimeFormatter.ISO_OFFSET_DATE_TIME);
        } catch (DateTimeParseException e1) {
            try {
                return OffsetDateTime.parse(iso,
                    DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                                     .withZone(ZoneOffset.UTC));
            } catch (DateTimeParseException e2) {
                return OffsetDateTime.now(ZoneOffset.UTC);
            }
        }
    }

    static String formatTs(OffsetDateTime dt) {
        if (dt == null) return null;
        return dt.atZoneSameInstant(ZoneOffset.UTC).format(
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSSSS'Z'"));
    }

    // ── document_aspects ───────────────────────────────────────────────────────

    /**
     * Upsert an aspect record.
     *
     * <p>Confidence gate: records with confidence &lt; MIN_CONFIDENCE are rejected
     * (mirrors Python _MIN_CONFIDENCE = 0.3 gate in DocumentAspects.upsert).
     *
     * <p>Conflict key: (tenant_id, collection, source_path). Returns the
     * surrogate id of the inserted/updated row, or -1 if rejected.
     */
    /**
     * RDR-204 Phase 1 (bead nexus-ft04v.7): require catalog_collections to already
     * carry a row for {@code collection} before any document_aspects /
     * document_highlights write; fails loud
     * ({@link UnregisteredCollectionException}, mapped to 422) instead of the
     * RDR-164-era stub INSERT ... ON CONFLICT DO NOTHING that used to paper over a
     * missing row with blank content_type/owner_id/embedding_model. A no-op for a
     * null collection (document_highlights.collection is NOT NULL and FK'd to
     * catalog_collections — document_highlights_collection_fk — but the aspect
     * callers already reject a blank collection before reaching here, so they never
     * pass null in practice). A caller that nne()-defaults a blank collection to ""
     * now needs that EXACT row registered, or the write fails loud — the
     * document_highlights default no longer registers itself (RDR-204 §Technical
     * Design step 4).
     */
    private static void ensureCollectionRegistered(DSLContext ctx, String tenant, String collection) {
        if (collection == null) return;
        // nexus-i15oc: an aspect or highlight row filed under a retired (superseded) name strands
        // exactly as a chunk does (nexus-mp8ys), so every live writer refuses it 409 naming the successor.
        CollectionRegistry.requireWritable(ctx, tenant, collection);
    }

    /**
     * {@link #ensureCollectionRegistered} for the writers that legitimately file rows under a tombstone
     * (nexus-i15oc): the fidelity-preserving {@code import*} verbs (as the manifest {@code UPSERT_IMPORT}
     * carries tombstoned collections as they were) and the renames' re-home onto their target. The target
     * stays plain because a rename onto an EMPTY tombstone revives it: renaming B back onto A (the round
     * trip that undoes a rename) lands on the tombstone the first rename left at A. The SOURCE needs no
     * check, it is only read. Pinned by {@code AspectSupersededWriteTest}.
     */
    private static void ensureCollectionRegisteredAllowingSuperseded(DSLContext ctx, String tenant,
                                                                     String collection) {
        if (collection == null) return;
        CollectionRegistry.requireRegistered(ctx, tenant, collection);
    }

    /**
     * The aspect upsert, optionally fenced by a tuple claim (nexus-5r2sc.2.5, RDR-235 E5).
     *
     * <p>{@code claim_id} and {@code claimant} come together or not at all: one without the other is an
     * {@link IllegalArgumentException} (400) naming both fields, thrown before a transaction opens. Both
     * absent is today's write, unchanged. Both present, the FIRST statement of the upsert's transaction is
     * {@link TupleRepository#lockLiveClaimRow}, which locks the claim's tuples row, and the write follows in
     * the same transaction. The order is fixed: the claim lookup, then {@link #ensureCollectionRegistered}
     * (a retired name answers 409), then the {@code document_aspects} insert. So a 409 means the claim was
     * live at that instant, and a lapsed claim on a retired name answers 404.
     *
     * <p><b>The fence runs before the payload gates (nexus-eyynd M1).</b> A fenced write is judged in this
     * order: (1) the pair itself, one field without the other or a blank one, 400 before any transaction;
     * (2) the claim lookup, 404 {@code ClaimNotFound} or 403 {@code ClaimOwnership}; (3) for a claim that is
     * not an {@code aspects} task, 403 {@code ClaimTaskMismatch}, and for one that is, the same refusal when
     * its body names another document than the {@code doc_id} written (checked here only when the write
     * carries a non-blank {@code doc_id}); (4) the confidence gate, which drops a confidence below
     * 0.3 (or a null or absent one) as {@code -1}, 200 {@code written:false};
     * (5) the required-field and blank-{@code doc_id} gates, 400. So a lapsed, foreign or mismatched claim is
     * refused 404 or 403 whatever the payload would have answered, where before a low-confidence or malformed
     * payload got 200 or 400 and never reached the fence. A claim on the right task keeps the payload
     * answers: low confidence is 200 {@code written:false}, a malformed payload 400. The payload gates run
     * inside the transaction, after the lock, so the claim is locked for the (short) time they take; a
     * claim-less write has no lock and runs the gates first, as ever. Nothing is written on any refusal.
     *
     * <p>Lock order, shared with the rename helper (bead nexus-5r2sc.2.6): the tuples row lock FIRST, then
     * {@code document_aspects}, in every path, so a rename that rewrites task bodies before its registry
     * loop cannot deadlock against a persist of the same document.
     */
    public long upsertAspect(String tenant, Map<String, Object> body) {
        // Before everything else: a malformed pair is a caller bug whatever the payload or the claim is.
        Object rawClaimId = body.get("claim_id");
        Object rawClaimant = body.get("claimant");
        if ((rawClaimId == null) != (rawClaimant == null)) {
            throw new IllegalArgumentException("aspect upsert: claim_id and claimant must be given together");
        }
        if (rawClaimId != null && !(rawClaimId instanceof String id && !id.isBlank()
                && rawClaimant instanceof String who && !who.isBlank())) {
            throw new IllegalArgumentException("aspect upsert: claim_id and claimant must be non-blank strings");
        }
        String claimId = (String) rawClaimId;
        String claimant = (String) rawClaimant;
        if (claimId == null) {
            // No fence: the payload gates run before a transaction opens, as ever.
            ValidUpsert valid = validateUpsert(body);
            return valid == null ? -1L : tenantScope.withTenant(tenant, ctx -> writeAspect(ctx, tenant, body, valid));
        }
        // Fenced: the claim is looked up and locked FIRST, so a lapsed or foreign claim answers 404 or 403
        // whatever the payload is (nexus-eyynd M1); the payload gates run after it, in the same transaction.
        return tenantScope.withTenant(tenant, ctx -> {
            // Lock order (shared with the rename helper, nexus-5r2sc.2.6): the tuples row lock FIRST, then
            // document_aspects, in every path. This lookup takes the first lock; the insert takes the second.
            // A rename rewrites task bodies (tuple row locks) before its registry loop updates
            // document_aspects, so the orders agree and neither can deadlock the other.
            var claimRow = TupleRepository.lockLiveClaimRow(ctx, tenant, claimId, claimant);
            requireClaimOnTaskFor(claimRow, claimId, body.get("doc_id") instanceof String d ? d : null);
            ValidUpsert valid = validateUpsert(body);
            return valid == null ? -1L : writeAspect(ctx, tenant, body, valid);
        });
    }

    /** An upsert body that passed the confidence, required-field and doc_id gates. */
    private record ValidUpsert(double confidence, String collection, String sourcePath, String docId,
                               String modelVersion, String extractorName, OffsetDateTime extractedAt) { }

    /**
     * The payload gates of {@link #upsertAspect}, in their fixed order: a confidence below the threshold
     * (null and absent count as below) returns {@code null}, a missing required field or a blank
     * {@code doc_id} is an {@link IllegalArgumentException}.
     */
    private static ValidUpsert validateUpsert(Map<String, Object> body) {
        // nexus-j0nec: an explicit JSON "confidence": null must be DROPPED like
        // absent/low confidence (Python _MIN_CONFIDENCE gate semantics), never
        // NPE via ((Number) null).doubleValue() -> HTTP 500.
        double confidence = body.get("confidence") instanceof Number n
            ? n.doubleValue()
            : -1.0;
        if (confidence < MIN_CONFIDENCE) {
            log.warn("event=aspect_upsert_rejected_low_confidence collection={} source_path={} confidence={}",
                body.get("collection"), body.get("source_path"), body.get("confidence"));
            return null;
        }

        String collection    = (String) body.get("collection");
        String sourcePath    = (String) body.get("source_path");
        String extractedAt   = (String) body.get("extracted_at");
        String modelVersion  = (String) body.get("model_version");
        String extractorName = (String) body.get("extractor_name");
        if (collection == null || sourcePath == null || extractedAt == null
                || modelVersion == null || extractorName == null) {
            throw new IllegalArgumentException("aspect upsert: required fields missing");
        }
        // hygiene-001 (nexus-tk070.p6a follow-on, Sam decision): document_aspects
        // .doc_id is NOT NULL now (reverses fk-001-2's nullable conversion) — an
        // aspect row with no attributable document is not representable going
        // forward. Refuse at the boundary with the exact message the route
        // javadoc documents, rather than letting a blank doc_id reach the DB and
        // fail an opaque NOT NULL violation.
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            throw new IllegalArgumentException("doc_id required");
        }
        return new ValidUpsert(confidence, collection, sourcePath, docId, modelVersion, extractorName,
                parseTs(extractedAt));
    }

    private long writeAspect(DSLContext ctx, String tenant, Map<String, Object> body, ValidUpsert v) {
        String collection = v.collection();
        String sourcePath = v.sourcePath();
        String modelVersion = v.modelVersion();
        String extractorName = v.extractorName();
        String docId = v.docId();
        double confidence = v.confidence();
        OffsetDateTime extractedAtTs = v.extractedAt();
        ensureCollectionRegistered(ctx, tenant, collection);
        var result = ctx.insertInto(DOCUMENT_ASPECTS,
                DOCUMENT_ASPECTS.TENANT_ID,
                DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.SOURCE_PATH,
                DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                DOCUMENT_ASPECTS.PROPOSED_METHOD,
                DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                DOCUMENT_ASPECTS.EXTRAS,
                DOCUMENT_ASPECTS.CONFIDENCE,
                DOCUMENT_ASPECTS.EXTRACTED_AT,
                DOCUMENT_ASPECTS.MODEL_VERSION,
                DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                DOCUMENT_ASPECTS.SOURCE_URI,
                DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                DOCUMENT_ASPECTS.DOC_ID)
            .values(
                tenant,
                collection,
                sourcePath,
                (String) body.get("problem_formulation"),
                (String) body.get("proposed_method"),
                (String) body.get("experimental_datasets"),
                (String) body.get("experimental_baselines"),
                (String) body.get("experimental_results"),
                jsonbOrNull((String) body.get("extras")),
                confidence,
                extractedAtTs,
                modelVersion,
                extractorName,
                // hygiene-001: source_uri is NOT NULL now too; nne() guards
                // against a caller that omits it (was a silent NULL write
                // before this migration, matching CatalogRepository's own
                // nne()-guarded NOT NULL text columns).
                NoteUri.canonical(nne((String) body.get("source_uri"))),
                jsonbOrNull((String) body.get("salient_sentences")),
                docId)
            .onConflict(
                DOCUMENT_ASPECTS.TENANT_ID,
                DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.SOURCE_PATH)
            .doUpdate()
            .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION,    EX_PROBLEM_FORMULATION)
            .set(DOCUMENT_ASPECTS.PROPOSED_METHOD,        EX_PROPOSED_METHOD)
            .set(DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,  EX_EXPERIMENTAL_DATASETS)
            .set(DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES, EX_EXPERIMENTAL_BASELINES)
            .set(DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,   EX_EXPERIMENTAL_RESULTS)
            .set(DOCUMENT_ASPECTS.EXTRAS,                 EX_EXTRAS)
            .set(DOCUMENT_ASPECTS.CONFIDENCE,             EX_CONFIDENCE)
            .set(DOCUMENT_ASPECTS.EXTRACTED_AT,           EX_EXTRACTED_AT)
            .set(DOCUMENT_ASPECTS.MODEL_VERSION,          EX_MODEL_VERSION)
            .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME,         EX_EXTRACTOR_NAME)
            .set(DOCUMENT_ASPECTS.SOURCE_URI,             EX_SOURCE_URI)
            .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES,      EX_SALIENT_SENTENCES)
            // nexus-x1de2 (52): an id-less re-upsert must never strip an
            // attribution a previous write established — same COALESCE the
            // other two writers already use.
            .set(DOCUMENT_ASPECTS.DOC_ID,                 EX_DOC_ID_COALESCE)
            .returning(DOCUMENT_ASPECTS.ID)
            .fetch();
        return result.isEmpty() ? -1L : result.get(0).get(DOCUMENT_ASPECTS.ID);
    }

    /** The aspect-extraction queue's subspace (the literal {@code aspects} template, RDR-235 E3). */
    private static final String TASK_SUBSPACE = "aspects";

    private static final com.fasterxml.jackson.databind.ObjectMapper CLAIM_BODY_MAPPER =
            new com.fasterxml.jackson.databind.ObjectMapper();

    /**
     * A claim id is a credential for one task: the locked claim row must be an {@code aspects} task whose
     * body names the {@code doc_id} being written. Anything else, a body that is not a JSON object or has no
     * string {@code doc_id} included, is refused {@link ClaimTaskMismatchException} (403). A write whose own
     * {@code doc_id} is absent or blank is checked for the subspace only; the doc_id gate that follows refuses
     * it 400.
     */
    private static void requireClaimOnTaskFor(dev.nexus.service.jooq.nexus.tables.records.TuplesRecord claimRow,
                                              String claimId, String docId) {
        String subspace = claimRow.getSubspace();
        if (!TASK_SUBSPACE.equals(subspace)) {
            throw new ClaimTaskMismatchException(claimId, "an aspects task (it is a claim in '" + subspace + "')");
        }
        if (docId == null || docId.isBlank()) {
            // No document to compare: the payload gate answers 400 doc_id required, after this fence.
            return;
        }
        String taskDocId = null;
        try {
            var node = CLAIM_BODY_MAPPER.readTree(claimRow.getBody());
            if (node != null && node.isObject() && node.path("doc_id").isTextual()) {
                taskDocId = node.get("doc_id").asText();
            }
        } catch (com.fasterxml.jackson.core.JsonProcessingException | IllegalArgumentException e) {
            // taskDocId stays null: a body that does not parse names no document.
        }
        if (taskDocId == null) {
            throw new ClaimTaskMismatchException(claimId,
                    "the task for document '" + docId + "' (the task body names no doc_id)");
        }
        if (!taskDocId.equals(docId)) {
            throw new ClaimTaskMismatchException(claimId,
                    "the task for document '" + docId + "' (it is the task for '" + taskDocId + "')");
        }
    }

    /**
     * Get an aspect record by (collection, source_path).
     */
    public Optional<Map<String, Object>> getAspect(String tenant, String collection, String sourcePath) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.COLLECTION.eq(collection)
                    .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(sourcePath)))
                .fetch();
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(recordToMap(rows.get(0)));
        });
    }

    /**
     * Get an aspect record by doc_id (catalog tumbler).
     */
    public Optional<Map<String, Object>> getAspectByDocId(String tenant, String docId) {
        if (docId == null || docId.isBlank()) return Optional.empty();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.DOC_ID.eq(docId))
                .fetch();
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(recordToMap(rows.get(0)));
        });
    }

    /**
     * List aspect records for a collection, paginated.
     */
    public List<Map<String, Object>> listByCollection(String tenant, String collection, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            var q = ctx.select(
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.COLLECTION.eq(collection))
                .orderBy(DOCUMENT_ASPECTS.SOURCE_PATH.asc());
            var rows = (limit > 0 ? q.limit(limit).offset(offset) : q).fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) out.add(recordToMap(r));
            return out;
        });
    }

    /**
     * List records by extractor/version for re-extraction triage.
     */
    public List<Map<String, Object>> listByExtractorVersion(String tenant, String extractorName, String maxVersion) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.EXTRACTOR_NAME.eq(extractorName)
                    .and(DOCUMENT_ASPECTS.MODEL_VERSION.lt(maxVersion)))
                .orderBy(DOCUMENT_ASPECTS.COLLECTION.asc(), DOCUMENT_ASPECTS.SOURCE_PATH.asc())
                .fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) out.add(recordToMap(r));
            return out;
        });
    }

    /**
     * Delete an aspect by (collection, source_path). Returns deleted count.
     */
    public int deleteAspect(String tenant, String collection, String sourcePath) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(DOCUMENT_ASPECTS)
               .where(DOCUMENT_ASPECTS.COLLECTION.eq(collection)
                   .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(sourcePath)))
               .execute());
    }

    /**
     * Rename collection denorm cache (mirrors DocumentAspects.rename_collection). The unconsumed {@code aspects}
     * tasks that name the collection follow it (nexus-hbnml: this route took over from the retired
     * {@code /queue/rename_collection}, which was the standalone caller's way of re-homing them).
     */
    public int renameAspectCollection(String tenant, String oldColl, String newColl) {
        return tenantScope.withTenant(tenant, ctx -> {
            // RDR-210 Phase 6 Step 2 (bead nexus-f98bg): refused while a re-embed job holds either name.
            CatalogRepository.refuseUnderReembedJob(ctx, tenant, "rename", oldColl, newColl);
            // RDR-164 P1a: register the new collection before re-pointing the denorm column
            ensureCollectionRegisteredAllowingSuperseded(ctx, tenant, newColl);
            // RDR-235 (nexus-5r2sc.2.6): the tuple row locks come before any document_aspects statement, the order
            // a claim-fenced aspect write takes them in.
            AspectTaskRefs.rewriteCollection(ctx, tenant, oldColl, newColl);
            // Collision defense: delete conflicting new-side rows first
            ctx.deleteFrom(DOCUMENT_ASPECTS)
               .where(DOCUMENT_ASPECTS.COLLECTION.eq(newColl)
                   .and(DOCUMENT_ASPECTS.SOURCE_PATH.in(
                       select(DOCUMENT_ASPECTS.SOURCE_PATH)
                           .from(DOCUMENT_ASPECTS)
                           .where(DOCUMENT_ASPECTS.COLLECTION.eq(oldColl)))))
               .execute();
            return ctx.update(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.COLLECTION, newColl)
                .where(DOCUMENT_ASPECTS.COLLECTION.eq(oldColl))
                .execute();
        });
    }

    /**
     * Set salient_sentences for a doc_id. Returns rows updated.
     */
    public int setSalientSentences(String tenant, String docId, String sentencesJson) {
        if (docId == null || docId.isBlank()) return 0;
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(DOCUMENT_ASPECTS)
               .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES, jsonbOrNull(sentencesJson))
               .where(DOCUMENT_ASPECTS.DOC_ID.eq(docId))
               .execute());
    }

    /**
     * Set salient_sentences by (collection, source_path) — pre-migration fallback.
     */
    public int setSalientSentencesByKey(String tenant, String collection, String sourcePath, String sentencesJson) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.update(DOCUMENT_ASPECTS)
               .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES, jsonbOrNull(sentencesJson))
               .where(DOCUMENT_ASPECTS.COLLECTION.eq(collection)
                   .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(sourcePath)))
               .execute());
    }

    /**
     * Get salient_sentences for a doc_id. Returns null when not found.
     */
    public String getSalientSentences(String tenant, String docId) {
        if (docId == null || docId.isBlank()) return null;
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(DOCUMENT_ASPECTS.SALIENT_SENTENCES)
                .from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.DOC_ID.eq(docId))
                .fetch();
            if (rows.isEmpty()) return null;
            JSONB val = rows.get(0).get(DOCUMENT_ASPECTS.SALIENT_SENTENCES);
            return val == null ? null : val.data();
        });
    }

    /**
     * Fidelity ETL import — idempotent, complete-overwrite on (tenant_id, collection, source_path).
     * Confidence gate: rows with confidence &lt; MIN_CONFIDENCE are skipped.
     * extracted_at is preserved verbatim (EXCLUDED.*). Returns count imported.
     */
    public int importAspect(String tenant, Map<String, Object> body) {
        return tenantScope.withTenant(tenant, ctx -> doImportAspect(ctx, tenant, body));
    }

    /** PG Int16 bind-count limit is 32767; keep a safety margin (nexus-1usso). */
    private static final int MAX_BATCH_PARAMS = 30_000;

    /**
     * nexus-1usso: GUC-once bulk aspect import — ONE multi-row {@code INSERT
     * ... ON CONFLICT} statement per chunk, mirroring {@code
     * ChashRepository.doImportBatch} (f0ab406f). The RDR-176 P3 endpoint
     * already existed but still looped the per-row {@link #doImportAspect}
     * (N round-trips) — the plan-audit finding on nexus-1usso ("has the
     * endpoint" != "batches at the DB") applies to every Aspect import
     * method. The confidence gate is applied BEFORE the multi-row insert
     * (sub-confidence rows never reach it, matching the single-row path's
     * skip semantics); kept rows are then deduped on {@code (collection,
     * source_path)} — the conflict key — within a chunk, last occurrence
     * wins. Returns the number of rows actually written (sub-confidence
     * rows count 0, matching the single-row path) — NOT the post-dedup
     * landed count, since each surviving row independently satisfied the
     * confidence gate.
     */
    public int importAspectsBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            List<Map<String, Object>> kept = new ArrayList<>();
            for (var body : rows) {
                double confidence = body.containsKey("confidence") && body.get("confidence") != null
                    ? ((Number) body.get("confidence")).doubleValue()
                    : -1.0;
                if (confidence >= MIN_CONFIDENCE) kept.add(body);
            }
            if (kept.isEmpty()) return 0;

            var collections = new java.util.LinkedHashSet<String>();
            for (var body : kept) {
                String c = (String) body.get("collection");
                if (c != null) collections.add(c);
            }
            for (String c : collections) ensureCollectionRegisteredAllowingSuperseded(ctx, tenant, c);

            // Conflict key: (tenant_id, collection, source_path). tenant constant.
            var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(kept.size());
            for (var body : kept) {
                unique.put(body.get("collection") + "\u0000" + body.get("source_path"), body);
            }
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            final int cols = 16;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                var insert = ctx.insertInto(DOCUMENT_ASPECTS,
                        DOCUMENT_ASPECTS.TENANT_ID,
                        DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH,
                        DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                        DOCUMENT_ASPECTS.PROPOSED_METHOD,
                        DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                        DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                        DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                        DOCUMENT_ASPECTS.EXTRAS,
                        DOCUMENT_ASPECTS.CONFIDENCE,
                        DOCUMENT_ASPECTS.EXTRACTED_AT,
                        DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                        DOCUMENT_ASPECTS.SOURCE_URI,
                        DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                        DOCUMENT_ASPECTS.DOC_ID);
                for (var body : batch) {
                    double confidence = ((Number) body.get("confidence")).doubleValue();
                    OffsetDateTime extractedAtTs = parseTs((String) body.get("extracted_at"));
                    insert = insert.values(
                            tenant,
                            (String) body.get("collection"),
                            (String) body.get("source_path"),
                            (String) body.get("problem_formulation"),
                            (String) body.get("proposed_method"),
                            (String) body.get("experimental_datasets"),
                            (String) body.get("experimental_baselines"),
                            (String) body.get("experimental_results"),
                            jsonbOrNull((String) body.get("extras")),
                            confidence,
                            extractedAtTs,
                            (String) body.get("model_version"),
                            (String) body.get("extractor_name"),
                            // hygiene-001 (nexus-tk070.p6a follow-on): source_uri is
                            // NOT NULL now. doc_id stays nullIfBlank (NOT nne()) --
                            // "" would satisfy NOT NULL but violate the FK to
                            // catalog_documents(tumbler); a blank doc_id here is a
                            // genuine caller error, same as the live upsert path.
                            NoteUri.canonical(nne((String) body.get("source_uri"))),
                            jsonbOrNull((String) body.get("salient_sentences")),
                            nullIfBlank((String) body.get("doc_id")));
                }
                insert.onConflict(
                        DOCUMENT_ASPECTS.TENANT_ID,
                        DOCUMENT_ASPECTS.COLLECTION,
                        DOCUMENT_ASPECTS.SOURCE_PATH)
                      .doUpdate()
                      .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION,    EX_PROBLEM_FORMULATION)
                      .set(DOCUMENT_ASPECTS.PROPOSED_METHOD,        EX_PROPOSED_METHOD)
                      .set(DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,  EX_EXPERIMENTAL_DATASETS)
                      .set(DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES, EX_EXPERIMENTAL_BASELINES)
                      .set(DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,   EX_EXPERIMENTAL_RESULTS)
                      .set(DOCUMENT_ASPECTS.EXTRAS,                 EX_EXTRAS)
                      .set(DOCUMENT_ASPECTS.CONFIDENCE,             EX_CONFIDENCE)
                      .set(DOCUMENT_ASPECTS.EXTRACTED_AT,           EX_EXTRACTED_AT)
                      .set(DOCUMENT_ASPECTS.MODEL_VERSION,          EX_MODEL_VERSION)
                      .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME,         EX_EXTRACTOR_NAME)
                      .set(DOCUMENT_ASPECTS.SOURCE_URI,             EX_SOURCE_URI_COALESCE)
                      .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES,      EX_SALIENT_COALESCE)
                      .set(DOCUMENT_ASPECTS.DOC_ID,                 EX_DOC_ID_COALESCE)
                      .execute();
            }
            return kept.size();
        });
    }

    private int doImportAspect(DSLContext ctx, String tenant, Map<String, Object> body) {
        double confidence = body.containsKey("confidence") && body.get("confidence") != null
            ? ((Number) body.get("confidence")).doubleValue()
            : -1.0;
        if (confidence < MIN_CONFIDENCE) return 0;

        String extractedAt = (String) body.get("extracted_at");
        OffsetDateTime extractedAtTs = parseTs(extractedAt);

        {
            ensureCollectionRegisteredAllowingSuperseded(ctx, tenant, (String) body.get("collection"));
            return ctx.insertInto(DOCUMENT_ASPECTS,
                    DOCUMENT_ASPECTS.TENANT_ID,
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.PROBLEM_FORMULATION,
                    DOCUMENT_ASPECTS.PROPOSED_METHOD,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES,
                    DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,
                    DOCUMENT_ASPECTS.EXTRAS,
                    DOCUMENT_ASPECTS.CONFIDENCE,
                    DOCUMENT_ASPECTS.EXTRACTED_AT,
                    DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.SOURCE_URI,
                    DOCUMENT_ASPECTS.SALIENT_SENTENCES,
                    DOCUMENT_ASPECTS.DOC_ID)
                .values(
                    tenant,
                    (String) body.get("collection"),
                    (String) body.get("source_path"),
                    (String) body.get("problem_formulation"),
                    (String) body.get("proposed_method"),
                    (String) body.get("experimental_datasets"),
                    (String) body.get("experimental_baselines"),
                    (String) body.get("experimental_results"),
                    jsonbOrNull((String) body.get("extras")),
                    confidence,
                    extractedAtTs,
                    (String) body.get("model_version"),
                    (String) body.get("extractor_name"),
                    // hygiene-001: source_uri is NOT NULL now (see importAspectsBatch's
                    // identical comment for why doc_id stays nullIfBlank).
                    NoteUri.canonical(nne((String) body.get("source_uri"))),
                    jsonbOrNull((String) body.get("salient_sentences")),
                    nullIfBlank((String) body.get("doc_id")))
                .onConflict(
                    DOCUMENT_ASPECTS.TENANT_ID,
                    DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH)
                .doUpdate()
                .set(DOCUMENT_ASPECTS.PROBLEM_FORMULATION,    EX_PROBLEM_FORMULATION)
                .set(DOCUMENT_ASPECTS.PROPOSED_METHOD,        EX_PROPOSED_METHOD)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_DATASETS,  EX_EXPERIMENTAL_DATASETS)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_BASELINES, EX_EXPERIMENTAL_BASELINES)
                .set(DOCUMENT_ASPECTS.EXPERIMENTAL_RESULTS,   EX_EXPERIMENTAL_RESULTS)
                .set(DOCUMENT_ASPECTS.EXTRAS,                 EX_EXTRAS)
                .set(DOCUMENT_ASPECTS.CONFIDENCE,             EX_CONFIDENCE)
                .set(DOCUMENT_ASPECTS.EXTRACTED_AT,           EX_EXTRACTED_AT)
                .set(DOCUMENT_ASPECTS.MODEL_VERSION,          EX_MODEL_VERSION)
                .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME,         EX_EXTRACTOR_NAME)
                .set(DOCUMENT_ASPECTS.SOURCE_URI,             EX_SOURCE_URI_COALESCE)
                .set(DOCUMENT_ASPECTS.SALIENT_SENTENCES,      EX_SALIENT_COALESCE)
                .set(DOCUMENT_ASPECTS.DOC_ID,                 EX_DOC_ID_COALESCE)
                .execute();
        }
    }

    // ── document_highlights ────────────────────────────────────────────────────

    /**
     * Upsert a highlight record. Returns true on write, false when no content.
     */
    public boolean upsertHighlight(String tenant, Map<String, Object> body) {
        String docId  = (String) body.get("doc_id");
        String ingestedAt = (String) body.get("ingested_at");
        if (docId == null || docId.isBlank()) throw new IllegalArgumentException("doc_id must not be empty");
        if (ingestedAt == null || ingestedAt.isBlank()) throw new IllegalArgumentException("ingested_at must not be empty");
        String highlightsMd = (String) body.getOrDefault("highlights_md", "");
        String mentionsMd   = (String) body.getOrDefault("mentions_md", "");
        if ((highlightsMd == null || highlightsMd.isBlank())
                && (mentionsMd == null || mentionsMd.isBlank())) {
            return false;
        }

        OffsetDateTime ingestedAtTs = parseTs(ingestedAt);
        tenantScope.withTenant(tenant, ctx -> {
            // hygiene-001: register the SAME nne()-defaulted collection value
            // the INSERT below binds, or a blank collection 404s the FK.
            ensureCollectionRegistered(ctx, tenant, nne((String) body.get("collection")));
            ctx.insertInto(DOCUMENT_HIGHLIGHTS,
                    DOCUMENT_HIGHLIGHTS.TENANT_ID,
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(
                    tenant,
                    docId,
                    // hygiene-001 (nexus-tk070.p6a follow-on): source_uri/collection
                    // are NOT NULL now.
                    NoteUri.canonical(nne((String) body.get("source_uri"))),
                    nne((String) body.get("collection")),
                    highlightsMd,
                    mentionsMd,
                    ingestedAtTs)
                .onConflict(DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID)
                .doUpdate()
                .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI,    EX_HL_SOURCE_URI)
                .set(DOCUMENT_HIGHLIGHTS.COLLECTION,    EX_HL_COLLECTION)
                .set(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, EX_HL_HIGHLIGHTS)
                .set(DOCUMENT_HIGHLIGHTS.MENTIONS_MD,   EX_HL_MENTIONS)
                .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT,   EX_HL_INGESTED_AT)
                .execute();
            return null;
        });
        return true;
    }

    /**
     * Get a highlight record by doc_id.
     */
    public Optional<Map<String, Object>> getHighlight(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId))
                .fetch();
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(highlightToMap(rows.get(0)));
        });
    }

    /**
     * Get a highlight record by source_uri (DEVONthink UUID URI).
     */
    public Optional<Map<String, Object>> getHighlightBySourceUri(String tenant, String sourceUri) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.SOURCE_URI.eq(NoteUri.canonical(sourceUri)))  // RDR-231
                .limit(1)
                .fetch();
            if (rows.isEmpty()) return Optional.empty();
            return Optional.of(highlightToMap(rows.get(0)));
        });
    }

    /**
     * List highlight records, most recent first.
     */
    public List<Map<String, Object>> listHighlights(String tenant, int limit, int offset) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .from(DOCUMENT_HIGHLIGHTS)
                .orderBy(DOCUMENT_HIGHLIGHTS.INGESTED_AT.desc())
                .limit(limit)
                .offset(offset)
                .fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) out.add(highlightToMap(r));
            return out;
        });
    }

    /**
     * Delete a highlight by doc_id. Returns true if deleted.
     */
    public boolean deleteHighlight(String tenant, String docId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(DOCUMENT_HIGHLIGHTS)
               .where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId))
               .execute() > 0);
    }

    /**
     * ETL import — fidelity-preserving highlight upsert.
     */
    public int importHighlight(String tenant, Map<String, Object> body) {
        return tenantScope.withTenant(tenant, ctx -> doImportHighlight(ctx, tenant, body));
    }

    /**
     * nexus-1usso: GUC-once bulk highlight import — ONE multi-row {@code
     * INSERT ... ON CONFLICT} statement per chunk. Rows with a blank
     * {@code doc_id} are skipped BEFORE the insert (matching the single-row
     * path's skip semantics); kept rows are deduped on {@code doc_id} — the
     * conflict key — within a chunk, last occurrence wins. Returns the
     * number of rows actually written (blank-doc_id rows count 0).
     */
    public int importHighlightsBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            List<Map<String, Object>> kept = new ArrayList<>();
            for (var body : rows) {
                String docId = (String) body.get("doc_id");
                if (docId != null && !docId.isBlank()) kept.add(body);
            }
            if (kept.isEmpty()) return 0;

            // hygiene-001: register the nne()-defaulted value (never raw
            // null/blank) -- a row whose collection lands as "" still needs
            // that row registered, or the batch INSERT below 404s the FK.
            var collections = new java.util.LinkedHashSet<String>();
            for (var body : kept) {
                collections.add(nne((String) body.get("collection")));
            }
            for (String c : collections) ensureCollectionRegisteredAllowingSuperseded(ctx, tenant, c);

            var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(kept.size());
            for (var body : kept) unique.put((String) body.get("doc_id"), body);
            List<Map<String, Object>> deduped = List.copyOf(unique.values());

            final int cols = 7;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            for (int start = 0; start < deduped.size(); start += chunkSize) {
                var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
                var insert = ctx.insertInto(DOCUMENT_HIGHLIGHTS,
                        DOCUMENT_HIGHLIGHTS.TENANT_ID,
                        DOCUMENT_HIGHLIGHTS.DOC_ID,
                        DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                        DOCUMENT_HIGHLIGHTS.COLLECTION,
                        DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                        DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                        DOCUMENT_HIGHLIGHTS.INGESTED_AT);
                for (var body : batch) {
                    String ingestedAt = (String) body.get("ingested_at");
                    OffsetDateTime ingestedAtTs = parseTs(ingestedAt);
                    insert = insert.values(
                            tenant,
                            (String) body.get("doc_id"),
                            NoteUri.canonical(nne((String) body.get("source_uri"))),
                            nne((String) body.get("collection")),
                            (String) body.getOrDefault("highlights_md", ""),
                            (String) body.getOrDefault("mentions_md", ""),
                            ingestedAtTs);
                }
                insert.onConflict(DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID)
                      .doUpdate()
                      .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI,    EX_HL_SOURCE_URI)
                      .set(DOCUMENT_HIGHLIGHTS.COLLECTION,    EX_HL_COLLECTION)
                      .set(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, EX_HL_HIGHLIGHTS)
                      .set(DOCUMENT_HIGHLIGHTS.MENTIONS_MD,   EX_HL_MENTIONS)
                      .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT,   EX_HL_INGESTED_AT)
                      .execute();
            }
            return kept.size();
        });
    }

    private int doImportHighlight(DSLContext ctx, String tenant, Map<String, Object> body) {
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) return 0;
        String ingestedAt = (String) body.get("ingested_at");
        OffsetDateTime ingestedAtTs = parseTs(ingestedAt);

        {
            // hygiene-001: register the SAME nne()-defaulted collection value
            // the INSERT below binds, or a blank collection 404s the FK.
            ensureCollectionRegisteredAllowingSuperseded(ctx, tenant, nne((String) body.get("collection")));
            return ctx.insertInto(DOCUMENT_HIGHLIGHTS,
                    DOCUMENT_HIGHLIGHTS.TENANT_ID,
                    DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                    DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.MENTIONS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(
                    tenant,
                    docId,
                    NoteUri.canonical(nne((String) body.get("source_uri"))),
                    nne((String) body.get("collection")),
                    (String) body.getOrDefault("highlights_md", ""),
                    (String) body.getOrDefault("mentions_md", ""),
                    ingestedAtTs)
                .onConflict(DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID)
                .doUpdate()
                .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI,    EX_HL_SOURCE_URI)
                .set(DOCUMENT_HIGHLIGHTS.COLLECTION,    EX_HL_COLLECTION)
                .set(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, EX_HL_HIGHLIGHTS)
                .set(DOCUMENT_HIGHLIGHTS.MENTIONS_MD,   EX_HL_MENTIONS)
                .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT,   EX_HL_INGESTED_AT)
                .execute();
        }
    }

    /**
     * Rename collection denorm in document_highlights (mirrors DocumentHighlights rename_collection).
     *
     * <p>PK is doc_id (tumbler), so the collection column has no uniqueness constraint
     * and no collision-defense DELETE is needed — a plain UPDATE suffices.
     */
    public int renameHighlightsCollection(String tenant, String oldColl, String newColl) {
        return tenantScope.withTenant(tenant, ctx -> {
            CatalogRepository.refuseUnderReembedJob(ctx, tenant, "rename", oldColl, newColl);
            ensureCollectionRegisteredAllowingSuperseded(ctx, tenant, newColl);
            return ctx.update(DOCUMENT_HIGHLIGHTS)
                .set(DOCUMENT_HIGHLIGHTS.COLLECTION, newColl)
                .where(DOCUMENT_HIGHLIGHTS.COLLECTION.eq(oldColl))
                .execute();
        });
    }

    // ── aspect_promotion_log ───────────────────────────────────────────────────

    /**
     * Record a promotion event in the audit log.
     */
    public void recordPromotion(String tenant, Map<String, Object> body) {
        String fieldName = (String) body.get("field_name");
        String sqlType   = (String) body.get("sql_type");
        String promotedAt = (String) body.get("promoted_at");
        if (fieldName == null || sqlType == null) throw new IllegalArgumentException("field_name and sql_type required");
        OffsetDateTime promotedAtTs = parseTs(promotedAt);

        boolean columnAdded = Boolean.TRUE.equals(body.get("column_added"));
        int rowsBackfilled = body.containsKey("rows_backfilled") ? ((Number) body.get("rows_backfilled")).intValue() : 0;
        int rowsPruned     = body.containsKey("rows_pruned") ? ((Number) body.get("rows_pruned")).intValue() : 0;
        boolean pruned      = Boolean.TRUE.equals(body.get("pruned"));

        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(ASPECT_PROMOTION_LOG,
                    ASPECT_PROMOTION_LOG.TENANT_ID,
                    ASPECT_PROMOTION_LOG.FIELD_NAME,
                    ASPECT_PROMOTION_LOG.SQL_TYPE,
                    ASPECT_PROMOTION_LOG.COLUMN_ADDED,
                    ASPECT_PROMOTION_LOG.ROWS_BACKFILLED,
                    ASPECT_PROMOTION_LOG.ROWS_PRUNED,
                    ASPECT_PROMOTION_LOG.PRUNED,
                    ASPECT_PROMOTION_LOG.PROMOTED_AT)
                .values(tenant, fieldName, sqlType, columnAdded, rowsBackfilled, rowsPruned, pruned, promotedAtTs)
                .execute();
            return null;
        });
    }

    /**
     * List promotion history, oldest first.
     */
    public List<Map<String, Object>> listPromotions(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    ASPECT_PROMOTION_LOG.FIELD_NAME,
                    ASPECT_PROMOTION_LOG.SQL_TYPE,
                    ASPECT_PROMOTION_LOG.COLUMN_ADDED,
                    ASPECT_PROMOTION_LOG.ROWS_BACKFILLED,
                    ASPECT_PROMOTION_LOG.ROWS_PRUNED,
                    ASPECT_PROMOTION_LOG.PRUNED,
                    ASPECT_PROMOTION_LOG.PROMOTED_AT)
                .from(ASPECT_PROMOTION_LOG)
                .orderBy(ASPECT_PROMOTION_LOG.PROMOTED_AT.asc(), ASPECT_PROMOTION_LOG.ID.asc())
                .fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("field_name",       r.get(ASPECT_PROMOTION_LOG.FIELD_NAME));
                m.put("sql_type",         r.get(ASPECT_PROMOTION_LOG.SQL_TYPE));
                // nexus-cefa1.4: column_added/pruned are boolean now (the wire was
                // already boolean; this retires the `!= 0` integer shim). Defensive
                // null-guard mirrors CatalogRepository's legacy_grandfathered convention.
                Boolean columnAdded = r.get(ASPECT_PROMOTION_LOG.COLUMN_ADDED);
                m.put("column_added",     columnAdded != null ? columnAdded : Boolean.FALSE);
                m.put("rows_backfilled",  r.get(ASPECT_PROMOTION_LOG.ROWS_BACKFILLED));
                m.put("rows_pruned",      r.get(ASPECT_PROMOTION_LOG.ROWS_PRUNED));
                Boolean pruned = r.get(ASPECT_PROMOTION_LOG.PRUNED);
                m.put("pruned",           pruned != null ? pruned : Boolean.FALSE);
                OffsetDateTime ts = r.get(ASPECT_PROMOTION_LOG.PROMOTED_AT);
                m.put("promoted_at", ts == null ? null : formatTs(ts));
                out.add(m);
            }
            return out;
        });
    }

    /**
     * ETL import of a promotion log row (event log — DO NOTHING on conflict).
     */
    public int importPromotionRow(String tenant, Map<String, Object> body) {
        return tenantScope.withTenant(tenant, ctx -> doImportPromotionRow(ctx, tenant, body));
    }

    /**
     * nexus-1usso: GUC-once bulk aspect-promotion import — ONE multi-row
     * {@code INSERT ... ON CONFLICT DO NOTHING} statement per chunk. No
     * dedup needed: intra-statement conflicts against {@code DO NOTHING}
     * are a documented no-op (unlike {@code DO UPDATE}, which cannot affect
     * the same row twice), and {@code .execute()} on a {@code DO NOTHING}
     * statement returns exactly the count of NEWLY inserted rows — so
     * summing it across chunks reproduces the per-row loop's "count of
     * rows actually written" contract exactly, without a synthetic dedup
     * count. Rows missing {@code field_name}/{@code promoted_at} are
     * skipped BEFORE the insert (matching the single-row path).
     */
    public int importPromotionBatch(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            List<Map<String, Object>> valid = new ArrayList<>();
            for (var body : rows) {
                String fieldName  = (String) body.get("field_name");
                String promotedAt = (String) body.get("promoted_at");
                if (fieldName != null && promotedAt != null) valid.add(body);
            }
            if (valid.isEmpty()) return 0;

            int written = 0;
            final int cols = 8;
            final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
            for (int start = 0; start < valid.size(); start += chunkSize) {
                var batch = valid.subList(start, Math.min(start + chunkSize, valid.size()));
                var insert = ctx.insertInto(ASPECT_PROMOTION_LOG,
                        ASPECT_PROMOTION_LOG.TENANT_ID,
                        ASPECT_PROMOTION_LOG.FIELD_NAME,
                        ASPECT_PROMOTION_LOG.SQL_TYPE,
                        ASPECT_PROMOTION_LOG.COLUMN_ADDED,
                        ASPECT_PROMOTION_LOG.ROWS_BACKFILLED,
                        ASPECT_PROMOTION_LOG.ROWS_PRUNED,
                        ASPECT_PROMOTION_LOG.PRUNED,
                        ASPECT_PROMOTION_LOG.PROMOTED_AT);
                for (var body : batch) {
                    String fieldName = (String) body.get("field_name");
                    OffsetDateTime promotedAtTs = parseTs((String) body.get("promoted_at"));
                    boolean columnAdded = Boolean.TRUE.equals(body.get("column_added"));
                    int rowsBackfilled = body.containsKey("rows_backfilled") ? ((Number) body.get("rows_backfilled")).intValue() : 0;
                    int rowsPruned     = body.containsKey("rows_pruned") ? ((Number) body.get("rows_pruned")).intValue() : 0;
                    boolean pruned      = Boolean.TRUE.equals(body.get("pruned"));
                    insert = insert.values(tenant, fieldName, (String) body.getOrDefault("sql_type", "TEXT"),
                            columnAdded, rowsBackfilled, rowsPruned, pruned, promotedAtTs);
                }
                written += insert.onConflict(
                        ASPECT_PROMOTION_LOG.TENANT_ID,
                        ASPECT_PROMOTION_LOG.FIELD_NAME,
                        ASPECT_PROMOTION_LOG.PROMOTED_AT)
                    .doNothing()
                    .execute();
            }
            return written;
        });
    }

    private int doImportPromotionRow(DSLContext ctx, String tenant, Map<String, Object> body) {
        String fieldName  = (String) body.get("field_name");
        String promotedAt = (String) body.get("promoted_at");
        if (fieldName == null || promotedAt == null) return 0;
        OffsetDateTime promotedAtTs = parseTs(promotedAt);
        boolean columnAdded = Boolean.TRUE.equals(body.get("column_added"));
        int rowsBackfilled = body.containsKey("rows_backfilled") ? ((Number) body.get("rows_backfilled")).intValue() : 0;
        int rowsPruned     = body.containsKey("rows_pruned") ? ((Number) body.get("rows_pruned")).intValue() : 0;
        boolean pruned      = Boolean.TRUE.equals(body.get("pruned"));

        return
            ctx.insertInto(ASPECT_PROMOTION_LOG,
                    ASPECT_PROMOTION_LOG.TENANT_ID,
                    ASPECT_PROMOTION_LOG.FIELD_NAME,
                    ASPECT_PROMOTION_LOG.SQL_TYPE,
                    ASPECT_PROMOTION_LOG.COLUMN_ADDED,
                    ASPECT_PROMOTION_LOG.ROWS_BACKFILLED,
                    ASPECT_PROMOTION_LOG.ROWS_PRUNED,
                    ASPECT_PROMOTION_LOG.PRUNED,
                    ASPECT_PROMOTION_LOG.PROMOTED_AT)
                .values(
                    tenant, fieldName,
                    (String) body.getOrDefault("sql_type", "TEXT"),
                    columnAdded, rowsBackfilled, rowsPruned, pruned, promotedAtTs)
                .onConflict(
                    ASPECT_PROMOTION_LOG.TENANT_ID,
                    ASPECT_PROMOTION_LOG.FIELD_NAME,
                    ASPECT_PROMOTION_LOG.PROMOTED_AT)
                .doNothing()
                .execute();
    }

    // ── Operator fast-path queries (RDR-152 bead nexus-l9hd8) ──────────────────

    /**
     * Filter: return source_uris from {@code document_aspects} whose
     * {@code field} column (or {@code extras.key} sub-field) matches
     * {@code predicate} (a SQL LIKE pattern such as {@code "%paxos%"}).
     *
     * <p>Mirrors Python {@code aspect_sql._query_filter} (RDR-089). Uses
     * {@code ILIKE} (case-insensitive LIKE) to match SQLite's default case-insensitive
     * LIKE behaviour for ASCII data. Per-batch IN-list pagination (300 per batch)
     * matches the SQLite fast path for exact result-set parity.
     *
     * <p>For {@code extras.key} fields ({@code field.startsWith("extras.")}):
     * Postgres {@code COALESCE(extras, '{}')::json->>'key' LIKE ?}.
     * For all other fields: {@code field LIKE ?}.
     *
     * <p>RLS enforcement: tenant isolation via {@link TenantScope#withTenant}.
     *
     * @param tenant      tenant scope
     * @param sourceUris  candidate source URIs to evaluate
     * @param field       aspect column or extras.key (e.g. {@code "proposed_method"},
     *                    {@code "experimental_datasets"}, {@code "extras.venue"})
     * @param predicate   SQL LIKE pattern (e.g. {@code "%paxos%"}, {@code "%\"TPC-C\"%"})
     * @return list of source_uris that match the predicate (subset of input)
     */
    public List<String> filterBySourceUris(
            String tenant, List<String> sourceUris, String field, String predicate) {
        if (sourceUris == null || sourceUris.isEmpty()) return List.of();
        List<String> uris = sourceUris.stream().map(NoteUri::canonical).distinct().toList();  // RDR-231
        validateField(field);
        String fieldExpr = fieldExpression(field);

        List<String> result = new ArrayList<>();
        for (int start = 0; start < uris.size(); start += 300) {
            List<String> batch = uris.subList(start, Math.min(start + 300, uris.size()));
            // fieldExpr is validated by fieldExpression() which only emits:
            //   - a bare column name (from ALLOWED_ASPECT_COLUMNS, no injection risk)
            //   - COALESCE(extras,'{}')::json#>>'{...}' (keys validated to [A-Za-z0-9_.]+)
            // ILIKE (case-insensitive) mirrors SQLite's default LIKE behaviour for ASCII.
            Field<String> fExpr = field(fieldExpr, String.class);
            List<String> matched = tenantScope.withTenant(tenant, ctx -> {
                var rows = ctx.select(DOCUMENT_ASPECTS.SOURCE_URI)
                    .from(DOCUMENT_ASPECTS)
                    .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                        .and(condition("{0} ILIKE {1}", fExpr, inline(predicate))))
                    .fetch();
                List<String> out = new ArrayList<>();
                for (var r : rows) {
                    String v = r.get(DOCUMENT_ASPECTS.SOURCE_URI);
                    if (v != null) out.add(v);
                }
                return out;
            });
            result.addAll(matched);
        }
        return result;
    }

    /**
     * GroupBy: return a map of {@code source_uri → key_value} for each URI
     * whose aspect row exists and has a non-null value for {@code field}.
     *
     * <p>Mirrors Python {@code aspect_sql._query_groupby} (RDR-089). URIs
     * without a matching aspect row are absent from the result map; the
     * Python caller maps absent entries to {@code "unassigned"}.
     *
     * <p>For {@code extras.key}: Postgres {@code COALESCE(extras,'{}')::json->>'key'}.
     *
     * @param tenant      tenant scope
     * @param sourceUris  candidate source URIs
     * @param field       aspect column or extras.key
     * @return map of uri → value string (null values omitted from map)
     */
    public Map<String, String> groupByField(String tenant, List<String> sourceUris, String field) {
        if (sourceUris == null || sourceUris.isEmpty()) return Map.of();
        List<String> uris = sourceUris.stream().map(NoteUri::canonical).distinct().toList();  // RDR-231
        validateField(field);
        String fieldExpr = fieldExpression(field);

        Map<String, String> result = new java.util.LinkedHashMap<>();
        for (int start = 0; start < uris.size(); start += 300) {
            List<String> batch = uris.subList(start, Math.min(start + 300, uris.size()));
            // fieldExpr validated by fieldExpression() — safe to embed in DSL.field()
            Field<String> fExpr = field(fieldExpr, String.class);

            tenantScope.withTenant(tenant, ctx -> {
                var rows = ctx.select(DOCUMENT_ASPECTS.SOURCE_URI, fExpr)
                    .from(DOCUMENT_ASPECTS)
                    .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch))
                    .fetch();
                for (var r : rows) {
                    String uri = r.get(DOCUMENT_ASPECTS.SOURCE_URI);
                    Object val = r.get(fExpr);
                    if (uri != null && val != null) {
                        result.put(uri, val.toString());
                    }
                }
                return null;
            });
        }
        return result;
    }

    /**
     * ConfidenceAggregate: compute AVG / MIN / MAX confidence across the
     * provided source URIs.
     *
     * <p>Mirrors Python {@code aspect_sql._query_confidence_aggregate} (RDR-089).
     * Uses a single SQL aggregate per batch (more efficient than fetching all
     * confidence values and folding in Java/Python).
     *
     * <p>Supported {@code reducerKind} values: {@code "avg_confidence"},
     * {@code "min_confidence"}, {@code "max_confidence"}. Any other value
     * returns {@code null}.
     *
     * @param tenant      tenant scope
     * @param sourceUris  candidate source URIs
     * @param reducerKind one of avg_confidence / min_confidence / max_confidence
     * @return the aggregate value, or null when no aspect rows exist or
     *         reducerKind is unrecognised
     */
    public Double confidenceAggregate(String tenant, List<String> sourceUris, String reducerKind) {
        if (sourceUris == null || sourceUris.isEmpty()) return null;
        List<String> uris = sourceUris.stream().map(NoteUri::canonical).distinct().toList();  // RDR-231

        String aggFunc = switch (reducerKind) {
            case "avg_confidence" -> "AVG";
            case "min_confidence" -> "MIN";
            case "max_confidence" -> "MAX";
            default -> null;
        };
        if (aggFunc == null) return null;

        // Accumulate across batches: AVG needs sum+count; MIN/MAX fold naturally.
        double sumAcc = 0.0;
        long   cntAcc = 0L;
        Double minAcc = null;
        Double maxAcc = null;

        for (int start = 0; start < uris.size(); start += 300) {
            List<String> batch = uris.subList(start, Math.min(start + 300, uris.size()));

            final double[] localSum = {0.0};
            final long[]   localCnt = {0L};
            final Double[] localVal = {null};

            tenantScope.withTenant(tenant, ctx -> {
                if ("AVG".equals(aggFunc)) {
                    // For AVG we need SUM+COUNT to fold across batches correctly.
                    var rows = ctx.select(
                            sum(DOCUMENT_ASPECTS.CONFIDENCE),
                            count(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object sumVal = rows.get(0).get(0);
                        Object cntVal = rows.get(0).get(1);
                        if (sumVal != null) localSum[0] = ((Number) sumVal).doubleValue();
                        if (cntVal != null) localCnt[0] = ((Number) cntVal).longValue();
                    }
                } else if ("MIN".equals(aggFunc)) {
                    var rows = ctx.select(min(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object val = rows.get(0).get(0);
                        if (val != null) localVal[0] = ((Number) val).doubleValue();
                    }
                } else {
                    var rows = ctx.select(max(DOCUMENT_ASPECTS.CONFIDENCE))
                        .from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.SOURCE_URI.in(batch)
                            .and(DOCUMENT_ASPECTS.CONFIDENCE.isNotNull()))
                        .fetch();
                    if (!rows.isEmpty()) {
                        Object val = rows.get(0).get(0);
                        if (val != null) localVal[0] = ((Number) val).doubleValue();
                    }
                }
                return null;
            });

            if ("AVG".equals(aggFunc)) {
                sumAcc += localSum[0];
                cntAcc += localCnt[0];
            } else if (localVal[0] != null) {
                if ("MIN".equals(aggFunc)) {
                    minAcc = (minAcc == null) ? localVal[0] : Math.min(minAcc, localVal[0]);
                } else {
                    maxAcc = (maxAcc == null) ? localVal[0] : Math.max(maxAcc, localVal[0]);
                }
            }
        }

        return switch (reducerKind) {
            case "avg_confidence" -> cntAcc == 0 ? null : sumAcc / cntAcc;
            case "min_confidence" -> minAcc;
            case "max_confidence" -> maxAcc;
            default -> null;
        };
    }

    /**
     * Bare column names allowed as operator query fields.
     *
     * <p>Mirrors Python {@code _ASPECT_COLUMN_TYPES} keys. The {@code "extras"} key itself
     * is excluded because it is only valid as the {@code extras.<key>} form; direct use of
     * the JSON object column is not meaningful as a filter/groupby field.
     *
     * <p>Server-side allowlist: even though Python callers validate field names before
     * posting, the service endpoint is externally reachable (any curl). Without a server-
     * side guard, a POST with {@code "field": "x; DROP TABLE nexus.document_aspects; --"}
     * would be injected directly into the SQL string via {@link #fieldExpression}.
     */
    public static final Set<String> ALLOWED_ASPECT_COLUMNS = Set.of(
        "problem_formulation",
        "proposed_method",
        "experimental_datasets",
        "experimental_baselines",
        "experimental_results",
        "confidence"
    );

    /**
     * Validate that {@code field} is either a known scalar column or an {@code extras.<key>}
     * reference. Throws {@link IllegalArgumentException} (→ HTTP 400) for anything else.
     *
     * <p>This is the server-side allowlist guard (C1 injection fix). Python callers
     * pre-validate via {@code _ASPECT_COLUMN_TYPES}, but the service is a public HTTP
     * endpoint — this guard prevents direct-POST injection.
     */
    public static void validateField(String field) {
        if (field == null || field.isBlank()) {
            throw new IllegalArgumentException("field must not be blank");
        }
        if (field.startsWith("extras.")) {
            String key = field.substring("extras.".length());
            if (key.isBlank()) {
                throw new IllegalArgumentException("extras. field requires a non-empty key");
            }
            // Key may contain dots (nested path) — dots and alphanumerics are allowed.
            // Reject anything that would escape the JSON path (single-quote, semicolon, etc.)
            if (!key.matches("[A-Za-z0-9_.]+")) {
                throw new IllegalArgumentException(
                    "extras key must match [A-Za-z0-9_.]+; got: " + key);
            }
            return;
        }
        if (!ALLOWED_ASPECT_COLUMNS.contains(field)) {
            throw new IllegalArgumentException(
                "field " + field + " is not a known aspect column; "
                + "allowed: " + ALLOWED_ASPECT_COLUMNS + " or extras.<key>");
        }
    }

    /**
     * Build the SQL field expression for a column name or extras.key.
     *
     * <p>For {@code extras.key}: uses Postgres {@code #>>} (path operator) for
     * true JSONPath-equivalent traversal, so {@code extras.a.b} correctly resolves
     * {@code extras → a → b} (mirrors SQLite's {@code json_extract(extras, '$.a.b')}).
     * Single-level keys (e.g. {@code extras.venue}) still work correctly via the
     * path form {@code extras #>> '{venue}'}.
     *
     * <p>COALESCE: when {@code extras} IS NULL the cast to {@code json} would fail;
     * {@code COALESCE(extras,'{}')::json} substitutes an empty object, so the path
     * extraction returns null instead of throwing.
     *
     * <p>For bare column names: returned verbatim after allowlist validation in
     * {@link #validateField}. The allowlist is the injection guard; this method
     * is only called after validateField passes.
     */
    private static String fieldExpression(String field) {
        if (field.startsWith("extras.")) {
            // Split on '.' to build the Postgres array literal {a,b,...}
            // e.g. "extras.venue"   → "{venue}"
            //      "extras.a.b"     → "{a,b}"
            String keyPart = field.substring("extras.".length());
            String[] segments = keyPart.split("\\.");
            StringBuilder sb = new StringBuilder("COALESCE(extras,'{}')::json#>>'{");
            for (int i = 0; i < segments.length; i++) {
                if (i > 0) sb.append(',');
                // Segments were validated by validateField to match [A-Za-z0-9_.]+ so
                // no quoting is needed inside the Postgres array literal.
                sb.append(segments[i]);
            }
            sb.append("}'");
            return sb.toString();
        }
        // Bare column name — allowlist validated by validateField before this call.
        return field;
    }

    // ── Private helpers ────────────────────────────────────────────────────────

    private static Map<String, Object> recordToMap(org.jooq.Record r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("collection",              r.get(0));
        m.put("source_path",             r.get(1));
        m.put("problem_formulation",     r.get(2));
        m.put("proposed_method",         r.get(3));
        m.put("experimental_datasets",   r.get(4));
        m.put("experimental_baselines",  r.get(5));
        m.put("experimental_results",    r.get(6));
        // nexus-cefa1.4: extras is jsonb now — JSONB.data() renders the raw JSON
        // text on the wire, unchanged shape (a caller-supplied JSON object string,
        // or null when the column is NULL — matching the prior TEXT column's null
        // passthrough exactly).
        Object extrasRaw = r.get(7);
        m.put("extras", extrasRaw instanceof JSONB extrasJ ? extrasJ.data() : extrasRaw);
        m.put("confidence",              r.get(8));
        Object eatRaw = r.get(9);
        m.put("extracted_at", eatRaw instanceof OffsetDateTime
            ? formatTs((OffsetDateTime) eatRaw) : (eatRaw == null ? null : eatRaw.toString()));
        m.put("model_version",           r.get(10));
        m.put("extractor_name",          r.get(11));
        m.put("source_uri",              r.get(12));
        // nexus-cefa1.4: salient_sentences is jsonb now — same JSONB.data() unwrap.
        Object salientRaw = r.get(13);
        m.put("salient_sentences", salientRaw instanceof JSONB salientJ ? salientJ.data() : salientRaw);
        m.put("doc_id",                  r.get(14) == null ? "" : r.get(14).toString());
        return m;
    }

    /**
     * Converts a doc_id string to null when blank or empty.
     * Required after nexus-b7v6i: the document_aspects doc_id column is nullable
     * with a real FK to catalog_documents.
     * An empty string '' would fail the FK (no catalog doc with tumbler='').
     */
    private static String nullIfBlank(String s) {
        return (s == null || s.isBlank()) ? null : s;
    }

    /**
     * Non-null empty: returns "" if null. hygiene-001 (nexus-tk070.p6a
     * follow-on) — mirrors CatalogRepository's identical helper, used to
     * guard NOT NULL text columns (document_aspects.source_uri) against a
     * caller that omits the field.
     */
    private static String nne(String v) {
        return v != null ? v : "";
    }

    private static Map<String, Object> highlightToMap(org.jooq.Record r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("doc_id",       r.get(0));
        m.put("source_uri",   r.get(1) == null ? "" : r.get(1).toString());
        m.put("collection",   r.get(2) == null ? "" : r.get(2).toString());
        m.put("highlights_md",r.get(3) == null ? "" : r.get(3).toString());
        m.put("mentions_md",  r.get(4) == null ? "" : r.get(4).toString());
        Object iRaw = r.get(5);
        m.put("ingested_at", iRaw instanceof OffsetDateTime
            ? formatTs((OffsetDateTime) iRaw) : (iRaw == null ? "" : iRaw.toString()));
        return m;
    }
}
