package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;

/**
 * Minimal HTTP response helpers. No framework dependency.
 */
public final class HttpUtil {

    private HttpUtil() {}

    public static void send(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }

    /** Response header on a request-deadline 503: {@code refused} or {@code aborted}
     *  (nexus-qajw7). Additive: a client that does not read it keeps its old retries. */
    public static final String DEADLINE_OUTCOME_HEADER = "X-Nexus-Deadline-Outcome";

    /**
     * The one wire shape for a {@link dev.nexus.service.vectors.RequestDeadlineExceededException}
     * on every route that embeds (nexus-8hdg9, nexus-qajw7): 503, inside the client's gateway
     * retry codes, with {@code Retry-After}, and the outcome as a header and a body field so
     * the client widens its retries for a refusal (nothing embedded) and not for an abort
     * (embedded work discarded). Shared so the vector and catalog handlers cannot drift.
     */
    public static void sendRequestDeadlineExceeded(
            HttpExchange exchange, dev.nexus.service.vectors.RequestDeadlineExceededException e)
            throws IOException {
        String outcome = e.outcome().wire();
        exchange.getResponseHeaders().set("Retry-After", Long.toString(e.retryAfterSeconds()));
        exchange.getResponseHeaders().set(DEADLINE_OUTCOME_HEADER, outcome);
        send(exchange, 503, "{\"error\":" + jsonString(e.getMessage())
                + ",\"retry_after_seconds\":" + e.retryAfterSeconds()
                + ",\"deadline_outcome\":" + jsonString(outcome) + "}");
    }

    /**
     * Minimal JSON string escaping (backslash, double-quote, control chars).
     * For structured responses use Jackson; this is for error detail strings only.
     */
    public static String jsonString(String value) {
        if (value == null) return "null";
        var sb = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            switch (c) {
                case '"'  -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default   -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
        return sb.toString();
    }

    /**
     * Walk the cause chain for a {@link SQLException} whose SQLSTATE is class 23
     * (integrity-constraint violation: 23502 not-null, 23503 foreign-key, 23505
     * unique, 23514 check). Returns the offending SQLSTATE string, or {@code null}
     * if no class-23 cause exists.
     *
     * <p>Extracted from {@code AspectHandler} (RDR-172 P3.1, nexus-gfl3y) to a
     * shared home so sibling handlers with a client-supplied id hitting a DB
     * constraint (RDR-172 follow-up, nexus-7e057) map it to a typed 409 AHEAD of
     * the generic 500, instead of duplicating the walk per handler.
     *
     * <p>jOOQ wraps the driver exception in a {@code DataAccessException}, so the
     * constraint violation is a cause of the thrown runtime exception, not the
     * top-level throwable — hence the chain walk. The walk is depth-bounded to
     * tolerate a malformed (self- or mutually-referential) cause chain.
     *
     * <p>Walks the {@link Throwable#getCause()} chain only — correct for the
     * PostgreSQL JDBC driver, which wraps via {@code initCause()}. It does NOT
     * traverse {@link SQLException#getNextException()} (used by some other
     * drivers for chained violations); generalise here if a non-PG driver is
     * ever introduced.
     */
    public static String sqlState23(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLException se) {
                String state = se.getSQLState();
                if (state != null && state.startsWith("23")) {
                    return state;
                }
            }
        }
        return null;
    }

    /**
     * Walk the cause chain for a {@link SQLException} whose SQLSTATE is class 22
     * (data exception — e.g. {@code 22021 character_not_in_repertoire}, the
     * NUL byte Postgres {@code text}/{@code jsonb} cannot store; {@code 22P05
     * untranslatable_character}; and siblings). Returns the offending SQLSTATE
     * string, or {@code null} if no class-22 cause exists.
     *
     * <p>Bead nexus-dmrkm, split out of nexus-yvzhz (the PDF-with-NUL-bytes
     * page_text 500). Class-wide (matches the whole {@code 22*} family), not a
     * {@code 22021}-only allowlist — mirrors {@link #sqlState23}'s class-wide
     * class-23 match rather than enumerating individual codes: the same
     * "caller-supplied data the database legitimately refuses is a 4xx, not a
     * 500" reasoning applies uniformly across class 22, not only to the NUL
     * case that happened to surface it first. Walks {@link Throwable#getCause()}
     * only, depth-bounded, same shape as {@link #sqlState23}.
     */
    public static String sqlStateDataException(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLException se) {
                String state = se.getSQLState();
                if (state != null && state.startsWith("22")) {
                    return state;
                }
            }
        }
        return null;
    }

    /**
     * Walk the cause chain for a {@link SQLTransientConnectionException} — HikariCP's
     * "Connection is not available, request timed out" signal, thrown from
     * {@code dataSource.getConnection()} when every pooled connection is checked out
     * (or blocked waiting on a DB-side lock) longer than {@code connectionTimeout}.
     *
     * <p>Bead nexus-h8rf6.2: this condition is RETRYABLE (the pool recovers as soon as
     * a connection frees up) and distinct from a genuine server fault — it deserves a
     * typed 503, not the opaque 500 catch-all, so callers on the client retry ladder
     * back off and retry instead of treating it as a hard failure. {@code TenantScope}
     * wraps the driver exception in a {@code RuntimeException}, so the transient
     * exception is a cause of the thrown exception, not the top-level throwable —
     * hence the chain walk (mirrors {@link #sqlState23}).
     *
     * @return true if a {@link SQLTransientConnectionException} appears anywhere in
     *         {@code t}'s cause chain
     */
    public static boolean isPoolExhausted(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLTransientConnectionException) {
                return true;
            }
        }
        return false;
    }

    /**
     * Terminal typed-DB-error mapper for handler catch-alls (wave review, Java-tree
     * audit High-2): pool exhaustion → retryable 503; class-23 integrity violation →
     * typed 409. Returns {@code true} when a typed response was sent; the caller's
     * catch block falls through to its own opaque-500 branch on {@code false}.
     *
     * <p>Exists so every handler shares ONE mapping instead of copy-pasting the
     * {@code isPoolExhausted}/{@code sqlState23} ladder — pre-fix only 2 of 15
     * handlers mapped pool exhaustion and 4 of 15 mapped class-23, so which typed
     * error a client saw depended on which handler it happened to hit. Client body
     * is a fixed message (+ sqlstate for 409); the raw driver message goes to the
     * server log only, never to the client.
     *
     * <p><b>The {@code reason} vocabulary (nexus-bgvnx).</b> {@code reason} is the
     * discriminator for a typed 4xx body: a stable, machine-readable, lower_snake
     * string a client decides on INSTEAD of matching the prose in {@code error}. The
     * prose may be reworded; a {@code reason} value, once shipped, never changes
     * meaning and is never reused. A body with no {@code reason} is either an older
     * engine or a typed body that has not needed one. Current values:
     * <ul>
     *   <li>{@code unregistered_collection} (422): a request named a
     *       {@code (tenant, collection)} with no catalog row
     *       ({@link #UNREGISTERED_COLLECTION_REASON}); the body also carries
     *       {@code tenant}, {@code collection} and {@code remedy}.</li>
     *   <li>{@code ownerless_chunk_write} (422): {@code upsert-chunks} or {@code store-put}
     *       was asked to write a chash with no live manifest row in the collection
     *       ({@link #OWNERLESS_CHUNK_WRITE_REASON}, RDR-223 Phase 3 Step 2); the body also
     *       carries {@code unowned_count}, {@code requested_count} and {@code unowned_chashes}
     *       (a sample), and {@code error} names the combined write routes that replace it.</li>
     *   <li>{@code collection_model_mismatch} (409): a re-home ({@code POST /v1/chash/rename_collection},
     *       {@code POST /v1/catalog/collections/rehome}) would file chunks of one embedding model under a
     *       collection of another ({@link #COLLECTION_MODEL_MISMATCH_REASON}, RDR-225); the body also
     *       carries {@code source_collection}, {@code source_model}, {@code target_collection} and
     *       {@code target_model}. Not retryable: a move between models is a cross-model migration.</li>
     *   <li>{@code unregistered_embedding_model} (422): a chunk or centroid write named a collection whose
     *       embedding model is registered in no {@code nexus.embedding_models} row and has no partition
     *       ({@link #UNREGISTERED_EMBEDDING_MODEL_REASON}, RDR-225); the body carries {@code model}.</li>
     *   <li>{@code model_partition_missing} (500): the model is registered but its changeset never created
     *       the partition ({@link #MODEL_PARTITION_MISSING_REASON}, RDR-225); an engine or deploy defect,
     *       not a caller error. The body carries {@code model} and the remedy.</li>
     *   <li>{@code tenant_partition_missing} (500): PostgreSQL found no partition for the row (SQLSTATE
     *       23514, "no partition of relation ... found for row"): a tenant's leaf was dropped or the
     *       {@code service_tokens} trigger was disabled ({@link #TENANT_PARTITION_MISSING_REASON}, RDR-225).
     *       An engine invariant, so a 500 and not the 409 a CHECK violation gets; the body names the
     *       tenant and the model when the server's detail carries them.</li>
     *   <li>{@code tenant_creation_busy} (503): a token insert waited on a lock, ran past its statement
     *       bound or lost a deadlock on every attempt (a tenant's first token also creates its partitions
     *       and is the usual cause), past the bound {@code TokenStore} gives it
     *       ({@link #TENANT_CREATION_BUSY_REASON}, RDR-225). Nothing was issued; retryable, the body
     *       carries {@code retry_after_seconds}.</li>
     *   <li>{@code quarantine_restore_busy} (503): {@code POST /gc/quarantine-restore} could not take the
     *       collection's sweep gate (or an owning document's index-run lock) inside its 2 s bound, or ran past
     *       its statement bound, or was the victim of a deadlock ({@link #QUARANTINE_RESTORE_BUSY_REASON}); the
     *       statement that tripped rolled back, and each quarantine sibling is its own transaction, so the body
     *       carries {@code retry_after_seconds}, {@code nothing_moved} (false when an earlier sibling had already
     *       committed), {@code audit_ids} and {@code moved_chashes} (what those earlier siblings committed).
     *       Retryable.</li>
     * </ul>
     * The typed 409 bodies predate the rule and discriminate on {@code status}
     * ({@code conflict_running}, {@code stale_run}) or on {@code constraint}; those
     * are grandfathered and are not renamed, since released clients read them. A NEW
     * typed 4xx body adds a {@code reason} value here in the same change, and the
     * client's copy of the vocabulary ({@code nexus.db.engine_reasons}) with it.
     *
     * @param exchange the exchange to respond on
     * @param e        the caught exception (cause chain is walked)
     * @param log      the HANDLER's logger, so log events keep their per-handler source
     * @param event    handler event prefix (e.g. {@code "memory_handler"})
     * @param context  preformatted log context (e.g. {@code "op=/put tenant=t1"})
     * @return true if a typed 503/409 was sent; false if the caller must 500
     */
    public static boolean sendTypedDbError(HttpExchange exchange, Throwable e,
                                           org.slf4j.Logger log, String event,
                                           String context) throws IOException {
        if (isPoolExhausted(e)) {
            // Bead nexus-h8rf6.2: HikariCP pool exhaustion is retryable — a typed 503
            // lets the client's retry ladder back off instead of failing hard.
            log.warn("event={}_pool_exhausted {} error={}", event, context, e.getMessage());
            send(exchange, 503, "{\"error\":\"database connection pool exhausted, retry\"}");
            return true;
        }
        // nexus-0ehwe arbiter class: a DELIBERATE refusal to give one identity to two
        // addresses. Mapped ahead of the generic class-23 branch because it is raised by
        // the repository BEFORE the statement runs, so it carries no SQLSTATE — but it
        // is the same 409 story with a diagnosable body (which key, what already holds
        // it, what was refused) instead of a bare "integrity constraint violation".
        var conflict = identityConflict(e);
        if (conflict != null) {
            log.warn("event={}_identity_conflict {} constraint={} identity={} existing={} attempted={}",
                event, context, conflict.constraint(), conflict.identity(),
                conflict.existingAddress(), conflict.attemptedAddress());
            send(exchange, 409,
                "{\"error\":" + jsonString(conflict.getMessage())
                + ",\"constraint\":" + jsonString(conflict.constraint())
                + ",\"identity\":" + jsonString(conflict.identity())
                + ",\"existing\":" + jsonString(conflict.existingAddress())
                + ",\"attempted\":" + jsonString(conflict.attemptedAddress())
                + "}");
            return true;
        }
        // nexus-lcmbp: a create() retry against a 'running' row with a fresh heartbeat
        // is a business-logic refusal (no SQLSTATE — the repository throws before any
        // statement conflicts), mapped ahead of the generic class-23 branch for the
        // same reason as the identity-conflict branch above: a diagnosable typed body
        // instead of falling through to an opaque 500, and — the defect this exists to
        // close — never a 200 "skip" the caller can mistake for success.
        //
        // nexus-lcmbp fix-list #5: the "remedy" literal below must stay TEXTUALLY
        // IDENTICAL to PipelineConflictException's message tail — the client
        // (HttpPipelineDB.create_pipeline / PipelineConflictRunning) dedups by
        // checking `remedy in error` before appending it to the exception message; a
        // drift here makes the user-facing text double up the remedy.
        var pipelineConflict = pipelineConflict(e);
        if (pipelineConflict != null) {
            log.warn("event={}_pipeline_conflict {} content_hash={} heartbeat_age_s={} "
                    + "stale_threshold_s={}",
                event, context, pipelineConflict.contentHash(),
                pipelineConflict.heartbeatAgeSeconds(), pipelineConflict.staleThresholdSeconds());
            send(exchange, 409,
                "{\"error\":" + jsonString(pipelineConflict.getMessage())
                + ",\"status\":\"conflict_running\""
                + ",\"content_hash\":" + jsonString(pipelineConflict.contentHash())
                + ",\"started_at\":" + jsonString(pipelineConflict.startedAt().toString())
                + ",\"heartbeat_age_seconds\":" + pipelineConflict.heartbeatAgeSeconds()
                + ",\"stale_threshold_seconds\":" + pipelineConflict.staleThresholdSeconds()
                + ",\"remedy\":\"wait for the resume window (retry after the heartbeat "
                + "exceeds the stale threshold) or inspect the pipeline row via "
                + "GET /v1/pipeline/state (engine route; requires service auth)\""
                + "}");
            return true;
        }
        // nexus-8vu8p: a pipeline write carried a run_epoch the row no longer has
        // (a newer resume took the run over). Same business-logic-refusal shape as
        // pipelineConflict above; the "remedy" literal is PipelineStaleRunException
        // .REMEDY itself, so the two cannot drift.
        var staleRun = staleRun(e);
        if (staleRun != null) {
            log.warn("event={}_pipeline_stale_run {} pipeline_id={} run_epoch={} current_epoch={}",
                event, context, staleRun.pipelineId(), staleRun.runEpoch(), staleRun.currentEpoch());
            send(exchange, 409,
                "{\"error\":" + jsonString(staleRun.getMessage())
                + ",\"status\":\"stale_run\""
                + ",\"pipeline_id\":" + staleRun.pipelineId()
                + ",\"content_hash\":" + jsonString(staleRun.contentHash())
                + ",\"run_epoch\":" + staleRun.runEpoch()
                + ",\"current_epoch\":" + staleRun.currentEpoch()
                + ",\"remedy\":" + jsonString(dev.nexus.service.db.PipelineStaleRunException.REMEDY)
                + "}");
            return true;
        }
        // RDR-204 Phase 1 (bead nexus-ft04v.7): the seven stub-insert paths that used
        // to auto-create a blank-attribute catalog_collections row on first write are
        // retired. CollectionRegistry.requireRegistered throws this INSTEAD of writing
        // one, from inside the repository's TenantScope.withTenant lambda before any
        // mutating statement runs — same business-logic-refusal shape as identityConflict
        // and pipelineConflict above, no SQLSTATE, mapped ahead of the generic class-23
        // branch. 422 (not 409): this is a well-formed request that cannot be processed
        // because a precondition — registration — is unmet, the same story as the
        // profile-mismatch 422 (Technical Design step 2), not a genuine conflict.
        var unregisteredCollection = unregisteredCollection(e);
        if (unregisteredCollection != null) {
            log.warn("event={}_unregistered_collection {} tenant={} collection={}",
                event, context, unregisteredCollection.tenant(), unregisteredCollection.collection());
            // nexus-bgvnx: "reason" is the stable, machine-readable key. A client that
            // needs to recognise this refusal (the write path's register-and-retry, the
            // read tools' "does not exist" rewrite) keys on it instead of the prose in
            // "error". The prose STAYS, unchanged: a client that predates "reason"
            // still matches "is not registered" in it.
            send(exchange, 422,
                "{\"error\":" + jsonString(unregisteredCollection.getMessage())
                + ",\"reason\":\"" + UNREGISTERED_COLLECTION_REASON + "\""
                + ",\"tenant\":" + jsonString(unregisteredCollection.tenant())
                + ",\"collection\":" + jsonString(unregisteredCollection.collection())
                + ",\"remedy\":\"register the collection first via "
                + "POST /v1/catalog/collections/upsert\""
                + "}");
            return true;
        }
        // nexus-4a8pn taxonomy-017: the doc_count posture tripwire (RAISE EXCEPTION
        // inside topics_doc_count_recount_ins/_del, SQLSTATE P0001, fired when the
        // calling session's nexus.tenant GUC does not cover the tenant_id(s) a
        // topic_assignments INSERT/DELETE just touched) is a caller-diagnosable
        // posture refusal, not a server fault -- typed 409 ahead of the generic
        // class-23 branch below (P0001 is not class 23 anyway, but grouped here
        // with the other business-refusal branches) so the remedy text already
        // embedded in the PG message reaches the CLIENT, not only the server log.
        // Narrowly scoped to this one message prefix: every OTHER P0001 (any other
        // plpgsql RAISE EXCEPTION anywhere else in this codebase) keeps the
        // generic-500 policy unchanged.
        String tripwireDetail = docCountPostureTripwireMessage(e);
        if (tripwireDetail != null) {
            log.warn("event={}_doc_count_posture_tripwire {} error={}", event, context, tripwireDetail);
            send(exchange, 409,
                "{\"error\":\"doc_count posture tripwire\",\"sqlstate\":\""
                + SQLSTATE_RAISE_EXCEPTION + "\",\"detail\":" + jsonString(tripwireDetail) + "}");
            return true;
        }
        // RDR-225 (nexus-3wh8d.13): the partitioned-chunks refusals, all ahead of the generic class-23
        // walk. Each is raised by the repository before its statement runs (no SQLSTATE) except the
        // PostgreSQL no-partition error, which IS a 23514 and must be told apart from a CHECK violation.
        var collectionModelMismatch = collectionModelMismatch(e);
        if (collectionModelMismatch != null) {
            log.warn("event={}_collection_model_mismatch {} source={} source_model={} target={} target_model={}",
                event, context, collectionModelMismatch.sourceCollection(), collectionModelMismatch.sourceModel(),
                collectionModelMismatch.targetCollection(), collectionModelMismatch.targetModel());
            send(exchange, 409,
                "{\"error\":" + jsonString(collectionModelMismatch.getMessage())
                + ",\"reason\":\"" + COLLECTION_MODEL_MISMATCH_REASON + "\""
                + ",\"source_collection\":" + jsonString(collectionModelMismatch.sourceCollection())
                + ",\"source_model\":" + jsonString(collectionModelMismatch.sourceModel())
                + ",\"target_collection\":" + jsonString(collectionModelMismatch.targetCollection())
                + ",\"target_model\":" + jsonString(collectionModelMismatch.targetModel())
                + "}");
            return true;
        }
        var modelPartitionMissing = modelPartitionMissing(e);
        if (modelPartitionMissing != null) {
            if (modelPartitionMissing.registered()) {
                // The model has an embedding_models row and no partition: its changeset forgot
                // create_model_partition. Nothing a caller can change, so a 500 that names the model.
                log.error("event={}_model_partition_missing {} model={} parent={}",
                    event, context, modelPartitionMissing.model(), modelPartitionMissing.parent());
                send(exchange, 500,
                    "{\"error\":" + jsonString(modelPartitionMissing.getMessage())
                    + ",\"reason\":\"" + MODEL_PARTITION_MISSING_REASON + "\""
                    + ",\"model\":" + jsonString(modelPartitionMissing.model()) + "}");
            } else {
                log.warn("event={}_unregistered_embedding_model {} model={} parent={}",
                    event, context, modelPartitionMissing.model(), modelPartitionMissing.parent());
                send(exchange, 422,
                    "{\"error\":" + jsonString(modelPartitionMissing.getMessage())
                    + ",\"reason\":\"" + UNREGISTERED_EMBEDDING_MODEL_REASON + "\""
                    + ",\"model\":" + jsonString(modelPartitionMissing.model()) + "}");
            }
            return true;
        }
        var tenantCreationBusy = tenantCreationBusy(e);
        if (tenantCreationBusy != null) {
            log.warn("event={}_tenant_creation_busy {} tenant={} attempts={}",
                event, context, tenantCreationBusy.tenant(), tenantCreationBusy.attempts());
            exchange.getResponseHeaders().set("Retry-After", Long.toString(TENANT_CREATION_RETRY_AFTER_SECONDS));
            send(exchange, 503,
                "{\"error\":" + jsonString(tenantCreationBusy.getMessage())
                + ",\"reason\":\"" + TENANT_CREATION_BUSY_REASON + "\""
                + ",\"retry_after_seconds\":" + TENANT_CREATION_RETRY_AFTER_SECONDS + "}");
            return true;
        }
        String noPartition = noPartitionMessage(e);
        if (noPartition != null) {
            String[] keyed = partitionKeyOf(noPartition);   // {tenant-or-null, model-or-null}
            log.error("event={}_tenant_partition_missing {} tenant={} model={} error={}",
                event, context, keyed[0], keyed[1], noPartition);
            send(exchange, 500,
                "{\"error\":" + jsonString("no partition exists for tenant "
                    + (keyed[0] == null ? "(not reported by the server)" : "'" + keyed[0] + "'")
                    + (keyed[1] == null ? "" : " and embedding model '" + keyed[1] + "'")
                    + "; the write was refused before it landed. This is an engine invariant, not a request error")
                + ",\"reason\":\"" + TENANT_PARTITION_MISSING_REASON + "\""
                + (keyed[0] == null ? "" : ",\"tenant\":" + jsonString(keyed[0]))
                + (keyed[1] == null ? "" : ",\"model\":" + jsonString(keyed[1]))
                + ",\"remedy\":\"an operator runs nexus.create_tenant_partitions for the tenant\"}");
            return true;
        }
        String sqlState = sqlState23(e);
        if (sqlState != null) {
            // nexus-7e057: class-23 integrity violations are caller errors (bad FK id
            // etc.), not server faults — typed 409 ahead of the generic 500.
            // nexus-0ehwe item 6: carry the CONSTRAINT NAME. A bare "integrity
            // constraint violation" is undiagnosable from the client — it cost
            // the entire nexus-pbawi investigation, where the real answer
            // (catalog_documents_pkey, i.e. a TUMBLER collision, not the
            // source_uri arbiter the insert declares) was sitting in the
            // driver's exception the whole time and was being discarded here.
            String constraint = constraintName(e);
            log.warn("event={}_integrity_violation {} sqlstate={} constraint={} error={}",
                event, context, sqlState, constraint, e.getMessage());
            String remedy = ttlDaysCheckRemedy(constraint);
            send(exchange, 409,
                "{\"error\":\"integrity constraint violation\",\"sqlstate\":"
                + jsonString(sqlState)
                + (constraint == null ? "" : ",\"constraint\":" + jsonString(constraint))
                + (remedy == null ? "" : ",\"remedy\":" + jsonString(remedy))
                + "}");
            return true;
        }
        String dataExceptionState = sqlStateDataException(e);
        if (dataExceptionState != null) {
            // nexus-dmrkm: class-22 data exceptions (22021 the NUL byte Postgres
            // text/jsonb cannot store — nexus-yvzhz; 22P05 untranslatable
            // character; siblings) are caller-data problems, not server faults —
            // typed 422 ahead of the generic 500, mirroring the class-23 branch
            // above. Unlike a constraint violation, Postgres's encoding-layer
            // rejection carries no column/table context in the driver exception
            // (it fires below the row, at client-encoding conversion), so the
            // body can only name the SQLSTATE, not the specific field — the raw
            // driver message (which does include the offending byte) goes to the
            // log only, never the client, same info-disclosure discipline as the
            // class-23 branch.
            log.warn("event={}_data_exception {} sqlstate={} error={}",
                event, context, dataExceptionState, e.getMessage());
            send(exchange, 422,
                "{\"error\":\"unrepresentable data rejected by the database\",\"sqlstate\":"
                + jsonString(dataExceptionState) + "}");
            return true;
        }
        return false;
    }

    /**
     * The violated constraint's name, walking the cause chain, or null
     * (nexus-0ehwe item 6).
     *
     * <p>Delegates to {@link dev.nexus.service.db.SqlConstraints#violated}. The
     * extraction moved to the {@code db} package when the repository layer also had to
     * branch on WHICH unique key fired (nexus-0ehwe arbiter class) — one implementation,
     * so a driver-shape change cannot fix the 409 body and leave the repository's
     * converge-vs-refuse decision reading a stale copy.
     */
    static String constraintName(Throwable e) {
        return dev.nexus.service.db.SqlConstraints.violated(e);
    }

    /**
     * A caller-facing remedy hint for a {@code *_ttl_days_positive_chk}
     * violation, or {@code null} for any other constraint (nexus-tk070.p6a
     * fix-pass, substantive-critic MINOR finding, 2026-08-20).
     *
     * <p>{@code memory_ttl_days_positive_chk} (memory-003-ttl-days.xml) and
     * {@code plans_ttl_days_positive_chk} (plans-003-ttl-days.xml) both mean
     * the identical thing: {@code ttl_days<=0} was rejected. {@code memory}'s
     * {@code /put}/{@code /put_or_merge} paths never reach this branch at all
     * — {@code MemoryHandler.requirePositiveOrNullTtl} rejects the same input
     * earlier, with a 400 that already names the fix. {@code plans} has no
     * such boundary validation by design (RDR-194 D5 names only memory_put +
     * frecency as needing a NEW loud rejection — see plans-003-ttl-days.xml's
     * header for that scope decision), so an explicit {@code ttl=0} to
     * {@code POST /v1/plans/save} falls all the way through to THIS class-23
     * branch and got only a bare "integrity constraint violation" + the raw
     * constraint name — diagnosable by a developer, not self-explanatory to a
     * caller. A wildcard match on the constraint-name SUFFIX (rather than an
     * enumerated {@code memory}/{@code plans} pair) covers every current and
     * future {@code ttl_days} CHECK the same way, mirroring
     * {@link #pipelineConflict}'s own textual remedy field rather than
     * inventing a new response shape.
     */
    static String ttlDaysCheckRemedy(String constraint) {
        if (constraint != null && constraint.endsWith("_ttl_days_positive_chk")) {
            return "ttl_days must be omitted, null, or a positive integer number "
                + "of days — 0 does NOT mean permanent; null does";
        }
        return null;
    }

    /**
     * The {@link dev.nexus.service.db.CatalogIdentityConflictException} in {@code t}'s
     * cause chain, or null. Depth-bounded like the sibling walks — the repository throws
     * it inside {@code TenantScope.withTenant}, which wraps on the way out.
     */
    static dev.nexus.service.db.CatalogIdentityConflictException identityConflict(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof dev.nexus.service.db.CatalogIdentityConflictException ce) {
                return ce;
            }
        }
        return null;
    }

    /**
     * The {@link dev.nexus.service.db.PipelineConflictException} in {@code t}'s cause
     * chain, or null. {@code TenantScope} propagates a {@code RuntimeException} thrown
     * from inside {@code withTenant}'s work lambda UNCHANGED (no wrapping), so this
     * exception is typically the top-level throwable itself — the walk still covers the
     * general case for symmetry with {@link #identityConflict}.
     */
    static dev.nexus.service.db.PipelineConflictException pipelineConflict(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof dev.nexus.service.db.PipelineConflictException pe) {
                return pe;
            }
        }
        return null;
    }

    /** The {@link dev.nexus.service.db.PipelineStaleRunException} in {@code t}'s
     *  cause chain, or null (nexus-8vu8p; same walk as {@link #pipelineConflict}). */
    static dev.nexus.service.db.PipelineStaleRunException staleRun(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof dev.nexus.service.db.PipelineStaleRunException se) {
                return se;
            }
        }
        return null;
    }

    /**
     * The {@link dev.nexus.service.db.UnregisteredCollectionException} in {@code t}'s
     * cause chain, or null (RDR-204 Phase 1, bead nexus-ft04v.7). {@code
     * CollectionRegistry.requireRegistered} throws it inside {@code TenantScope
     * .withTenant}'s work lambda, which wraps on the way out — same walk shape as
     * {@link #identityConflict} and {@link #pipelineConflict}.
     */
    static dev.nexus.service.db.UnregisteredCollectionException unregisteredCollection(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof dev.nexus.service.db.UnregisteredCollectionException uce) {
                return uce;
            }
        }
        return null;
    }

    /** {@code reason} on the 409 for a re-home across embedding models (RDR-225). Wire contract. */
    static final String COLLECTION_MODEL_MISMATCH_REASON = "collection_model_mismatch";

    /** {@code reason} on the 422 for a write naming a model that is registered nowhere (RDR-225). Wire contract. */
    static final String UNREGISTERED_EMBEDDING_MODEL_REASON = "unregistered_embedding_model";

    /** {@code reason} on the 500 for a registered model whose partition was never created (RDR-225). Wire contract. */
    static final String MODEL_PARTITION_MISSING_REASON = "model_partition_missing";

    /** {@code reason} on the 500 for PostgreSQL's "no partition of relation found for row" (RDR-225). Wire contract. */
    static final String TENANT_PARTITION_MISSING_REASON = "tenant_partition_missing";

    /** {@code reason} on the retryable 503 for a first token that waited on partition-creation locks (RDR-225). Wire contract. */
    static final String TENANT_CREATION_BUSY_REASON = "tenant_creation_busy";

    /** {@code Retry-After} on {@link #TENANT_CREATION_BUSY_REASON}: one {@code lock_timeout} of the creation. */
    static final long TENANT_CREATION_RETRY_AFTER_SECONDS = 2;

    /** The {@link dev.nexus.service.db.CollectionModelMismatchException} in {@code t}'s cause chain, or null. */
    static dev.nexus.service.db.CollectionModelMismatchException collectionModelMismatch(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof dev.nexus.service.db.CollectionModelMismatchException m) {
                return m;
            }
        }
        return null;
    }

    /** The {@link dev.nexus.service.db.ModelPartitions.ModelPartitionMissingException} in {@code t}'s cause chain, or null. */
    static dev.nexus.service.db.ModelPartitions.ModelPartitionMissingException modelPartitionMissing(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof dev.nexus.service.db.ModelPartitions.ModelPartitionMissingException m) {
                return m;
            }
        }
        return null;
    }

    /** The {@link dev.nexus.service.db.TenantCreationBusyException} in {@code t}'s cause chain, or null. */
    static dev.nexus.service.db.TenantCreationBusyException tenantCreationBusy(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof dev.nexus.service.db.TenantCreationBusyException b) {
                return b;
            }
        }
        return null;
    }

    /** The message of PostgreSQL's "no partition of relation ... found for row" error, or null. */
    private static final String NO_PARTITION_MESSAGE = "no partition of relation";

    /**
     * RDR-225: the driver message of a SQLSTATE 23514 whose text is "no partition of relation ... found
     * for row" (tuple routing found no partition), or null. The dimension CHECK on a model partition
     * raises 23514 too, so the SQLSTATE alone cannot tell them apart; this keys on the message, as the
     * doc-count tripwire does on its prefix. The same locale caveat applies: a server reporting in
     * another language degrades this to the class-23 409 (wrong status, never a wrong success).
     */
    static String noPartitionMessage(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLException se
                    && "23514".equals(se.getSQLState())
                    && se.getMessage() != null
                    && se.getMessage().contains(NO_PARTITION_MESSAGE)) {
                return se.getMessage();
            }
        }
        return null;
    }

    private static final java.util.regex.Pattern PARTITION_KEY_DETAIL = java.util.regex.Pattern.compile(
        "Partition key of the failing row contains \\(([^)]*)\\) = \\((.*)\\)");

    /**
     * The tenant and model out of the server's detail line, {@code Partition key of the failing row
     * contains (embedding_model, tenant_id) = (m, t)} (the model partition missing) or
     * {@code (tenant_id) = (t)} (a leaf missing under an existing model partition). Either element is
     * null when the detail does not carry it.
     */
    static String[] partitionKeyOf(String message) {
        var m = PARTITION_KEY_DETAIL.matcher(message);
        String tenant = null;
        String model = null;
        if (m.find()) {
            String[] cols = m.group(1).split(", ");
            String[] vals = m.group(2).split(", ", cols.length);
            for (int i = 0; i < cols.length && i < vals.length; i++) {
                if ("tenant_id".equals(cols[i])) tenant = vals[i];
                if ("embedding_model".equals(cols[i])) model = vals[i];
            }
        }
        return new String[] {tenant, model};
    }

    /** The {@code reason} value on the typed 422 for an unregistered collection
     *  (nexus-bgvnx). Part of the wire contract: clients key on it. */
    static final String UNREGISTERED_COLLECTION_REASON = "unregistered_collection";

    /** The {@code reason} value on the typed 422 for an ownerless chunk write
     *  (RDR-223 Phase 3 Step 2, nexus-z0o2p.24). Part of the wire contract: clients key on it. */
    static final String OWNERLESS_CHUNK_WRITE_REASON = "ownerless_chunk_write";

    /** The {@code reason} value on the typed 503 of {@code POST /v1/vectors/gc/quarantine-restore} when the
     *  restore's lock or statement bound tripped (nexus-wbfpw.49). Retryable; the body also carries
     *  {@code retry_after_seconds}, {@code nothing_moved}, {@code audit_ids} and {@code moved_chashes}
     *  (nexus-wbfpw.55: false and non-empty when an earlier quarantine sibling of the call had committed).
     *  Part of the wire contract. */
    static final String QUARANTINE_RESTORE_BUSY_REASON = "quarantine_restore_busy";

    /** PostgreSQL SQLSTATE for a plain {@code RAISE EXCEPTION} with no explicit
     *  {@code ERRCODE} (the {@code raise_exception} default class). */
    private static final String SQLSTATE_RAISE_EXCEPTION = "P0001";

    /** The message prefix {@code topics_doc_count_recount_ins}/{@code _del}'s
     *  taxonomy-017 posture-tripwire {@code RAISE EXCEPTION} always carries
     *  (see {@link #docCountPostureTripwireMessage}). */
    private static final String DOC_COUNT_TRIPWIRE_MESSAGE_PREFIX = "topics_doc_count_recount_";

    /**
     * Walk the cause chain for a {@link SQLException} whose SQLSTATE is {@code
     * P0001} and whose message starts with {@code topics_doc_count_recount_} —
     * the nexus-4a8pn taxonomy-017-doc-count-posture-tripwire.xml {@code RAISE
     * EXCEPTION} inside {@code topics_doc_count_recount_ins}/{@code _del}, fired
     * when the calling session's {@code nexus.tenant} GUC does not cover the
     * tenant_id(s) a {@code topic_assignments} INSERT/DELETE just touched (see
     * that changeset's own header for the full derivation). Returns the
     * offending exception's message (the PG error text, already naming the
     * tenant(s) and the remedy) or {@code null} if no such cause exists.
     *
     * <p>Narrowly scoped to this ONE message prefix, deliberately: {@code
     * P0001} is the generic code for EVERY plpgsql {@code RAISE EXCEPTION} in
     * this codebase that does not set an explicit {@code ERRCODE} — matching on
     * the bare SQLSTATE alone would reclassify any other such RAISE as a typed
     * 409 too. Same cause-chain-walk shape as {@link #sqlState23}.
     */
    static String docCountPostureTripwireMessage(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLException se
                    && SQLSTATE_RAISE_EXCEPTION.equals(se.getSQLState())
                    && se.getMessage() != null
                    && se.getMessage().startsWith(DOC_COUNT_TRIPWIRE_MESSAGE_PREFIX)) {
                return se.getMessage();
            }
        }
        return null;
    }

    /**
     * Reject a caller-supplied JSON-string field that fails to parse. Shared by every
     * handler writing a jsonb-typed column (schema type-hygiene arc, epic nexus-cefa1)
     * so the same malformed body 400s at the handler instead of reaching the repository
     * and aborting mid-write as a class-22 SQLSTATE 422.
     *
     * <p>Originated as a private method in {@code AspectHandler} (nexus-cefa1.4, P3) for
     * {@code extras}/{@code salient_sentences}; extracted here when {@code PlanHandler}
     * (nexus-cefa1.5, P4) needed the identical check for {@code plan_json}/{@code
     * default_bindings} — ONE shared helper, not a second copy.
     *
     * <p>A blank/null value is fine (either the column's own {@code NULLIF(...,'')
     * ::jsonb} USING clause maps it to SQL NULL, or the caller's own required-field
     * check already rejected a blank required value); a non-blank value that fails
     * {@code ObjectMapper#readTree} is not valid JSON and throws here.
     *
     * @param mapper the calling handler's Jackson {@code ObjectMapper}
     * @param field  the field name, for the error message
     * @param value  the caller-supplied value (only a non-blank {@code String} is checked)
     * @throws IllegalArgumentException if value is a non-blank String that is not valid JSON
     */
    public static void rejectMalformedJson(com.fasterxml.jackson.databind.ObjectMapper mapper,
                                            String field, Object value) {
        if (!(value instanceof String s) || s.isBlank()) return;
        try {
            mapper.readTree(s);
        } catch (Exception e) {
            throw new IllegalArgumentException(
                "field '" + field + "' must be valid JSON: " + e.getMessage());
        }
    }

    /** PostgreSQL SQLSTATE for insufficient_privilege — what an RLS refusal raises. */
    private static final String SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501";

    /**
     * True when *t* wraps a PostgreSQL row-level-security REFUSAL of a specific row,
     * as opposed to a genuine privilege misconfiguration.
     *
     * <p>Bead nexus-asaod. A fidelity-ETL import carries a CLIENT-SUPPLIED id
     * (``POST /v1/taxonomy/import/topic`` preserves ids verbatim so a migration
     * round-trips). ``nexus.topics`` has a global ``BIGSERIAL`` primary key — global
     * because its self-referential parent FK (``fk_topics_parent_tenant`` since
     * RDR-194 P5a/taxonomy-014-2, formerly ``topics_parent_fk``) means a composite
     * ``(tenant_id, id)`` PRIMARY KEY would force every ``parent_id`` to carry a
     * tenant too. RDR-194 D4 instead added a separate ``UNIQUE (tenant_id, id)``
     * alongside the unchanged global PK and repointed the parent FK onto that
     * composite — the PK itself never moved.
     * When two tenants supply the same id, the second INSERT is refused by the RLS
     * policy rather than by the PK, because RLS is evaluated first: the row exists but
     * is invisible to this tenant.
     *
     * <p>That is tenant isolation WORKING, and it is a caller-resolvable conflict — so
     * it deserves a 409, not the opaque 500 it produced before this fix. It does NOT
     * come through {@link #sqlState23}: an RLS refusal is SQLSTATE 42501
     * (insufficient_privilege), not class 23, so the shared ladder correctly declined
     * it and it fell through.
     *
     * <p>DISCRIMINATION, and why it is not a bare SQLSTATE check: 42501 ALSO fires when
     * the connecting role genuinely lacks a table privilege — a deployment fault that
     * must stay a 500 so it is not silently reported to callers as their conflict.
     * The PostgreSQL RLS refusal is distinguished by its message ("row-level security
     * policy"), so both signals are required. This couples to a PG message string; if
     * a future PG release rewords it this returns false and the behaviour degrades to
     * the previous 500 — wrong status, never a wrong success. The paired
     * ``rejectsCrossTenantIdWith409`` test pins the live wording so the coupling
     * cannot rot silently.
     *
     * <p>LOCALE COUPLING (review, 2026-07-25): the message match assumes the PG
     * server reports in English. A server with a non-English {@code lc_messages}
     * localises "row-level security policy", the match silently fails, and every
     * RLS refusal degrades back to an opaque 500 — the exact defect this exists to
     * remove, reappearing as a config-dependent regression rather than a crash.
     * Acceptable for a controlled hosted instance; state it rather than rediscover
     * it. Same fragility class as a future PG rewording.
     */
    public static boolean isRlsRowRejection(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLException se
                    && SQLSTATE_INSUFFICIENT_PRIVILEGE.equals(se.getSQLState())) {
                String msg = se.getMessage();
                if (msg != null && msg.contains("row-level security policy")) {
                    return true;
                }
            }
        }
        return false;
    }
}
