// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-193 Phase 1 gate seam fixes (nexus-u5m5g.5) on {@code catalog_link_write} (catalog-048-7) and the reconcile
 * commit: an alias of a dead canonical is dangling, a rename move does not carry a link to one, and two runs of one owner
 * commit one after the other. Split from {@code CatalogReconcileCommitBehaviourTest}, which is at its file-size cap.
 */
class CatalogReconcileCommitSeamsTest extends CatalogReconcileTestBase {

    private static final String OWNER = "1.1";

    @BeforeAll
    void boot() throws Exception {
        migrate();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, "svc_rc048_seams", "svc_rc048_seams_pass");
        }
        connect("svc_rc048_seams", "svc_rc048_seams_pass");
    }

    private String tenantWithOwner() {
        String t = newTenant();
        owner(t, OWNER, "/r");
        return t;
    }

    private String linkWrite(String tenant, String from, String to, String type, String fromSpan, String toSpan,
                             String by, boolean ifAbsent) {
        return scope.withTenant(tenant, ctx -> ctx.select(dev.nexus.service.jooq.nexus.Routines.catalogLinkWrite(
                from, to, type, fromSpan, toSpan, by, ifAbsent))
            .fetchOne(0, String.class));
    }

    /** Locks a run's header row FOR UPDATE in {@code su}'s open transaction, as the commit's first statement does. */
    private void holdRunHeader(Connection su, String tenant, String run) {
        suCtx(su).selectOne().from(dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS)
            .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS.TENANT_ID.eq(tenant)
                .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS.RUN_ID.eq(run)))
            .forUpdate().fetch();
    }

    /**
     * nexus-u5m5g.5 (catalog-048-7): an endpoint whose alias walk ends on a row that is tombstoned, missing, or still an
     * alias (a cycle) is dangling, as POST /link answers it; the 048-4 body wrote the link to the alias row. A control
     * (an alias of a live canonical) still writes to the canonical.
     */
    @Test
    void theLinkWriteHelper_anAliasWhoseCanonicalIsDeadGoneOrCyclic_isDangling_andWritesNothing() {
        String t = tenantWithOwner();
        String x = seed(t, OWNER, "x.py");
        String live = seed(t, OWNER, "live.py");
        String aliasOfLive = seed(t, OWNER, "alias-live.py");
        String deadCanon = seed(t, OWNER, "dead-canon.py");
        String aliasOfDead = seed(t, OWNER, "alias-dead.py");
        String aliasOfGone = seed(t, OWNER, "alias-gone.py");
        String cycleA = seed(t, OWNER, "cycle-a.py");
        String cycleB = seed(t, OWNER, "cycle-b.py");
        setDoc(t, aliasOfLive, CATALOG_DOCUMENTS.ALIAS_OF, live);
        setDoc(t, aliasOfDead, CATALOG_DOCUMENTS.ALIAS_OF, deadCanon);
        setDoc(t, aliasOfGone, CATALOG_DOCUMENTS.ALIAS_OF, OWNER + ".777");
        setDoc(t, cycleA, CATALOG_DOCUMENTS.ALIAS_OF, cycleB);
        setDoc(t, cycleB, CATALOG_DOCUMENTS.ALIAS_OF, cycleA);
        tombstone(t, deadCanon, false);

        assertThat(linkWrite(t, x, aliasOfLive, "cites", "", "", "u", false)).as("control: a live canonical").isEqualTo("created");
        assertThat(links(t)).containsExactly(x + ">" + live + ":cites:u:::");

        assertThat(linkWrite(t, x, aliasOfDead, "cites", "", "", "u", false)).as("to-side: canonical tombstoned").isEqualTo("dangling");
        assertThat(linkWrite(t, aliasOfDead, x, "cites", "", "", "u", false)).as("from-side: canonical tombstoned").isEqualTo("dangling");
        assertThat(linkWrite(t, x, aliasOfGone, "cites", "", "", "u", false)).as("canonical absent").isEqualTo("dangling");
        assertThat(linkWrite(t, x, cycleA, "cites", "", "", "u", false)).as("an alias cycle ends on an alias").isEqualTo("dangling");
        assertThat(linkWrite(t, x, aliasOfDead, "cites", "", "", "u", true)).as("if-absent, too").isEqualTo("dangling");
        assertThat(links(t)).as("none of those wrote").hasSize(1);
    }

    /**
     * nexus-u5m5g.5: the rename move carries a link through catalog_link_write, so a link to an alias of a dead canonical
     * is not carried (POST /link answers dangling for it), while a link to an alias of a live canonical still lands on
     * the canonical ({@code aRenameMove_followsAliasOf_likeTodaysPostLink}). This is a Java invariant test, not a parity
     * case: the client path's rename loop has no handler for that 400 (its {@code ValueError} ends the whole housekeeping
     * as {@code catalog_hook_failed}), so there is no old-path end state to compare the commit with.
     */
    @Test
    void aRenameMove_doesNotCarryALinkToAnAliasOfADeadCanonical() {
        String t = tenantWithOwner();
        String old = seed(t, OWNER, "old.py", Map.of("meta", Map.of("content_hash", "H")));
        String deadCanon = seed(t, OWNER, "dead-canon.py", Map.of("head_hash", "H1"));
        String aliasRow = seed(t, OWNER, "alias.py", Map.of("head_hash", "H1"));
        String other = seed(t, OWNER, "other.py", Map.of("head_hash", "H1"));
        link(t, old, aliasRow, "cites", "alice", "1-2", "3-4");
        link(t, old, other, "relates", "bob", "", "");
        setDoc(t, aliasRow, CATALOG_DOCUMENTS.ALIAS_OF, deadCanon);
        tombstone(t, deadCanon, false);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete",
            present(0, "new.py").hash("H"), present(1, "other.py"));

        String neu = tumblerOf(out, "new.py");
        assertThat(links(t)).as("the live partner's link is carried; the alias row's is not, and nothing lands on the alias row")
            .contains(neu + ">" + other + ":relates:bob:::")
            .noneMatch(l -> l.startsWith(neu + ">" + aliasRow) || l.startsWith(neu + ">" + deadCanon));
    }


    /**
     * nexus-u5m5g.5: the engine's per-owner commit scope (EngineJobs) lives in one process; a second engine replica can
     * start another run of the same owner. The commit takes a transaction-scoped advisory lock on (tenant, owner) before
     * its function runs, so two runs of one owner commit one after the other, and another owner's run is not held. Run A
     * is parked on its own header row (taken FOR UPDATE here) after it holds the owner lock: without the lock, B would
     * commit at once and only A would be blocked.
     */
    @Test
    void twoRunsOfOneOwner_commitOneAfterTheOther_anotherOwnersRunIsNotHeld() throws Exception {
        String t = tenantWithOwner();
        owner(t, "1.2", "/r2");
        stage(t, OWNER, "runA", "partial", present(0, "a.py"));
        stage(t, OWNER, "runB", "partial", present(0, "b.py"));
        stage(t, "1.2", "runC", "partial", present(0, "c.py"));
        var pool = java.util.concurrent.Executors.newFixedThreadPool(3);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            holdRunHeader(su, t, "runA");
            var a = pool.submit(() -> repo.reconcileCommit(t, OWNER, "runA", COMMIT_TIMEOUT));
            awaitCommitsBlocked(su, 1);                       // A holds the owner lock and waits on its header
            var b = pool.submit(() -> repo.reconcileCommit(t, OWNER, "runB", COMMIT_TIMEOUT));
            awaitCommitsBlocked(su, 2);                       // B waits on the owner lock (without it B would be done)
            assertThat(b.isDone()).as("the second run of the owner waits for the first").isFalse();
            assertThat(runState(t, "runB")).as("and has written nothing").isEqualTo("staged");
            assertThat(liveCount(t, OWNER)).as("no document of B or A yet").isZero();

            Map<String, Object> other = pool.submit(() -> repo.reconcileCommit(t, "1.2", "runC", COMMIT_TIMEOUT))
                .get(60, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(outcomeRows(other)).as("another owner's run commits while A and B wait").hasSize(1);
            assertThat(b.isDone()).isFalse();

            su.rollback();                                    // A proceeds, then B
            assertThat(outcomeRows(a.get(60, java.util.concurrent.TimeUnit.SECONDS))).hasSize(1);
            assertThat(outcomeRows(b.get(60, java.util.concurrent.TimeUnit.SECONDS))).hasSize(1);
        } finally {
            pool.shutdownNow();
        }
        assertThat(runState(t, "runA")).isEqualTo("committed");
        assertThat(runState(t, "runB")).isEqualTo("committed");
        assertThat(liveCount(t, OWNER)).as("a.py and b.py").isEqualTo(2);
    }

    // ── resolve_or_register's sensitive flag (RDR-193 Step 6, Sam's ask rdr193-resolve-sensitive, option 1) ──

    private Map<String, Object> resolveReq(String uri, Object sensitive) {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("owner_name", "knowledge");
        m.put("source_uri", uri);
        m.put("title", "T");
        if (sensitive != null) m.put("sensitive", sensitive);
        return m;
    }

    private boolean sensitiveOf(String t, String tumbler) {
        return scope.withTenant(t, ctx -> ctx.select(CATALOG_DOCUMENTS.SENSITIVE).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
            .fetchOne(CATALOG_DOCUMENTS.SENSITIVE));
    }

    @Test
    void resolveOrRegister_aNewRowWithSensitiveTrue_isBornFlagged_andAuditedLikeTheCommitsInsert() {
        String t = newTenant();
        var out = repo.resolveOrRegister(t, resolveReq("nx-note://c1/born", true));

        assertThat(out.created()).isTrue();
        assertThat(sensitiveOf(t, out.tumbler())).as("born flagged").isTrue();
        assertThat(audit(t)).as("one audit row, the reason and actor the reconcile commit writes for a row it inserts flagged")
            .hasSize(1).first().asString().contains(out.tumbler()).contains("register_set").contains("engine");
    }

    @Test
    void resolveOrRegister_aFoundRowKeepsItsSensitivity_whateverTheRequestSays_andWritesNoAudit() {
        String t = newTenant();
        var plain = repo.resolveOrRegister(t, resolveReq("nx-note://c1/plain", null));
        assertThat(plain.created()).isTrue();
        assertThat(audit(t)).isEmpty();

        var again = repo.resolveOrRegister(t, resolveReq("nx-note://c1/plain", true));

        assertThat(again.created()).isFalse();
        assertThat(again.tumbler()).isEqualTo(plain.tumbler());
        assertThat(sensitiveOf(t, plain.tumbler())).as("a resolve never raises an existing row").isFalse();
        assertThat(audit(t)).as("and writes no audit row").isEmpty();

        var flagged = repo.resolveOrRegister(t, resolveReq("nx-note://c1/flagged", true));
        var lower = repo.resolveOrRegister(t, resolveReq("nx-note://c1/flagged", false));
        assertThat(lower.created()).isFalse();
        assertThat(sensitiveOf(t, flagged.tumbler())).as("nor lowers one").isTrue();
        assertThat(audit(t)).as("only the birth is audited").hasSize(1);
    }

    @Test
    void resolveOrRegister_aNonBooleanSensitiveIsRefusedNamingTheField_andWritesNothing() {
        String t = newTenant();
        for (Object bad : java.util.List.of("true", "yes", 1, java.util.List.of(true))) {
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> repo.resolveOrRegister(t, resolveReq("nx-note://c1/bad", bad)))
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("'sensitive' must be a boolean");
        }
        assertThat(audit(t)).isEmpty();
    }
}
