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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_BINDINGS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_MEMBERSHIPS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_WORK;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_SESSIONS;

/**
 * Retention and the operator purge of identity state (RDR-253 "Retention", "Fencing").
 *
 * <p>Seats, their aliases and their identity records are permanent. An ephemeral or delegated identity is
 * tombstoned 30 days after it ended, or 30 days after the engine recorded its holder gone while it was lapsed
 * (an {@code unleased} ephemeral the same way); a delegated one has no registry file for a holder check to find,
 * so for it 30 days lapsed is enough. A tombstone deletes the identity row, which cascades to its binding and
 * session-index rows, deletes its board entry, its memberships, its work rows and its mailbox, and writes one
 * {@code gc_audit} row. The counter keeps the numbers: an id is never issued twice. Binding rows older than 180
 * days go, never an identity's newest, and session-index rows whose binding rows went go with them.
 *
 * <p>The purge is the same deletion on request, for a seat as well, but a seat keeps its record, aliases and
 * binding rows (permanent history). It answers an identity that {@code ended} or is {@code lapsed} with its holder
 * recorded {@code gone}; it is refused for a {@code live} identity, an {@code unheld} one, and a lapsed one whose
 * holder has no recorded {@code gone}.
 *
 * <p>The audit sink is {@code nexus.gc_audit}, the engine's one audit table ({@code operation}
 * {@code identity_purge} or {@code identity_tombstone}); a log line alone is not an audit record. Identity
 * scratch (the T1 identity scope) does not exist until RDR-253 Step 6, so there is none to remove yet.
 */
final class IdentityRetention {

    private static final Logger log = LoggerFactory.getLogger(IdentityRetention.class);

    static final int TOMBSTONE_DAYS = 30;
    static final int BINDING_RETENTION_DAYS = 180;
    /** Identities tombstoned per pass and tenant; the next pass takes the rest. */
    static final int PASS_LIMIT = 200;

    /** Rows one prune statement deletes, and statements a pass may run for each of the two prunes. */
    static final int PRUNE_BATCH = 5000;
    static final int PRUNE_MAX_BATCHES = 20;

    private final TenantScope scope;
    private final Clock clock;
    private final int pruneBatch;
    private final int pruneMaxBatches;

    IdentityRetention(TenantScope scope, Clock clock, int pruneBatch, int pruneMaxBatches) {
        this.scope = scope;
        this.clock = clock;
        this.pruneBatch = pruneBatch;
        this.pruneMaxBatches = pruneMaxBatches;
    }

    /** What an identity owns, removed inside the caller's transaction; returns the counts removed. */
    private static Map<String, Object> removeOwned(DSLContext ctx, String tenant, String id, boolean deleteRecord) {
        Map<String, Object> removed = new LinkedHashMap<>();
        removed.put("board_entries", IdentityBoard.deleteEntries(ctx, tenant, id));
        removed.putAll(IdentityBoard.deleteMailbox(ctx, tenant, id));
        removed.put("work", ctx.deleteFrom(IDENTITY_WORK).where(IDENTITY_WORK.TENANT_ID.eq(tenant))
            .and(IDENTITY_WORK.IDENTITY_ID.eq(id)).execute());
        removed.put("member", ctx.deleteFrom(IDENTITY_MEMBERSHIPS).where(IDENTITY_MEMBERSHIPS.TENANT_ID.eq(tenant))
            .and(IDENTITY_MEMBERSHIPS.IDENTITY_ID.eq(id)).execute());
        if (deleteRecord) {
            int sessions = ctx.fetchCount(IDENTITY_SESSIONS,
                IDENTITY_SESSIONS.TENANT_ID.eq(tenant).and(IDENTITY_SESSIONS.IDENTITY_ID.eq(id)));
            int bindings = ctx.fetchCount(IDENTITY_BINDINGS,
                IDENTITY_BINDINGS.TENANT_ID.eq(tenant).and(IDENTITY_BINDINGS.IDENTITY_ID.eq(id)));
            ctx.deleteFrom(IDENTITY_RECORDS)
                .where(IDENTITY_RECORDS.TENANT_ID.eq(tenant)).and(IDENTITY_RECORDS.ID.eq(id)).execute();
            removed.put("sessions", sessions);
            removed.put("bindings", bindings);
            removed.put("identity_record", 1);
        }
        return removed;
    }

    private static void audit(DSLContext ctx, String tenant, String operation, String actor, IdentityRecordsRecord rec,
                              Map<String, Object> removed) {
        Map<String, Object> details = new LinkedHashMap<>();
        details.put("identity", rec.getId());
        details.put("kind", rec.getKind());
        details.put("state", rec.getState());
        details.put("removed", removed);
        CatalogRepository.insertGcAuditRow(ctx, tenant, operation, "", actor, false, List.of(), details);
    }

    /**
     * The operator purge. {@code actor} names the caller for the audit row. Throws 409 {@code identity_live} for a
     * live identity and {@code holder_not_gone} for a lapsed one with no recorded verdict.
     */
    Map<String, Object> purge(String tenant, String id, String actor) {
        return scope.withTenant(tenant, ctx -> {
            IdentityRecordsRecord rec = IdentityRows.lock(ctx, tenant, id);
            OffsetDateTime now = OffsetDateTime.now(clock);
            IdentityRows.lapseIfDue(ctx, rec, now);
            if ("live".equals(rec.getState())) {
                throw new IdentityException(409, "identity_live", id + " is live: its holder is still renewing",
                    Map.of("state", "live", "generation", rec.getGeneration()));
            }
            if ("unheld".equals(rec.getState())) {
                throw new IdentityException(409, "identity_unheld",
                    id + " is unheld: it was never bound or was released, and purge removes the state of an identity that ended"
                    + " or lapsed with its holder gone", Map.of("state", "unheld", "generation", rec.getGeneration()));
            }
            if ("lapsed".equals(rec.getState()) && rec.getGoneAt() == null) {
                throw new IdentityException(409, "holder_not_gone",
                    id + " is lapsed and no client has recorded its holder gone",
                    Map.of("state", "lapsed", "generation", rec.getGeneration()));
            }
            boolean tombstone = !"seat".equals(rec.getKind());
            Map<String, Object> removed = removeOwned(ctx, tenant, id, tombstone);
            audit(ctx, tenant, "identity_purge", actor, rec, removed);
            log.info("event=identity_purged tenant={} id={} kind={} actor={} removed={}", tenant, id, rec.getKind(),
                actor, removed);
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("identity", id);
            out.put("kind", rec.getKind());
            out.put("tombstoned", tombstone);
            out.put("removed", removed);
            return out;
        });
    }

    /** One retention pass over one tenant. Returns {@code tombstoned}, {@code bindings_pruned}, {@code sessions_pruned}. */
    Map<String, Integer> run(String tenant, java.time.Duration statementTimeout) {
        OffsetDateTime now = OffsetDateTime.now(clock);
        OffsetDateTime cutoff = now.minusDays(TOMBSTONE_DAYS);
        applyDueLapses(tenant, now, statementTimeout);
        List<String> due = scope.withTenant(tenant, ctx -> {
            SweepBounds.applyStatementTimeout(ctx, statementTimeout);
            return ctx.select(IDENTITY_RECORDS.ID).from(IDENTITY_RECORDS)
                .where(IDENTITY_RECORDS.TENANT_ID.eq(tenant)).and(eligible(cutoff))
                .orderBy(IDENTITY_RECORDS.ID).limit(PASS_LIMIT).fetch(IDENTITY_RECORDS.ID);
        });
        int tombstoned = 0;
        for (String id : due) {
            tombstoned += scope.withTenant(tenant, ctx -> {
                SweepBounds.applyStatementTimeout(ctx, statementTimeout);
                // Re-read under the lock: a renewal or a rebind since the listing keeps the identity.
                IdentityRecordsRecord rec = ctx.selectFrom(IDENTITY_RECORDS)
                    .where(IDENTITY_RECORDS.TENANT_ID.eq(tenant)).and(IDENTITY_RECORDS.ID.eq(id))
                    .and(eligible(cutoff)).forUpdate().fetchOne();
                if (rec == null) {
                    return 0;
                }
                Map<String, Object> removed = removeOwned(ctx, tenant, id, true);
                audit(ctx, tenant, "identity_tombstone", "engine", rec, removed);
                return 1;
            });
        }
        int[] pruned = {pruneBindings(tenant, now.minusDays(BINDING_RETENTION_DAYS), statementTimeout),
            pruneSessions(tenant, statementTimeout)};
        Map<String, Integer> out = new LinkedHashMap<>();
        out.put("tombstoned", tombstoned);
        out.put("bindings_pruned", pruned[0]);
        out.put("sessions_pruned", pruned[1]);
        if (tombstoned > 0 || pruned[0] > 0 || pruned[1] > 0) {
            log.info("event=identity_retention tenant={} tombstoned={} bindings_pruned={} sessions_pruned={}", tenant,
                tombstoned, pruned[0], pruned[1]);
        }
        return out;
    }

    /**
     * Deletes binding rows older than the cutoff, never an identity's newest, {@code pruneBatch} rows a statement in
     * their own transaction, at most {@code pruneMaxBatches} statements a pass, so a large backlog progresses over
     * passes and no single statement outruns the sweep's statement timeout. Returns the rows deleted.
     */
    private int pruneBindings(String tenant, OffsetDateTime cutoff, java.time.Duration statementTimeout) {
        int total = 0;
        for (int batch = 0; batch < pruneMaxBatches; batch++) {
            int n = scope.withTenant(tenant, ctx -> {
                SweepBounds.applyStatementTimeout(ctx, statementTimeout);
                var b = IDENTITY_BINDINGS.as("b");
                var newest = IDENTITY_BINDINGS.as("newest");
                var victims = ctx.select(b.TENANT_ID, b.IDENTITY_ID, b.SEQ).from(b)
                    .where(b.TENANT_ID.eq(tenant)).and(b.AT.lt(cutoff))
                    .and(b.SEQ.lt(DSL.field(DSL.select(DSL.max(newest.SEQ)).from(newest)
                        .where(newest.TENANT_ID.eq(b.TENANT_ID)).and(newest.IDENTITY_ID.eq(b.IDENTITY_ID)))))
                    .orderBy(b.AT, b.IDENTITY_ID, b.SEQ).limit(pruneBatch);
                return ctx.deleteFrom(IDENTITY_BINDINGS)
                    .where(DSL.row(IDENTITY_BINDINGS.TENANT_ID, IDENTITY_BINDINGS.IDENTITY_ID, IDENTITY_BINDINGS.SEQ).in(victims))
                    .execute();
            });
            total += n;
            if (n < pruneBatch) {
                break;
            }
        }
        return total;
    }

    /** Deletes session-index rows that no binding row of their identity names any more, batched like the bindings. */
    private int pruneSessions(String tenant, java.time.Duration statementTimeout) {
        int total = 0;
        for (int batch = 0; batch < pruneMaxBatches; batch++) {
            int n = scope.withTenant(tenant, ctx -> {
                SweepBounds.applyStatementTimeout(ctx, statementTimeout);
                var s = IDENTITY_SESSIONS.as("s");
                var victims = ctx.select(s.TENANT_ID, s.SESSION_ID).from(s).where(s.TENANT_ID.eq(tenant))
                    .andNotExists(DSL.selectOne().from(IDENTITY_BINDINGS)
                        .where(IDENTITY_BINDINGS.TENANT_ID.eq(s.TENANT_ID))
                        .and(IDENTITY_BINDINGS.IDENTITY_ID.eq(s.IDENTITY_ID))
                        .and(IDENTITY_BINDINGS.SESSION_ID.eq(s.SESSION_ID)))
                    .orderBy(s.AT, s.SESSION_ID).limit(pruneBatch);
                return ctx.deleteFrom(IDENTITY_SESSIONS)
                    .where(DSL.row(IDENTITY_SESSIONS.TENANT_ID, IDENTITY_SESSIONS.SESSION_ID).in(victims)).execute();
            });
            total += n;
            if (n < pruneBatch) {
                break;
            }
        }
        return total;
    }

    /**
     * Lapse is written lazily, when the engine reads a binding, so an identity that died and was never read has no
     * {@code lapsed} state for the 30-day rule to see. A delegated identity whose dispatcher died is exactly that
     * (nothing reads it again), so the pass applies the lapses that are due on non-seat identities first.
     */
    private void applyDueLapses(String tenant, OffsetDateTime now, java.time.Duration statementTimeout) {
        List<String> ids = scope.withTenant(tenant, ctx -> {
            SweepBounds.applyStatementTimeout(ctx, statementTimeout);
            return ctx.select(IDENTITY_RECORDS.ID).from(IDENTITY_RECORDS)
                .where(IDENTITY_RECORDS.TENANT_ID.eq(tenant)).and(IDENTITY_RECORDS.KIND.ne("seat"))
                .and(IDENTITY_RECORDS.STATE.eq("live")).and(IDENTITY_RECORDS.LEASE.eq(IdentityEvidence.LEASED))
                .and(IDENTITY_RECORDS.LAST_RENEWAL.lt(now.minusSeconds(IdentityRows.LAPSE_SECONDS)))
                .orderBy(IDENTITY_RECORDS.ID).limit(PASS_LIMIT).fetch(IDENTITY_RECORDS.ID);
        });
        for (String id : ids) {
            scope.withTenant(tenant, ctx -> {
                SweepBounds.applyStatementTimeout(ctx, statementTimeout);
                IdentityRows.lapseIfDue(ctx, IdentityRows.lock(ctx, tenant, id), now);
                return null;
            });
        }
    }

    /** Ephemeral or delegated, and ended, or lapsed with its holder gone, 30 days ago or more. */
    private static org.jooq.Condition eligible(OffsetDateTime cutoff) {
        return IDENTITY_RECORDS.KIND.ne("seat").and(
            IDENTITY_RECORDS.STATE.eq("ended").and(IDENTITY_RECORDS.ENDED_AT.le(cutoff))
                .or(IDENTITY_RECORDS.STATE.eq("lapsed").and(
                    IDENTITY_RECORDS.GONE_AT.le(cutoff)
                        .or(IDENTITY_RECORDS.KIND.eq("delegated").and(IDENTITY_RECORDS.LAPSED_AT.le(cutoff))))));
    }
}
