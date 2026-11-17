// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.db.DrivingTableIsolationCheck.IsolationException;
import dev.nexus.service.db.DrivingTableIsolationCheck.Probe;
import dev.nexus.service.db.DrivingTableIsolationCheck.Table;
import dev.nexus.service.db.DrivingTableIsolationCheck.Violation;
import org.junit.jupiter.api.Test;

import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;

/**
 * nexus-f3yqd.2.16: the retry and refusal logic of {@link DrivingTableIsolationCheck}, with no database. What the check
 * asks the server, and which tables it asks about, is {@code DataMoveOwnerReadPolicyIntegrationTest}.
 */
class DrivingTableIsolationCheckTest {

    private static final Table ROWS = new Table("dm_ownerread", "rows");
    private static final long[] BACKOFF = {10L, 20L, 40L};

    private static Probe clean() {
        return new Probe("nexus_svc", 1, null);
    }

    @Test
    void aCleanProbePasses_withoutSleeping() {
        var slept = new ArrayList<Long>();
        DrivingTableIsolationCheck.verifyAtStartup(DrivingTableIsolationCheckTest::clean, BACKOFF, slept::add);
        assertThat(slept).isEmpty();
    }

    @Test
    void aTransientFailureIsRetriedAfterEachPause_thenPasses() {
        var calls = new int[1];
        Callable<Probe> flaky = () -> {
            if (calls[0]++ < 2) {
                throw new SQLException("connection reset", "08006");
            }
            return clean();
        };
        var slept = new ArrayList<Long>();
        DrivingTableIsolationCheck.verifyAtStartup(flaky, BACKOFF, slept::add);
        assertThat(calls[0]).isEqualTo(3);
        assertThat(slept).containsExactly(10L, 20L);
    }

    @Test
    void aTransientFailureThatOutlastsTheBackoff_isARefusal() {
        var calls = new int[1];
        Callable<Probe> down = () -> {
            calls[0]++;
            throw new SQLException("too many connections", "53300");
        };
        assertThatThrownBy(() -> DrivingTableIsolationCheck.verifyAtStartup(down, BACKOFF, ms -> { }))
            .isInstanceOf(IsolationException.class)
            .hasMessageContaining("could not check").hasMessageContaining("too many connections");
        assertThat(calls[0]).as("the first try and one per pause").isEqualTo(BACKOFF.length + 1);
    }

    @Test
    void aFailureThatIsNotTransient_isARefusalAtOnce() {
        var calls = new int[1];
        Callable<Probe> broken = () -> {
            calls[0]++;
            throw new SQLException("relation \"nexus.data_moves\" does not exist", "42P01");
        };
        assertThatThrownBy(() -> DrivingTableIsolationCheck.verifyAtStartup(broken, BACKOFF, ms -> { }))
            .isInstanceOf(IsolationException.class).hasMessageContaining("could not check");
        assertThat(calls[0]).isEqualTo(1);
    }

    @Test
    void aViolationIsNeverRetried_andTheRefusalNamesTheTablePolicyRoleAndBothRemedies() {
        var calls = new int[1];
        Callable<Probe> exposed = () -> {
            calls[0]++;
            return new Probe("nexus_svc", 2, DrivingTableIsolationCheck.refusal("nexus_svc", List.of(
                new Violation(ROWS, "rows_owner_read", "nexus_admin"),
                new Violation(new Table("dm_ownerread", "other"), "other_owner_read", "public"))));
        };
        assertThatThrownBy(() -> DrivingTableIsolationCheck.verifyAtStartup(exposed, BACKOFF, ms -> { }))
            .isInstanceOf(IsolationException.class)
            .hasMessageContaining("rows_owner_read").hasMessageContaining("dm_ownerread.rows")
            .hasMessageContaining("nexus_svc").hasMessageContaining("role nexus_admin")
            .hasMessageContaining("1 more violation(s) follow it")
            .hasMessageContaining("as the table owner")
            .hasMessageContaining("WITH INHERIT FALSE").hasMessageContaining("NX_DB_ADMIN_URL");
        assertThat(calls[0]).isEqualTo(1);
    }

    @Test
    void aPolicyReachingTheRoleThroughPublicSaysPublic() {
        assertThat(DrivingTableIsolationCheck.refusal("nexus_svc",
            List.of(new Violation(ROWS, "rows_owner_read", "public")))).contains("through PUBLIC");
    }
}
