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
            send(exchange, 409,
                "{\"error\":\"integrity constraint violation\",\"sqlstate\":"
                + jsonString(sqlState)
                + (constraint == null ? "" : ",\"constraint\":" + jsonString(constraint))
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

    /** PostgreSQL SQLSTATE for insufficient_privilege — what an RLS refusal raises. */
    private static final String SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501";

    /**
     * True when *t* wraps a PostgreSQL row-level-security REFUSAL of a specific row,
     * as opposed to a genuine privilege misconfiguration.
     *
     * <p>Bead nexus-asaod. A fidelity-ETL import carries a CLIENT-SUPPLIED id
     * (``POST /v1/taxonomy/import/topic`` preserves ids verbatim so a migration
     * round-trips). ``nexus.topics`` has a global ``BIGSERIAL`` primary key — global
     * because ``topics_parent_fk`` is self-referential, so a composite
     * ``(tenant_id, id)`` key would force every ``parent_id`` to carry a tenant too.
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
