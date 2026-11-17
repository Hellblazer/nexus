// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.time.Clock;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_MEMBERSHIPS;

/**
 * The group memberships of an identity (RDR-253 "State owned by the identity"): permanent rows in
 * {@code nexus.identity_memberships}, one per (identity, group), with the role and the time the identity joined.
 * A membership lasts as long as the identity does, so RDR-252 can key its groups on it; a tuple template cannot
 * give that, because a re-out never extends a row past its first expiry. A row goes when the identity leaves the
 * group, when retention tombstones a non-seat identity (the foreign key cascades), or on an operator purge.
 *
 * <p>{@code created_at} is when the identity first joined, kept when the role later changes, so a group's roster
 * reads in join order. The fence that only the owner may write them is RDR-253 Step 2's, and it lives in the
 * identity handler, not in {@code TupleHandler}, because there is no tuple.
 */
public final class IdentityMemberships {

    private static final Pattern GROUP = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$");

    private final TenantScope scope;
    private final Clock clock;

    IdentityMemberships(TenantScope scope, Clock clock) {
        this.scope = scope;
        this.clock = clock;
    }

    private static void requireGroup(String group) {
        if (group == null || !GROUP.matcher(group).matches()) {
            throw new IdentityException(400, "bad_group",
                "a group name starts with a letter or digit and holds letters, digits, '.', '_' and '-' (at most 64)");
        }
    }

    private static Map<String, Object> row(String id, String group, String role, OffsetDateTime joined) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("identity", id);
        m.put("group", group);
        m.put("role", role);
        m.put("joined_at", IdentityRows.str(joined));
        return m;
    }

    /** Joins the group, or changes the role of a membership that exists (the join time stays). */
    Map<String, Object> join(String tenant, String id, String group, String role) {
        IdentityRepository.requireId(id);
        requireGroup(group);
        if (!"leader".equals(role) && !"member".equals(role)) {
            throw new IdentityException(400, "bad_role", "role must be leader or member");
        }
        return scope.withTenant(tenant, ctx -> {
            IdentityRows.lock(ctx, tenant, id);
            OffsetDateTime now = OffsetDateTime.now(clock);
            var rec = ctx.insertInto(IDENTITY_MEMBERSHIPS)
                .set(IDENTITY_MEMBERSHIPS.TENANT_ID, tenant).set(IDENTITY_MEMBERSHIPS.IDENTITY_ID, id)
                .set(IDENTITY_MEMBERSHIPS.GROUP_NAME, group).set(IDENTITY_MEMBERSHIPS.ROLE, role)
                .set(IDENTITY_MEMBERSHIPS.CREATED_AT, now)
                .onConflict(IDENTITY_MEMBERSHIPS.TENANT_ID, IDENTITY_MEMBERSHIPS.IDENTITY_ID, IDENTITY_MEMBERSHIPS.GROUP_NAME)
                .doUpdate().set(IDENTITY_MEMBERSHIPS.ROLE, role)
                .returning(IDENTITY_MEMBERSHIPS.CREATED_AT).fetchOne();
            return row(id, group, role, rec.getCreatedAt());
        });
    }

    /** Leaves the group; leaving one never joined removes nothing and says so. */
    Map<String, Object> leave(String tenant, String id, String group) {
        IdentityRepository.requireId(id);
        requireGroup(group);
        return scope.withTenant(tenant, ctx -> {
            int n = ctx.deleteFrom(IDENTITY_MEMBERSHIPS).where(IDENTITY_MEMBERSHIPS.TENANT_ID.eq(tenant))
                .and(IDENTITY_MEMBERSHIPS.IDENTITY_ID.eq(id)).and(IDENTITY_MEMBERSHIPS.GROUP_NAME.eq(group)).execute();
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("identity", id);
            m.put("group", group);
            m.put("removed", n);
            return m;
        });
    }

    /** The groups an identity belongs to, in join order. */
    List<Map<String, Object>> ofIdentity(String tenant, String id) {
        IdentityRepository.requireId(id);
        return scope.withTenant(tenant, ctx -> {
            if (IdentityRows.peek(ctx, tenant, id) == null) {
                throw new IdentityException(404, "unknown_identity", "no identity " + id);
            }
            return ctx.selectFrom(IDENTITY_MEMBERSHIPS).where(IDENTITY_MEMBERSHIPS.TENANT_ID.eq(tenant))
                .and(IDENTITY_MEMBERSHIPS.IDENTITY_ID.eq(id))
                .orderBy(IDENTITY_MEMBERSHIPS.CREATED_AT, IDENTITY_MEMBERSHIPS.GROUP_NAME)
                .fetch(r -> row(id, r.getGroupName(), r.getRole(), r.getCreatedAt()));
        });
    }

    /** A group's roster in join order (ties by identity id). */
    List<Map<String, Object>> ofGroup(String tenant, String group) {
        requireGroup(group);
        return scope.withTenant(tenant, ctx -> ctx.selectFrom(IDENTITY_MEMBERSHIPS)
            .where(IDENTITY_MEMBERSHIPS.TENANT_ID.eq(tenant)).and(IDENTITY_MEMBERSHIPS.GROUP_NAME.eq(group))
            .orderBy(IDENTITY_MEMBERSHIPS.CREATED_AT, IDENTITY_MEMBERSHIPS.IDENTITY_ID)
            .fetch(r -> row(r.getIdentityId(), group, r.getRole(), r.getCreatedAt())));
    }
}
