// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.records.IdentityRecordsRecord;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_ALIASES;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_BINDINGS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_COUNTERS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_SESSIONS;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;

/**
 * RDR-253 Step 1: the engine records of session identity and the operations that change them.
 *
 * <p>An identity is a long-lived seat (a name that many sessions hold one after another), an ephemeral
 * identity (a launch that named no seat) or a delegated one (a {@code claude -p} child). A <em>generation</em>
 * counts the processes that have held it. {@link #bind} is a compare-and-set on that generation: it locks the
 * identity row, compares the caller's expected generation, applies the policy for the event, appends a binding
 * row and updates the record in one transaction, so two simultaneous binds with one expected generation cannot
 * both win. {@code TenantScope} stamps the tenant for row-level security on every call, and the clock is
 * injected so the 300-second lapse, the 10-minute reservation, the 7-day alias grace and the 30-day retention
 * are tested with a fixed one.
 *
 * <p>The decisions the RDR left open, as built:
 * <ul>
 *   <li>A {@code gone} row carries the dead holder's host, pid, {@code started_at} and {@code proc_start} (the
 *       verdict) both as the row's process fields and in {@code holder_gone}, the holder's session id, and the
 *       time the engine recorded it.</li>
 *   <li>A second {@code gone} for the same holder is a no-op that answers {@code recorded:false}; no second row.</li>
 *   <li>The holder-match guard reads the identity's current holder, which a {@code lapse} row leaves in place (a
 *       lapsed holder can be reported gone) and an {@code end} or {@code release} row clears (nothing is there to
 *       mark, so {@code not_held}). A verdict naming another process is {@code holder_mismatch}.</li>
 *   <li>The 30-day clock of an identity lapsed with its holder gone starts at the engine time the {@code gone} row
 *       was recorded; a revive or a rebind clears it. A delegated identity has no registry file for a holder
 *       check to find, so 30 days lapsed is enough for it.</li>
 *   <li>A renewal and a refresh write no binding row unless they revive a lapsed identity; only {@code revive}
 *       and the events of the policy table are rows.</li>
 *   <li>(Step 2) A stale generation on an event of a process that already holds the identity ({@code clear},
 *       {@code fork}, {@code refresh}, {@code end}) or on {@code renew} is {@code 409 generation_superseded}, the
 *       client heartbeat's stop-writing signal and the code the fences answer; a stale generation on an event
 *       that takes the seat ({@code start}, {@code resume}, {@code takeover}), on {@code release} or on
 *       {@code gone} is the lost compare-and-set, {@code 409 generation_conflict}.</li>
 * </ul>
 */
public final class IdentityRepository {

    private static final Logger log = LoggerFactory.getLogger(IdentityRepository.class);

    static final Set<String> BIND_EVENTS =
        Set.of("start", "resume", "clear", "fork", "takeover", "release", "refresh", "end");
    static final Set<String> CREATED_BY = Set.of("sam", "restart", "session", "dispatch");
    static final int ALIAS_GRACE_DAYS = 7;

    private static final Pattern NAME = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$");
    private static final Pattern RESERVED = Pattern.compile("^[iI][0-9]+$");
    private static final Pattern ID = Pattern.compile("^i[0-9]+$");

    private final TenantScope scope;
    private final Clock clock;
    private final IdentityRetention retention;
    private final IdentityMemberships memberships;
    private final IdentityWork work;

    public IdentityRepository(TenantScope scope, Clock clock) {
        this(scope, clock, IdentityRetention.PRUNE_BATCH, IdentityRetention.PRUNE_MAX_BATCHES);
    }

    /**
     * As above, with the retention prune's batch size and the most batches a pass runs for each prune (production:
     * 5000 and 20), so a test can show a backlog larger than one batch progresses over passes.
     */
    public IdentityRepository(TenantScope scope, Clock clock, int pruneBatch, int pruneMaxBatches) {
        this.scope = scope;
        this.clock = clock;
        this.retention = new IdentityRetention(scope, clock, pruneBatch, pruneMaxBatches);
        this.memberships = new IdentityMemberships(scope, clock);
        this.work = new IdentityWork(scope, clock);
    }

    private OffsetDateTime now() {
        return OffsetDateTime.now(clock);
    }

    // ── names and ids ───────────────────────────────────────────────────────

    /** Throws 400 unless {@code name} is a usable alias: the mailbox name charset, and never {@code i<digits>}. */
    static void requireAliasName(String name) {
        if (name == null || !NAME.matcher(name).matches()) {
            throw new IdentityException(400, "bad_name",
                "a name starts with a letter or digit and holds letters, digits, '.', '_' and '-' (at most 64)");
        }
        if (RESERVED.matcher(name).matches()) {
            throw new IdentityException(400, "reserved_name", "names that look like identity ids (i<digits>) are reserved");
        }
    }

    /** The longest free-text field a caller may write (display name, host, pane, client version, ...). */
    static final int MAX_TEXT = 256;

    /** Throws 400 when {@code value} is longer than {@link #MAX_TEXT}; null passes. */
    static String bounded(String value, String field) {
        if (value != null && value.length() > MAX_TEXT) {
            throw new IdentityException(400, "bad_field", "'" + field + "' is longer than " + MAX_TEXT + " characters");
        }
        return value;
    }

    static void requireId(String id) {
        if (id == null || !ID.matcher(id).matches()) {
            throw new IdentityException(400, "bad_identity", "an identity id is i followed by digits: " + id);
        }
    }

    /** The next id of the tenant's counter, which only grows: a number is never issued twice. */
    private static String nextId(DSLContext ctx, String tenant) {
        // The scheduled sweep visits the tenants of nexus.tuple_tenants, and the identity retention is one of its
        // arms: a tenant with identities and no tuple traffic must be visited too, so creating an identity registers it.
        OffsetDateTime seen = OffsetDateTime.now(java.time.ZoneOffset.UTC);
        ctx.insertInto(TUPLE_TENANTS, TUPLE_TENANTS.TENANT_ID, TUPLE_TENANTS.FIRST_SEEN, TUPLE_TENANTS.LAST_SEEN)
            .values(tenant, seen, seen).onConflictDoNothing().execute();
        long n = ctx.insertInto(IDENTITY_COUNTERS, IDENTITY_COUNTERS.TENANT_ID, IDENTITY_COUNTERS.LAST_N)
            .values(tenant, 1L)
            .onConflict(IDENTITY_COUNTERS.TENANT_ID).doUpdate()
            .set(IDENTITY_COUNTERS.LAST_N, IDENTITY_COUNTERS.LAST_N.plus(1L))
            .returning(IDENTITY_COUNTERS.LAST_N).fetchOne().getLastN();
        return "i" + n;
    }

    private static IdentityRecordsRecord insertRecord(DSLContext ctx, String tenant, String id, String kind,
                                                      String parent, String displayName, String createdBy,
                                                      OffsetDateTime now) {
        IdentityRecordsRecord rec = ctx.newRecord(IDENTITY_RECORDS);
        rec.setTenantId(tenant);
        rec.setId(id);
        rec.setKind(kind);
        rec.setParent(parent);
        rec.setGeneration(0);
        rec.setState("unheld");
        rec.setDisplayName(displayName);
        rec.setCreator(createdBy);
        rec.setCreatedAt(now);
        rec.setLastBindingSeq(0);
        rec.insert();
        return rec;
    }

    private static void insertAlias(DSLContext ctx, String tenant, String id, String name, OffsetDateTime now) {
        int n = ctx.insertInto(IDENTITY_ALIASES)
            .set(IDENTITY_ALIASES.TENANT_ID, tenant).set(IDENTITY_ALIASES.NAME, name)
            .set(IDENTITY_ALIASES.IDENTITY_ID, id).set(IDENTITY_ALIASES.STATE, "live")
            .set(IDENTITY_ALIASES.SINCE, now)
            .onConflictDoNothing().execute();
        if (n == 0) {
            throw new IdentityException(409, "name_taken", "the name '" + name + "' is or was a seat's alias",
                Map.of("name", name));
        }
    }

    // ── creation ────────────────────────────────────────────────────────────

    /** Creates a seat: unheld, generation 0, with a live alias. {@code createdBy} is {@code sam} or {@code restart}. */
    public Map<String, Object> createSeat(String tenant, String name, String displayName, String createdBy) {
        requireAliasName(name);
        bounded(displayName, "display_name");
        if (!"sam".equals(createdBy) && !"restart".equals(createdBy)) {
            throw new IdentityException(400, "bad_created_by", "a seat is created by sam or restart");
        }
        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = now();
            String id = nextId(ctx, tenant);
            IdentityRecordsRecord rec = insertRecord(ctx, tenant, id, "seat", null, displayName, createdBy, now);
            insertAlias(ctx, tenant, id, name, now);
            log.info("event=identity_created tenant={} id={} kind=seat name={}", tenant, id, name);
            return IdentityRows.view(rec, name);
        });
    }

    /** Creates an ephemeral identity already bound to the launching process: generation 1, live, a {@code start} row. */
    public Map<String, Object> createEphemeral(String tenant, String displayName, String createdBy, IdentityEvidence ev) {
        requireProcess(ev, true);
        bounded(displayName, "display_name");
        if (!"session".equals(createdBy) && !"restart".equals(createdBy)) {
            throw new IdentityException(400, "bad_created_by", "an ephemeral identity is created by session or restart");
        }
        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = now();
            String id = nextId(ctx, tenant);
            IdentityRecordsRecord rec = insertRecord(ctx, tenant, id, "ephemeral", null, displayName, createdBy, now);
            firstStart(ctx, rec, ev, now);
            log.info("event=identity_created tenant={} id={} kind=ephemeral", tenant, id);
            return IdentityRows.view(rec, null);
        });
    }

    /** Creates a delegated identity under {@code parent} (a {@code claude -p} child), generation 1, live. */
    public Map<String, Object> createDelegated(String tenant, String parent, String displayName, IdentityEvidence ev) {
        requireId(parent);
        requireProcess(ev, false);
        bounded(displayName, "display_name");
        return scope.withTenant(tenant, ctx -> {
            if (IdentityRows.peek(ctx, tenant, parent) == null) {
                throw new IdentityException(404, "unknown_identity", "no parent identity " + parent);
            }
            OffsetDateTime now = now();
            String id = nextId(ctx, tenant);
            IdentityRecordsRecord rec = insertRecord(ctx, tenant, id, "delegated", parent, displayName, "dispatch", now);
            firstStart(ctx, rec, ev, now);
            log.info("event=identity_created tenant={} id={} kind=delegated parent={}", tenant, id, parent);
            return IdentityRows.view(rec, null);
        });
    }

    private static void firstStart(DSLContext ctx, IdentityRecordsRecord rec, IdentityEvidence ev, OffsetDateTime now) {
        rec.setGeneration(1);
        rec.setState("live");
        IdentityRows.setHolder(rec, ev);
        rec.setLastRenewal(now);
        IdentityRows.appendBinding(ctx, rec, "start", now, ev, null);
        rec.update();
        IdentityRows.indexSession(ctx, rec.getTenantId(), ev.sessionId(), rec.getId(), 1, now);
    }

    /** Makes an ephemeral identity a seat with {@code name} as its alias. */
    public Map<String, Object> promote(String tenant, String id, String name) {
        requireId(id);
        requireAliasName(name);
        return scope.withTenant(tenant, ctx -> {
            IdentityRecordsRecord rec = IdentityRows.lock(ctx, tenant, id);
            if (!"ephemeral".equals(rec.getKind())) {
                throw new IdentityException(409, "not_ephemeral", id + " is a " + rec.getKind() + ", not an ephemeral identity");
            }
            OffsetDateTime now = now();
            insertAlias(ctx, tenant, id, name, now);
            rec.setKind("seat");
            rec.update();
            return IdentityRows.view(rec, name);
        });
    }

    // ── bind ────────────────────────────────────────────────────────────────

    private static void requireProcess(IdentityEvidence ev, boolean needSession) {
        if (ev == null || !ev.hasProcess() || (needSession && (ev.sessionId() == null || ev.sessionId().isBlank()))) {
            throw new IdentityException(400, "missing_evidence",
                needSession ? "session_id, host, pid and started_at are required" : "host, pid and started_at are required");
        }
    }

    private static IdentityException conflict(IdentityRecordsRecord rec, int expected) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("expected_generation", expected);
        d.put("current_generation", rec.getGeneration());
        d.put("state", rec.getState());
        return new IdentityException(409, "generation_conflict",
            "the identity is at generation " + rec.getGeneration() + ", not " + expected, d);
    }

    /**
     * The refusal of a holder that has been superseded (RDR-253 Step 2): the identity is at a later generation than
     * the caller's. Distinct from {@link #conflict}, which is also the answer to a lost compare-and-set on
     * {@code start}: this one is the client heartbeat's stop-writing signal, and the fences answer the same code.
     */
    static IdentityException superseded(IdentityRecordsRecord rec, int expected) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("expected_generation", expected);
        d.put("current_generation", rec.getGeneration());
        d.put("state", rec.getState());
        d.put("holder", IdentityRows.view(rec, null).get("holder"));
        return new IdentityException(409, "generation_superseded",
            "the identity is at generation " + rec.getGeneration() + "; generation " + expected + " has been superseded", d);
    }

    /** The events of a process that already holds the identity: a stale generation on one of these is a superseded holder. */
    private static final Set<String> HOLDER_EVENTS = Set.of("clear", "fork", "refresh", "end");

    private static IdentityException held(IdentityRecordsRecord rec) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("state", rec.getState());
        d.put("generation", rec.getGeneration());
        d.put("holder", IdentityRows.view(rec, null).get("holder"));
        d.put("reserved_until", IdentityRows.str(IdentityRows.reservedUntil(rec)));
        return new IdentityException(409, "held", "the identity is held by another process", d);
    }

    private static IdentityException notHeld(IdentityRecordsRecord rec) {
        return new IdentityException(409, "not_held", "the identity is " + rec.getState() + ", no process holds it",
            Map.of("state", rec.getState(), "generation", rec.getGeneration()));
    }

    private static IdentityException mismatch(IdentityRecordsRecord rec) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("state", rec.getState());
        d.put("generation", rec.getGeneration());
        d.put("holder", IdentityRows.view(rec, null).get("holder"));
        return new IdentityException(409, "holder_mismatch",
            "the verdict names a process that is not the identity's current holder", d);
    }

    /**
     * A {@code clear}, {@code fork} or {@code refresh} that names a process (host, pid and {@code started_at}, or a
     * {@code proc_start}) must name the one that holds the identity: each of them ends with a fresh identity token,
     * and a caller that describes some other process is not the holder. A request that names none is unchanged (the
     * contract leaves the fields optional, so the credential of the tenant remains the trust boundary for it).
     */
    private static void requireNamedHolder(IdentityRecordsRecord rec, IdentityEvidence ev) {
        if (!ev.hasProcess() && ev.procStart() == null) {
            return;
        }
        IdentityEvidence.Process cur = IdentityRows.holder(rec);
        boolean ok = cur != null && (!ev.hasProcess() || sameProcess(cur, ev))
            && (ev.procStart() == null || cur.procStart() == null || ev.procStart().equals(cur.procStart()));
        if (!ok) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("state", rec.getState());
            d.put("generation", rec.getGeneration());
            d.put("holder", IdentityRows.view(rec, null).get("holder"));
            throw new IdentityException(409, "holder_mismatch",
                "the request names a process that is not the identity's current holder", d);
        }
    }

    private static boolean isHeld(IdentityRecordsRecord rec) {
        return "live".equals(rec.getState()) || "lapsed".equals(rec.getState());
    }

    private static boolean sameProcess(IdentityEvidence.Process cur, IdentityEvidence ev) {
        return cur != null && ev.hasProcess() && java.util.Objects.equals(cur.host(), ev.host())
            && java.util.Objects.equals(cur.pid(), ev.pid()) && java.util.Objects.equals(cur.startedAt(), ev.startedAt());
    }

    /** The evidence with the holder's process filled in where the caller sent none (a clear, an end). */
    private static IdentityEvidence withHolder(IdentityRecordsRecord rec, IdentityEvidence ev) {
        if (ev.hasProcess()) {
            return ev;
        }
        return new IdentityEvidence(ev.sessionId() != null ? ev.sessionId() : rec.getHolderSessionId(),
            rec.getHolderHost(), rec.getHolderPid(), rec.getHolderStartedAt(), rec.getHolderProcStart(),
            rec.getHolderPane(), ev.clientVersion() != null ? ev.clientVersion() : rec.getClientVersion(),
            rec.getLease() != null ? rec.getLease() : IdentityEvidence.LEASED, ev.priorSessionId(), ev.holderGone());
    }

    private static void revive(IdentityRecordsRecord rec) {
        rec.setState("live");
        rec.setLapsedAt(null);
        rec.setGoneAt(null);
    }

    /**
     * Binds the identity: the compare-and-set on {@code expectedGeneration}, then the policy of {@code event}.
     * Returns the identity as it stands, plus {@code event}, {@code generation_changed} and {@code noop}.
     */
    public Map<String, Object> bind(String tenant, String id, String event, Integer expectedGeneration,
                                    IdentityEvidence ev) {
        requireId(id);
        if (event == null || !BIND_EVENTS.contains(event)) {
            throw new IdentityException(400, "bad_event", "event must be one of " + new java.util.TreeSet<>(BIND_EVENTS));
        }
        if (expectedGeneration == null) {
            throw new IdentityException(400, "missing_generation", "expected_generation is required");
        }
        IdentityEvidence evidence = ev == null ? IdentityEvidence.none() : ev;
        return scope.withTenant(tenant, ctx -> {
            IdentityRecordsRecord rec = IdentityRows.lock(ctx, tenant, id);
            OffsetDateTime now = now();
            IdentityRows.lapseIfDue(ctx, rec, now);
            if (rec.getGeneration() != expectedGeneration.intValue()) {
                throw HOLDER_EVENTS.contains(event) && expectedGeneration < rec.getGeneration()
                    ? superseded(rec, expectedGeneration) : conflict(rec, expectedGeneration);
            }
            int before = rec.getGeneration();
            boolean noop = false;
            switch (event) {
                case "start", "resume", "takeover" -> acquire(ctx, rec, event, evidence, now);
                case "clear", "fork" -> keep(ctx, rec, event, evidence, now);
                case "refresh" -> {
                    if (isHeld(rec)) {
                        requireNamedHolder(rec, evidence);
                    }
                    renewLocked(ctx, rec, evidence.clientVersion(), now);
                }
                case "end" -> noop = end(ctx, rec, evidence, now);
                case "release" -> noop = release(ctx, rec, evidence, now);
                default -> throw new IllegalStateException(event);
            }
            rec.update();
            Map<String, Object> out = new LinkedHashMap<>(IdentityRows.view(rec, IdentityRows.liveAlias(ctx, tenant, id)));
            out.put("event", event);
            out.put("generation_changed", rec.getGeneration() != before);
            out.put("noop", noop);
            return out;
        });
    }

    private static void acquire(DSLContext ctx, IdentityRecordsRecord rec, String event, IdentityEvidence ev,
                                OffsetDateTime now) {
        requireProcess(ev, true);
        IdentityEvidence.Process cur = IdentityRows.holder(rec);
        boolean held = isHeld(rec);
        IdentityEvidence.Process verdict = ev.holderGone();
        if (verdict != null && held && cur != null && !verdict.sameAs(cur)) {
            throw mismatch(rec);
        }
        if (held && sameProcess(cur, ev)) {
            // The same process: a resume or a take-over of what it already holds. No new generation.
            if ("lapsed".equals(rec.getState())) {
                revive(rec);
            }
        } else {
            if (!"takeover".equals(event) && held) {
                boolean verdictFound = verdict != null || rec.getGoneAt() != null;
                java.time.OffsetDateTime reserved = IdentityRows.reservedUntil(rec);
                boolean otherHostFree = "lapsed".equals(rec.getState()) && cur != null
                    && !java.util.Objects.equals(cur.host(), ev.host()) && reserved != null && !now.isBefore(reserved);
                if (!verdictFound && !otherHostFree) {
                    throw held(rec);
                }
            }
            rec.setGeneration(rec.getGeneration() + 1);
            rec.setState("live");
            rec.setEndedAt(null);
            rec.setGoneAt(null);
            rec.setLapsedAt(null);
        }
        IdentityRows.setHolder(rec, ev);
        rec.setLastRenewal(now);
        IdentityRows.appendBinding(ctx, rec, event, now, ev, null);
        IdentityRows.indexSession(ctx, rec.getTenantId(), ev.sessionId(), rec.getId(), rec.getGeneration(), now);
    }

    private static void keep(DSLContext ctx, IdentityRecordsRecord rec, String event, IdentityEvidence ev,
                             OffsetDateTime now) {
        if (!isHeld(rec)) {
            throw notHeld(rec);
        }
        if (ev.sessionId() == null || ev.sessionId().isBlank()) {
            throw new IdentityException(400, "missing_evidence", "session_id is required for " + event);
        }
        requireNamedHolder(rec, ev);
        if ("lapsed".equals(rec.getState())) {
            revive(rec);
        }
        rec.setHolderSessionId(ev.sessionId());
        rec.setLastRenewal(now);
        if (ev.clientVersion() != null) {
            rec.setClientVersion(ev.clientVersion());
        }
        IdentityRows.appendBinding(ctx, rec, event, now, withHolder(rec, ev), null);
        IdentityRows.indexSession(ctx, rec.getTenantId(), ev.sessionId(), rec.getId(), rec.getGeneration(), now);
    }

    /** A renewal or a refresh: stamps the renewal, revives a lapsed identity (one {@code revive} row). */
    private static void renewLocked(DSLContext ctx, IdentityRecordsRecord rec, String clientVersion, OffsetDateTime now) {
        if (!isHeld(rec)) {
            throw notHeld(rec);
        }
        if ("lapsed".equals(rec.getState())) {
            revive(rec);
            rec.setLastRenewal(now);
            if (clientVersion != null) {
                rec.setClientVersion(clientVersion);
            }
            IdentityRows.appendBinding(ctx, rec, "revive", now, withHolder(rec, IdentityEvidence.none()), null);
            return;
        }
        rec.setLastRenewal(now);
        if (clientVersion != null) {
            rec.setClientVersion(clientVersion);
        }
    }

    /** A clean exit of the holder. Already ended: a no-op. Unheld: the caller no longer holds it. */
    private static boolean end(DSLContext ctx, IdentityRecordsRecord rec, IdentityEvidence ev, OffsetDateTime now) {
        if ("ended".equals(rec.getState())) {
            return true;
        }
        if (!isHeld(rec)) {
            throw notHeld(rec);
        }
        IdentityEvidence row = withHolder(rec, ev);
        rec.setState("ended");
        rec.setEndedAt(now);
        rec.setGoneAt(null);
        IdentityRows.appendBinding(ctx, rec, "end", now, row, null);
        IdentityRows.clearHolder(rec);
        return false;
    }

    /**
     * Frees a seat. Allowed to the holder itself (same process), on a verdict that the holder is gone, on a
     * recorded {@code gone}, or from another host once the 10-minute reservation of a lapsed holder has passed.
     */
    private static boolean release(DSLContext ctx, IdentityRecordsRecord rec, IdentityEvidence ev, OffsetDateTime now) {
        if (!"seat".equals(rec.getKind())) {
            throw new IdentityException(400, "not_a_seat", "release frees a seat; use end for an " + rec.getKind() + " identity");
        }
        if (!isHeld(rec)) {
            return true;
        }
        IdentityEvidence.Process cur = IdentityRows.holder(rec);
        IdentityEvidence.Process verdict = ev.holderGone();
        if (verdict != null && cur != null && !verdict.sameAs(cur)) {
            throw mismatch(rec);
        }
        java.time.OffsetDateTime reserved = IdentityRows.reservedUntil(rec);
        boolean self = sameProcess(cur, ev);
        boolean gone = verdict != null || rec.getGoneAt() != null;
        boolean otherHostFree = reserved != null && cur != null && ev.host() != null
            && !ev.host().equals(cur.host()) && !now.isBefore(reserved);
        if (!self && !gone && !otherHostFree) {
            throw held(rec);
        }
        IdentityEvidence row = withHolder(rec, ev);
        rec.setState("unheld");
        rec.setGoneAt(null);
        IdentityRows.appendBinding(ctx, rec, "release", now, row, null);
        IdentityRows.clearHolder(rec);
        return false;
    }

    /** A renewal from the process that holds generation {@code generation}; revives a lapsed identity. */
    public Map<String, Object> renew(String tenant, String id, Integer generation, String clientVersion) {
        requireId(id);
        if (generation == null) {
            throw new IdentityException(400, "missing_generation", "generation is required");
        }
        bounded(clientVersion, "client_version");
        return scope.withTenant(tenant, ctx -> {
            IdentityRecordsRecord rec = IdentityRows.lock(ctx, tenant, id);
            OffsetDateTime now = now();
            IdentityRows.lapseIfDue(ctx, rec, now);
            if (rec.getGeneration() != generation.intValue()) {
                throw generation < rec.getGeneration() ? superseded(rec, generation) : conflict(rec, generation);
            }
            renewLocked(ctx, rec, clientVersion, now);
            rec.update();
            return IdentityRows.view(rec, IdentityRows.liveAlias(ctx, tenant, id));
        });
    }

    /**
     * Records a client's verdict that the current holder is gone. Under the same compare-and-set, and only when
     * the verdict's host, pid, {@code started_at} and {@code proc_start} match the identity's current holder.
     */
    public Map<String, Object> gone(String tenant, String id, Integer expectedGeneration,
                                    IdentityEvidence.Process verdict) {
        requireId(id);
        if (expectedGeneration == null) {
            throw new IdentityException(400, "missing_generation", "expected_generation is required");
        }
        if (verdict == null || verdict.host() == null || verdict.pid() == null || verdict.startedAt() == null) {
            throw new IdentityException(400, "missing_evidence", "holder_gone needs host, pid and started_at");
        }
        return scope.withTenant(tenant, ctx -> {
            IdentityRecordsRecord rec = IdentityRows.lock(ctx, tenant, id);
            OffsetDateTime now = now();
            IdentityRows.lapseIfDue(ctx, rec, now);
            if (rec.getGeneration() != expectedGeneration.intValue()) {
                throw conflict(rec, expectedGeneration);
            }
            if (!isHeld(rec)) {
                throw notHeld(rec);
            }
            if (!verdict.sameAs(IdentityRows.holder(rec))) {
                throw mismatch(rec);
            }
            boolean recorded = rec.getGoneAt() == null;
            if (recorded) {
                if (rec.getLapsedAt() == null) {
                    rec.setLapsedAt(now);
                }
                rec.setState("lapsed");
                rec.setGoneAt(now);
                IdentityEvidence row = new IdentityEvidence(rec.getHolderSessionId(), verdict.host(), verdict.pid(),
                    verdict.startedAt(), verdict.procStart(), rec.getHolderPane(), rec.getClientVersion(),
                    rec.getLease(), null, verdict);
                IdentityRows.appendBinding(ctx, rec, "gone", now, row, null);
                rec.update();
            }
            Map<String, Object> out = new LinkedHashMap<>(IdentityRows.view(rec, IdentityRows.liveAlias(ctx, tenant, id)));
            out.put("recorded", recorded);
            return out;
        });
    }

    // ── rename and alias resolution ─────────────────────────────────────────

    /**
     * Renames a seat: retires the old alias (setting {@code renamed_to}), takes the new one, appends a
     * {@code rename} row and retitles the seat's board entry, in one transaction. {@code from} is the old alias or
     * the seat's id.
     */
    public Map<String, Object> rename(String tenant, String from, String to) {
        requireAliasName(to);
        if (from == null || from.isBlank()) {
            throw new IdentityException(400, "bad_name", "the old name is required");
        }
        return scope.withTenant(tenant, ctx -> {
            String id = ID.matcher(from).matches() ? from
                : ctx.select(IDENTITY_ALIASES.IDENTITY_ID).from(IDENTITY_ALIASES)
                    .where(IDENTITY_ALIASES.TENANT_ID.eq(tenant)).and(IDENTITY_ALIASES.NAME.eq(from))
                    .and(IDENTITY_ALIASES.STATE.eq("live")).fetchOne(IDENTITY_ALIASES.IDENTITY_ID);
            if (id == null) {
                throw new IdentityException(404, "unknown_name", "no live alias " + from);
            }
            IdentityRecordsRecord rec = IdentityRows.lock(ctx, tenant, id);
            if (!"seat".equals(rec.getKind())) {
                throw new IdentityException(409, "not_a_seat", id + " is not a seat");
            }
            String oldName = IdentityRows.liveAlias(ctx, tenant, id);
            if (oldName == null) {
                throw new IdentityException(409, "no_alias", id + " has no live alias");
            }
            if (!ID.matcher(from).matches() && !oldName.equals(from)) {
                // A second rename retired the name between the lookup and the lock: the caller named a name the seat no
                // longer holds, and renaming what it holds now is not what it asked for.
                throw new IdentityException(409, "stale_name", "'" + from + "' is no longer the seat's name; it is '" + oldName + "'",
                    Map.of("current_name", oldName));
            }
            if (oldName.equals(to)) {
                throw new IdentityException(409, "same_name", "the seat is already named " + to);
            }
            OffsetDateTime now = now();
            ctx.update(IDENTITY_ALIASES).set(IDENTITY_ALIASES.STATE, "retired")
                .set(IDENTITY_ALIASES.RETIRED_AT, now).set(IDENTITY_ALIASES.RENAMED_TO, to)
                .where(IDENTITY_ALIASES.TENANT_ID.eq(tenant)).and(IDENTITY_ALIASES.NAME.eq(oldName)).execute();
            insertAlias(ctx, tenant, id, to, now);
            IdentityRows.lapseIfDue(ctx, rec, now);
            IdentityRows.appendBinding(ctx, rec, "rename", now, withHolderOrNone(rec), oldName + " -> " + to);
            rec.update();
            int retitled = IdentityBoard.retitle(ctx, tenant, id, to);
            Map<String, Object> out = new LinkedHashMap<>(IdentityRows.view(rec, to));
            out.put("renamed_from", oldName);
            out.put("board_entries_retitled", retitled);
            return out;
        });
    }

    private static java.time.LocalDate utcDate(OffsetDateTime t) {
        return t.withOffsetSameInstant(java.time.ZoneOffset.UTC).toLocalDate();
    }

    private static IdentityEvidence withHolderOrNone(IdentityRecordsRecord rec) {
        return IdentityRows.holder(rec) == null ? IdentityEvidence.none() : IdentityRows.holderEvidence(rec);
    }

    /**
     * Resolves a name. A live alias answers its identity. A retired alias answers it for 7 days after the rename,
     * with a notice naming the current alias, then is refused (410) with where the seat went and when. A retired
     * name is never reissued.
     */
    public Map<String, Object> resolveAlias(String tenant, String name) {
        return scope.withTenant(tenant, ctx -> {
            var row = ctx.selectFrom(IDENTITY_ALIASES)
                .where(IDENTITY_ALIASES.TENANT_ID.eq(tenant)).and(IDENTITY_ALIASES.NAME.eq(name)).fetchOne();
            if (row == null) {
                throw new IdentityException(404, "unknown_name", "no seat has been named " + name);
            }
            OffsetDateTime now = now();
            String id = row.getIdentityId();
            String current = IdentityRows.liveAlias(ctx, tenant, id);
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("identity", id);
            out.put("name", name);
            out.put("state", row.getState());
            out.put("current_name", current);
            if ("retired".equals(row.getState())) {
                OffsetDateTime until = row.getRetiredAt().plusDays(ALIAS_GRACE_DAYS);
                String renamed = current != null ? current : row.getRenamedTo();
                if (!now.isBefore(until)) {
                    throw new IdentityException(410, "renamed", "renamed to " + renamed + " on " + utcDate(row.getRetiredAt()),
                        Map.of("renamed_to", renamed, "on", IdentityRows.str(row.getRetiredAt()), "identity", id));
                }
                out.put("renamed_to", renamed);
                out.put("retired_at", IdentityRows.str(row.getRetiredAt()));
                out.put("grace_until", IdentityRows.str(until));
                out.put("notice", "renamed to " + renamed + "; this name resolves until " + utcDate(until));
            }
            IdentityRecordsRecord rec = refreshed(ctx, tenant, id, now);
            out.put("generation", rec == null ? null : rec.getGeneration());
            out.put("identity_state", rec == null ? null : rec.getState());
            return out;
        });
    }

    // ── reads ───────────────────────────────────────────────────────────────

    /** The identity with a due lapse applied (a read that writes the lapse row), or null. */
    private static IdentityRecordsRecord refreshed(DSLContext ctx, String tenant, String id, OffsetDateTime now) {
        IdentityRecordsRecord rec = IdentityRows.peek(ctx, tenant, id);
        if (rec != null && IdentityRows.lapseDue(rec, now)) {
            rec = IdentityRows.lock(ctx, tenant, id);
            IdentityRows.lapseIfDue(ctx, rec, now);
        }
        return rec;
    }

    /** One identity, with state and holder as of now (a due lapse is applied first). */
    public Map<String, Object> show(String tenant, String id) {
        requireId(id);
        return scope.withTenant(tenant, ctx -> {
            IdentityRecordsRecord rec = refreshed(ctx, tenant, id, now());
            if (rec == null) {
                throw new IdentityException(404, "unknown_identity", "no identity " + id);
            }
            return IdentityRows.view(rec, IdentityRows.liveAlias(ctx, tenant, id));
        });
    }

    /** The session index: a session id to its identity and generation. */
    public Map<String, Object> lookupSession(String tenant, String sessionId) {
        return scope.withTenant(tenant, ctx -> {
            var row = ctx.selectFrom(IDENTITY_SESSIONS)
                .where(IDENTITY_SESSIONS.TENANT_ID.eq(tenant)).and(IDENTITY_SESSIONS.SESSION_ID.eq(sessionId)).fetchOne();
            if (row == null) {
                throw new IdentityException(404, "unknown_session", "no identity has held session " + sessionId);
            }
            IdentityRecordsRecord rec = refreshed(ctx, tenant, row.getIdentityId(), now());
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("session_id", sessionId);
            out.put("identity", row.getIdentityId());
            out.put("generation", row.getGeneration());
            out.put("current_generation", rec == null ? null : rec.getGeneration());
            out.put("kind", rec == null ? null : rec.getKind());
            out.put("state", rec == null ? null : rec.getState());
            out.put("liveness", rec == null ? null : IdentityRows.liveness(rec));
            return out;
        });
    }

    /** A session's identity and generation, for another engine component (RDR-252's stamper). */
    public record SessionIdentity(String identity, int generation) { }

    /**
     * Plain lookup of the session index: the identity and generation a session id maps to, or empty. It throws
     * nothing for an unknown session and applies no lapse, so the engine's own components can call it freely.
     */
    public java.util.Optional<SessionIdentity> sessionIdentity(String tenant, String sessionId) {
        return scope.withTenant(tenant, ctx -> java.util.Optional.ofNullable(ctx.selectFrom(IDENTITY_SESSIONS)
            .where(IDENTITY_SESSIONS.TENANT_ID.eq(tenant)).and(IDENTITY_SESSIONS.SESSION_ID.eq(sessionId))
            .fetchOne(r -> new SessionIdentity(r.getIdentityId(), r.getGeneration()))));
    }

    /** The live and lapsed holders on {@code host} (optionally one pid), unleased ones included. */
    public List<Map<String, Object>> bindingsByHost(String tenant, String host, Integer pid) {
        if (host == null || host.isBlank()) {
            throw new IdentityException(400, "missing_host", "host is required");
        }
        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = now();
            var q = ctx.select(IDENTITY_RECORDS.ID).from(IDENTITY_RECORDS)
                .where(IDENTITY_RECORDS.TENANT_ID.eq(tenant)).and(IDENTITY_RECORDS.HOLDER_HOST.eq(host))
                .and(IDENTITY_RECORDS.STATE.in("live", "lapsed"));
            List<String> ids = (pid == null ? q : q.and(IDENTITY_RECORDS.HOLDER_PID.eq(pid)))
                .orderBy(IDENTITY_RECORDS.ID).fetch(IDENTITY_RECORDS.ID);
            List<Map<String, Object>> out = new ArrayList<>();
            for (String id : ids) {
                IdentityRecordsRecord rec = refreshed(ctx, tenant, id, now);
                if (rec != null && isHeld(rec)) {
                    out.add(IdentityRows.view(rec, IdentityRows.liveAlias(ctx, tenant, id)));
                }
            }
            return out;
        });
    }

    /** The binding log of one identity, oldest first. */
    public List<Map<String, Object>> bindingLog(String tenant, String id) {
        requireId(id);
        return scope.withTenant(tenant, ctx -> {
            if (IdentityRows.peek(ctx, tenant, id) == null) {
                throw new IdentityException(404, "unknown_identity", "no identity " + id);
            }
            return ctx.selectFrom(IDENTITY_BINDINGS)
                .where(IDENTITY_BINDINGS.TENANT_ID.eq(tenant)).and(IDENTITY_BINDINGS.IDENTITY_ID.eq(id))
                .orderBy(IDENTITY_BINDINGS.SEQ).fetch(IdentityRepository::bindingMap);
        });
    }

    private static Map<String, Object> bindingMap(dev.nexus.service.jooq.nexus.tables.records.IdentityBindingsRecord r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("seq", r.getSeq());
        m.put("event", r.getEvent());
        m.put("generation", r.getGeneration());
        m.put("lease", r.getLease());
        m.put("session_id", r.getSessionId());
        m.put("host", r.getHost());
        m.put("pid", r.getPid());
        m.put("started_at", r.getStartedAt());
        m.put("proc_start", r.getProcStart());
        m.put("pane", r.getPane());
        m.put("client_version", r.getClientVersion());
        m.put("prior_session_id", r.getPriorSessionId());
        m.put("holder_gone", IdentityRows.parse(r.getHolderGone()));
        m.put("detail", r.getDetail());
        m.put("at", IdentityRows.str(r.getAt()));
        return m;
    }

    /** The most identities a history lookup returns. */
    static final int HISTORY_LIMIT = 20;

    /**
     * The history mode of bindings-by-host: for every identity whose binding log has a row on {@code host} with
     * {@code pid} and {@code procStart}, its NEWEST such row, joined to the identity's current record,
     * most recent first. Unlike {@link #bindingsByHost} it finds an identity that {@code ended} or was released,
     * whose holder columns are cleared: the in-place {@code /restart} rebind (RDR-253 policy row 1) looks for the most
     * recent binding with the process's own pid and {@code proc_start}, "whatever the identity's state, ended
     * included".
     */
    public List<Map<String, Object>> bindingHistoryByProcess(String tenant, String host, Integer pid, String procStart) {
        if (host == null || host.isBlank() || pid == null || procStart == null || procStart.isBlank()) {
            throw new IdentityException(400, "missing_process",
                "host, pid and proc_start are required for the history of a process: a pid alone can name a reused one");
        }
        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = now();
            var b = IDENTITY_BINDINGS;
            var cond = b.TENANT_ID.eq(tenant).and(b.HOST.eq(host)).and(b.PID.eq(pid)).and(b.PROC_START.eq(procStart));
            var newest = DSL.max(b.AT);
            var seqs = DSL.max(b.SEQ);
            List<org.jooq.Record2<String, Integer>> found = ctx.select(b.IDENTITY_ID, seqs).from(b).where(cond)
                .groupBy(b.IDENTITY_ID).orderBy(newest.desc(), b.IDENTITY_ID).limit(HISTORY_LIMIT).fetch();
            List<Map<String, Object>> out = new ArrayList<>();
            for (var f : found) {
                String id = f.value1();
                var row = ctx.selectFrom(b).where(b.TENANT_ID.eq(tenant)).and(b.IDENTITY_ID.eq(id))
                    .and(b.SEQ.eq(f.value2())).fetchOne();
                IdentityRecordsRecord rec = refreshed(ctx, tenant, id, now);
                if (row == null || rec == null) {
                    continue;
                }
                Map<String, Object> m = new LinkedHashMap<>(IdentityRows.view(rec, IdentityRows.liveAlias(ctx, tenant, id)));
                m.put("binding", bindingMap(row));
                out.add(m);
            }
            return out;
        });
    }

    // ── memberships ─────────────────────────────────────────────────────────

    /** Joins {@code group} with {@code role}, or changes the role of a membership that exists. */
    public Map<String, Object> joinGroup(String tenant, String id, String group, String role) {
        return memberships.join(tenant, id, group, role);
    }

    /** Leaves {@code group}; idempotent. */
    public Map<String, Object> leaveGroup(String tenant, String id, String group) {
        return memberships.leave(tenant, id, group);
    }

    /** The groups of one identity, in join order. */
    public List<Map<String, Object>> groupsOf(String tenant, String id) {
        return memberships.ofIdentity(tenant, id);
    }

    /** The roster of a group, in join order. */
    public List<Map<String, Object>> rosterOf(String tenant, String group) {
        return memberships.ofGroup(tenant, group);
    }

    // ── background work ─────────────────────────────────────────────────────

    /** Registers work an identity owns, or changes its state in place (a label, not fenced). */
    public Map<String, Object> putWork(String tenant, String id, String kind, String ref, String state) {
        return work.put(tenant, id, kind, ref, state);
    }

    /** Forgets work; idempotent. */
    public Map<String, Object> removeWork(String tenant, String id, String kind, String ref) {
        return work.remove(tenant, id, kind, ref);
    }

    /** Every identity that owns the resource {@code kind}/{@code ref} (several may): the reverse of {@link #workOf}. */
    public List<Map<String, Object>> ownersOfWork(String tenant, String kind, String ref) {
        return work.owners(tenant, kind, ref);
    }

    /** The work of one identity. */
    public List<Map<String, Object>> workOf(String tenant, String id) {
        return work.ofIdentity(tenant, id);
    }

    // ── purge and retention ─────────────────────────────────────────────────

    /** The operator purge of {@code id}'s fenced state; see {@link IdentityRetention#purge}. */
    public Map<String, Object> purge(String tenant, String id, String actor) {
        requireId(id);
        return retention.purge(tenant, id, actor);
    }

    /** One retention pass over a tenant; see {@link IdentityRetention#run}. */
    public Map<String, Integer> runRetention(String tenant) {
        return retention.run(tenant, null);
    }

    /** One retention pass over a tenant with a statement timeout on every statement, as the sweep's other arms have. */
    public Map<String, Integer> runRetention(String tenant, java.time.Duration statementTimeout) {
        return retention.run(tenant, statementTimeout);
    }
}
