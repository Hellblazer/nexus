// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.jooq.nexus.tables.records.IdentityRecordsRecord;
import org.jooq.DSLContext;
import org.jooq.JSONB;

import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_ALIASES;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_BINDINGS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_SESSIONS;

/**
 * Row-level helpers of the identity records (RDR-253), all over an already-open, tenant-stamped
 * {@link DSLContext}: lock the identity row, apply a lapse that is due, append a binding row, index a session,
 * render a record. {@link IdentityRepository} and {@link IdentityRetention} compose them inside one
 * transaction each, so the generation compare-and-set and the row it appends commit together.
 */
final class IdentityRows {

    static final int LAPSE_SECONDS = 300;
    static final int RESERVATION_SECONDS = 600;

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() { };

    private IdentityRows() {
    }

    /** The identity row, locked {@code FOR UPDATE}; the compare-and-set on the generation reads it under this lock. */
    static IdentityRecordsRecord lock(DSLContext ctx, String tenant, String id) {
        IdentityRecordsRecord rec = ctx.selectFrom(IDENTITY_RECORDS)
            .where(IDENTITY_RECORDS.TENANT_ID.eq(tenant)).and(IDENTITY_RECORDS.ID.eq(id))
            .forUpdate().fetchOne();
        if (rec == null) {
            throw new IdentityException(404, "unknown_identity", "no identity " + id);
        }
        return rec;
    }

    /** The identity row without a lock, or null. */
    static IdentityRecordsRecord peek(DSLContext ctx, String tenant, String id) {
        return ctx.selectFrom(IDENTITY_RECORDS)
            .where(IDENTITY_RECORDS.TENANT_ID.eq(tenant)).and(IDENTITY_RECORDS.ID.eq(id)).fetchOne();
    }

    /** True when a leased live holder has gone more than 300 seconds without renewing. */
    static boolean lapseDue(IdentityRecordsRecord rec, OffsetDateTime now) {
        return "live".equals(rec.getState()) && IdentityEvidence.LEASED.equals(rec.getLease())
            && rec.getLastRenewal() != null && now.isAfter(rec.getLastRenewal().plusSeconds(LAPSE_SECONDS));
    }

    /**
     * Applies a lapse that is due, on a locked row: state {@code lapsed}, {@code lapsed_at} the instant the lapse
     * was due (last renewal plus 300 seconds, not when it is first read), a {@code lapse} row under the same
     * generation. An {@code unleased} binding never lapses. Returns whether it wrote.
     */
    static boolean lapseIfDue(DSLContext ctx, IdentityRecordsRecord rec, OffsetDateTime now) {
        if (!lapseDue(rec, now)) {
            return false;
        }
        OffsetDateTime due = rec.getLastRenewal().plusSeconds(LAPSE_SECONDS);
        rec.setState("lapsed");
        rec.setLapsedAt(due);
        appendBinding(ctx, rec, "lapse", due, holderEvidence(rec), null);
        rec.update();
        return true;
    }

    /** The evidence the identity's current holder gave, rebuilt from the record, for a row that names no new process. */
    static IdentityEvidence holderEvidence(IdentityRecordsRecord rec) {
        return new IdentityEvidence(rec.getHolderSessionId(), rec.getHolderHost(), rec.getHolderPid(),
            rec.getHolderStartedAt(), rec.getHolderProcStart(), rec.getHolderPane(), rec.getClientVersion(),
            rec.getLease(), null, null);
    }

    /** The current holder as a process, or null when the identity has none. */
    static IdentityEvidence.Process holder(IdentityRecordsRecord rec) {
        if (rec.getHolderHost() == null && rec.getHolderPid() == null && rec.getHolderStartedAt() == null) {
            return null;
        }
        return new IdentityEvidence.Process(rec.getHolderHost(), rec.getHolderPid(), rec.getHolderStartedAt(),
            rec.getHolderProcStart());
    }

    static void setHolder(IdentityRecordsRecord rec, IdentityEvidence ev) {
        rec.setHolderSessionId(ev.sessionId());
        rec.setHolderHost(ev.host());
        rec.setHolderPid(ev.pid());
        rec.setHolderStartedAt(ev.startedAt());
        rec.setHolderProcStart(ev.procStart());
        rec.setHolderPane(ev.pane());
        rec.setLease(ev.lease());
        if (ev.clientVersion() != null) {
            rec.setClientVersion(ev.clientVersion());
        }
    }

    static void clearHolder(IdentityRecordsRecord rec) {
        rec.setHolderSessionId(null);
        rec.setHolderHost(null);
        rec.setHolderPid(null);
        rec.setHolderStartedAt(null);
        rec.setHolderProcStart(null);
        rec.setHolderPane(null);
        rec.setLease(null);
        rec.setLastRenewal(null);
        rec.setLapsedAt(null);
    }

    /**
     * Appends a binding row under the next per-identity sequence number and records it on {@code rec} (the caller
     * updates the record). The caller holds the identity row lock, so two appenders cannot take one number.
     */
    static void appendBinding(DSLContext ctx, IdentityRecordsRecord rec, String event, OffsetDateTime at,
                              IdentityEvidence ev, String detail) {
        int seq = rec.getLastBindingSeq() + 1;
        rec.setLastBindingSeq(seq);
        ctx.insertInto(IDENTITY_BINDINGS)
            .set(IDENTITY_BINDINGS.TENANT_ID, rec.getTenantId())
            .set(IDENTITY_BINDINGS.IDENTITY_ID, rec.getId())
            .set(IDENTITY_BINDINGS.SEQ, seq)
            .set(IDENTITY_BINDINGS.EVENT, event)
            .set(IDENTITY_BINDINGS.GENERATION, rec.getGeneration())
            .set(IDENTITY_BINDINGS.LEASE, ev.hasProcess() ? ev.lease() : rec.getLease())
            .set(IDENTITY_BINDINGS.SESSION_ID, ev.sessionId())
            .set(IDENTITY_BINDINGS.HOST, ev.host())
            .set(IDENTITY_BINDINGS.PID, ev.pid())
            .set(IDENTITY_BINDINGS.STARTED_AT, ev.startedAt())
            .set(IDENTITY_BINDINGS.PROC_START, ev.procStart())
            .set(IDENTITY_BINDINGS.PANE, ev.pane())
            .set(IDENTITY_BINDINGS.CLIENT_VERSION, ev.clientVersion())
            .set(IDENTITY_BINDINGS.PRIOR_SESSION_ID, ev.priorSessionId())
            .set(IDENTITY_BINDINGS.HOLDER_GONE, ev.holderGone() == null ? null : json(ev.holderGone().toMap()))
            .set(IDENTITY_BINDINGS.DETAIL, detail)
            .set(IDENTITY_BINDINGS.AT, at)
            .execute();
    }

    /** Maps a session id to the identity and generation (the session index); a repeated id points at the newest. */
    static void indexSession(DSLContext ctx, String tenant, String sessionId, String id, int generation,
                             OffsetDateTime at) {
        if (sessionId == null || sessionId.isBlank()) {
            return;
        }
        ctx.insertInto(IDENTITY_SESSIONS)
            .set(IDENTITY_SESSIONS.TENANT_ID, tenant)
            .set(IDENTITY_SESSIONS.SESSION_ID, sessionId)
            .set(IDENTITY_SESSIONS.IDENTITY_ID, id)
            .set(IDENTITY_SESSIONS.GENERATION, generation)
            .set(IDENTITY_SESSIONS.AT, at)
            .onConflict(IDENTITY_SESSIONS.TENANT_ID, IDENTITY_SESSIONS.SESSION_ID)
            .doUpdate()
            .set(IDENTITY_SESSIONS.IDENTITY_ID, id)
            .set(IDENTITY_SESSIONS.GENERATION, generation)
            .set(IDENTITY_SESSIONS.AT, at)
            .execute();
    }

    /** The live alias of an identity, or null. */
    static String liveAlias(DSLContext ctx, String tenant, String id) {
        return ctx.select(IDENTITY_ALIASES.NAME).from(IDENTITY_ALIASES)
            .where(IDENTITY_ALIASES.TENANT_ID.eq(tenant)).and(IDENTITY_ALIASES.IDENTITY_ID.eq(id))
            .and(IDENTITY_ALIASES.STATE.eq("live")).fetchOne(IDENTITY_ALIASES.NAME);
    }

    static JSONB json(Map<String, Object> m) {
        try {
            return JSONB.valueOf(MAPPER.writeValueAsString(m));
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    static Map<String, Object> parse(JSONB b) {
        if (b == null) {
            return null;
        }
        try {
            return MAPPER.readValue(b.data(), MAP_TYPE);
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The reserved-until instant for another host: 600 seconds after the lapse was due; null when nothing is reserved. */
    static OffsetDateTime reservedUntil(IdentityRecordsRecord rec) {
        return "lapsed".equals(rec.getState()) && rec.getLapsedAt() != null
            ? rec.getLapsedAt().plusSeconds(RESERVATION_SECONDS) : null;
    }

    /** One identity as the routes return it. */
    static Map<String, Object> view(IdentityRecordsRecord rec, String alias) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", rec.getId());
        m.put("kind", rec.getKind());
        m.put("parent", rec.getParent());
        m.put("generation", rec.getGeneration());
        m.put("state", rec.getState());
        m.put("liveness", liveness(rec));
        m.put("name", alias);
        m.put("display_name", rec.getDisplayName());
        m.put("created_by", rec.getCreator());
        m.put("created_at", str(rec.getCreatedAt()));
        m.put("ended_at", str(rec.getEndedAt()));
        m.put("lease", rec.getLease());
        m.put("last_renewal", str(rec.getLastRenewal()));
        m.put("lapsed_at", str(rec.getLapsedAt()));
        m.put("reserved_until", str(reservedUntil(rec)));
        m.put("gone_at", str(rec.getGoneAt()));
        m.put("client_version", rec.getClientVersion());
        IdentityEvidence.Process h = holder(rec);
        if (h == null) {
            m.put("holder", null);
        } else {
            Map<String, Object> hm = new LinkedHashMap<>(h.toMap());
            hm.put("session_id", rec.getHolderSessionId());
            hm.put("pane", rec.getHolderPane());
            m.put("holder", hm);
        }
        return m;
    }

    /**
     * What is known about the holder: {@code live} (a leased holder renewing), {@code lapsed}, {@code unknown} for an
     * {@code unleased} binding (its MCP server cannot renew, so the engine cannot say), else the state.
     */
    static String liveness(IdentityRecordsRecord rec) {
        if ("live".equals(rec.getState())) {
            return IdentityEvidence.UNLEASED.equals(rec.getLease()) ? "unknown" : "live";
        }
        return rec.getState();
    }

    static String str(OffsetDateTime t) {
        return t == null ? null : t.toInstant().toString();
    }
}
