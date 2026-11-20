// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.time.Clock;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_WORK;

/**
 * The background work and resources an identity owns (RDR-253 "Background work"): watchers, waits, lab runs,
 * worktrees and tmux servers, one permanent row per (identity, kind, ref) in {@code nexus.identity_work}, so a
 * checkpoint can list them by identity after a {@code /clear}. The state ({@code running} or {@code done}) is
 * updated in place. A tuple could not hold it: a keyed-and-nonced template cannot change a row's state, and every
 * tuple expires, while this lasts as long as the identity does and goes with it when retention tombstones it.
 *
 * <p>A label, not a fenced family: a lab run on another host has no token file, so the write needs no ownership
 * check in Step 2.
 */
public final class IdentityWork {

    static final Set<String> KINDS = Set.of("watcher", "wait", "labrun", "worktree", "tmux", "agent");
    static final Set<String> STATES = Set.of("running", "done");
    /** The most work rows one identity may hold; the next registration is refused with 409 {@code work_limit}. */
    static final int MAX_PER_IDENTITY = 200;

    private final TenantScope scope;
    private final Clock clock;

    IdentityWork(TenantScope scope, Clock clock) {
        this.scope = scope;
        this.clock = clock;
    }

    private static Map<String, Object> row(String id, String kind, String ref, String state, OffsetDateTime at) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("identity", id);
        m.put("kind", kind);
        m.put("ref", ref);
        m.put("state", state);
        m.put("updated_at", IdentityRows.str(at));
        return m;
    }

    private static void requireKindAndRef(String kind, String ref) {
        if (kind == null || !KINDS.contains(kind)) {
            throw new IdentityException(400, "bad_kind", "kind must be one of " + new java.util.TreeSet<>(KINDS));
        }
        if (ref == null || ref.isBlank()) {
            throw new IdentityException(400, "bad_ref", "ref is required");
        }
        IdentityRepository.bounded(ref, "ref");
    }

    /** Registers the work, or changes its state in place. */
    Map<String, Object> put(String tenant, String id, String kind, String ref, String state) {
        IdentityRepository.requireId(id);
        requireKindAndRef(kind, ref);
        if (state == null || !STATES.contains(state)) {
            throw new IdentityException(400, "bad_state", "state must be running or done");
        }
        return scope.withTenant(tenant, ctx -> {
            IdentityRows.lock(ctx, tenant, id);   // the lock also serializes the count below against another writer
            OffsetDateTime now = OffsetDateTime.now(clock);
            boolean exists = ctx.fetchExists(IDENTITY_WORK, IDENTITY_WORK.TENANT_ID.eq(tenant)
                .and(IDENTITY_WORK.IDENTITY_ID.eq(id)).and(IDENTITY_WORK.KIND.eq(kind)).and(IDENTITY_WORK.REF.eq(ref)));
            if (!exists && ctx.fetchCount(IDENTITY_WORK, IDENTITY_WORK.TENANT_ID.eq(tenant)
                    .and(IDENTITY_WORK.IDENTITY_ID.eq(id))) >= MAX_PER_IDENTITY) {
                throw new IdentityException(409, "work_limit", id + " already holds " + MAX_PER_IDENTITY
                    + " work rows; forget finished work first", Map.of("limit", MAX_PER_IDENTITY));
            }
            ctx.insertInto(IDENTITY_WORK)
                .set(IDENTITY_WORK.TENANT_ID, tenant).set(IDENTITY_WORK.IDENTITY_ID, id).set(IDENTITY_WORK.KIND, kind)
                .set(IDENTITY_WORK.REF, ref).set(IDENTITY_WORK.STATE, state).set(IDENTITY_WORK.UPDATED_AT, now)
                .onConflict(IDENTITY_WORK.TENANT_ID, IDENTITY_WORK.IDENTITY_ID, IDENTITY_WORK.KIND, IDENTITY_WORK.REF)
                .doUpdate().set(IDENTITY_WORK.STATE, state).set(IDENTITY_WORK.UPDATED_AT, now)
                .execute();
            return row(id, kind, ref, state, now);
        });
    }

    /** Forgets the work; removing what is not there removes nothing and says so. */
    Map<String, Object> remove(String tenant, String id, String kind, String ref) {
        IdentityRepository.requireId(id);
        requireKindAndRef(kind, ref);
        return scope.withTenant(tenant, ctx -> {
            int n = ctx.deleteFrom(IDENTITY_WORK).where(IDENTITY_WORK.TENANT_ID.eq(tenant))
                .and(IDENTITY_WORK.IDENTITY_ID.eq(id)).and(IDENTITY_WORK.KIND.eq(kind)).and(IDENTITY_WORK.REF.eq(ref)).execute();
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("identity", id);
            m.put("kind", kind);
            m.put("ref", ref);
            m.put("removed", n);
            return m;
        });
    }

    /** The identities that own one resource, by identity id. */
    List<Map<String, Object>> owners(String tenant, String kind, String ref) {
        requireKindAndRef(kind, ref);
        return scope.withTenant(tenant, ctx -> ctx.selectFrom(IDENTITY_WORK).where(IDENTITY_WORK.TENANT_ID.eq(tenant))
            .and(IDENTITY_WORK.KIND.eq(kind)).and(IDENTITY_WORK.REF.eq(ref)).orderBy(IDENTITY_WORK.IDENTITY_ID)
            .fetch(r -> row(r.getIdentityId(), r.getKind(), r.getRef(), r.getState(), r.getUpdatedAt())));
    }

    /** The work of one identity, oldest registration first by kind and ref. */
    List<Map<String, Object>> ofIdentity(String tenant, String id) {
        IdentityRepository.requireId(id);
        return scope.withTenant(tenant, ctx -> {
            if (IdentityRows.peek(ctx, tenant, id) == null) {
                throw new IdentityException(404, "unknown_identity", "no identity " + id);
            }
            return ctx.selectFrom(IDENTITY_WORK).where(IDENTITY_WORK.TENANT_ID.eq(tenant))
                .and(IDENTITY_WORK.IDENTITY_ID.eq(id)).orderBy(IDENTITY_WORK.KIND, IDENTITY_WORK.REF)
                .fetch(r -> row(id, r.getKind(), r.getRef(), r.getState(), r.getUpdatedAt()));
        });
    }
}
