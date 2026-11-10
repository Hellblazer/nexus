// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.BackendReaper;
import dev.nexus.service.db.PgSession.PciSettings;
import org.junit.jupiter.api.Test;

import java.util.Properties;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-227 Step 2 (nexus-43ulx.16, fix round): how {@link PciBuilderSession} connects, with no database. The builder's
 * socket is held for up to 35 minutes by a build, so it is bound at login and kept alive, and a connect that fails
 * says why without ever saying the credentials.
 */
class PciBuilderSessionConnectTest {

    private static final PciSettings ON = new PciSettings(true, 20_000, 600, 16);

    @Test
    void theConnection_isBoundAtLogin_andKeptAlive() {
        Properties p = new PciBuilderSession("jdbc:postgresql://127.0.0.1:1/x", "u", "p", "n1", ON)
            .connectionProperties();

        assertThat(p.getProperty("tcpKeepAlive")).isEqualTo("true");
        assertThat(p.getProperty("loginTimeout")).as("the whole handshake, not only the TCP connect")
            .isEqualTo(Integer.toString(PciBuilderSession.LOGIN_TIMEOUT_SECONDS));
        assertThat(p.getProperty("connectTimeout")).isEqualTo(Integer.toString(BackendReaper.CONNECT_TIMEOUT_SECONDS));
        assertThat(p.getProperty("socketTimeout")).isEqualTo(Integer.toString(PciBuilderSession.SOCKET_TIMEOUT_SECONDS));
        assertThat(p.getProperty("ApplicationName")).isEqualTo("nexus-pci-builder-n1");
    }

    @Test
    void anUnreachableDatabase_failsWithTheDriversReason_andNeverTheCredentials() {
        // Never asserted by value in a message: a failure here must not echo it either.
        String secret = "s3cret-" + UUID.randomUUID();
        var session = new PciBuilderSession("jdbc:postgresql://127.0.0.1:1/nowhere?password=" + secret, "u", secret,
            "n1", ON);

        assertThatThrownBy(session::open)
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("pci builder could not connect")
            .hasMessageContaining("cause=")
            .hasMessageContaining("refused")
            .satisfies(e -> assertThat(e.getMessage()).doesNotContain(secret));
    }

    /**
     * nexus-f3yqd.3: the progress sampler's connect failure reaches a log line. A driver that echoes the URL ("No suitable
     * driver found for ...") would carry a password parameter into it, so the message is scrubbed as the builder's
     * other connect paths are.
     */
    @Test
    void theProgressSamplersConnectFailure_isScrubbed_neverCarryingThePassword() {
        String secret = "s3cret-" + UUID.randomUUID();
        var session = new PciBuilderSession("jdbc:nosuchdriver://h/d?user=u&password=" + secret, "u", secret, "n1", ON);

        assertThatThrownBy(session::sampleBuildProgress)
            .isInstanceOf(java.sql.SQLException.class)
            .hasMessageContaining("progress read failed")
            .hasMessageContaining("No suitable driver")
            .hasMessageContaining("password=***")
            .satisfies(e -> assertThat(e.getMessage()).doesNotContain(secret));
    }

    /** The whole {@code 08xxx} class and {@code 57P01} end a pass as a lost connection; a failed statement does not. */
    @Test
    void isConnectionLoss_coversTheWholeConnectionExceptionClass_andTheTerminationState() {
        for (String lost : new String[] {"08000", "08001", "08003", "08006", "08P01", "57P01"}) {
            assertThat(PciBuilderSession.Pass.isConnectionLoss(lost)).as(lost).isTrue();
        }
        for (String statement : new String[] {"57014", "42501", "42P07", "55P03", "28000", "28P01", "0A000", "40001"}) {
            assertThat(PciBuilderSession.Pass.isConnectionLoss(statement)).as(statement).isFalse();
        }
        assertThat(PciBuilderSession.Pass.isConnectionLoss(null)).isFalse();
    }

    @Test
    void scrub_masksThePasswordAndAnyPasswordUrlParameter_andKeepsTheLineOnOneRow() {
        var session = new PciBuilderSession("jdbc:postgresql://h/d", "u", "hunter2", "n1", ON);

        String out = session.scrub("No suitable driver found for jdbc:x://h/d?user=u&password=hunter2&ssl=true"
            + "\nFATAL: password authentication failed for user \"u\" (hunter2)");

        assertThat(out).doesNotContain("hunter2").doesNotContain("\n").doesNotContain("\"");
        assertThat(out).contains("password=***&ssl=true").contains("password authentication failed for user 'u'");
        assertThat(session.scrub(null)).isEmpty();
    }

    /**
     * Only a failed dynamic shared memory resize (its message names the segment) switches the builder to serial
     * builds for the engine's lifetime. A lock-table exhaustion is the same SQLSTATE, 53200, and its message also says
     * "shared memory", but running serially does not help it.
     */
    @Test
    void sharedMemoryShort_matchesTheSegmentResizeFailure_andNotALockTableShortage() throws Exception {
        assertThat(PciBuilderSession.sharedMemoryShort(new java.sql.SQLException(
            "ERROR: could not resize shared memory segment \"/PostgreSQL.1\" to 2147483648 bytes: No space left on device",
            "53100"))).as("ENOSPC on /dev/shm").isTrue();
        assertThat(PciBuilderSession.sharedMemoryShort(new java.sql.SQLException(
            "ERROR: could not resize shared memory segment \"/PostgreSQL.1\" to 2147483648 bytes: Cannot allocate memory",
            "53200"))).as("out_of_memory on /dev/shm").isTrue();
        assertThat(PciBuilderSession.sharedMemoryShort(new java.sql.SQLException(
            "ERROR: out of shared memory\n  Hint: You might need to increase max_locks_per_transaction.", "53200")))
            .as("the lock table is full").isFalse();
        assertThat(PciBuilderSession.sharedMemoryShort(new java.sql.SQLException(
            "ERROR: could not resize shared memory segment \"/PostgreSQL.1\"", "42501")))
            .as("not a resource SQLSTATE").isFalse();
        assertThat(PciBuilderSession.sharedMemoryShort(new java.sql.SQLException(null, "53100"))).isFalse();
    }

    /**
     * nexus-f3yqd: a build runs under its own, long statement timeout, finite because it is also the bound on how long
     * an orphaned stalled build holds the builder lock; the client socket must outlast it, or the client would cut a
     * build the server still allows.
     */
    @Test
    void theBuildStatementTimeout_isSixHours_andTheSocketOutlastsIt() {
        assertThat(PciBuilderSession.BUILD_STATEMENT_TIMEOUT).isEqualTo(java.time.Duration.ofHours(6));
        assertThat(PciBuilderSession.OTHER_STATEMENT_TIMEOUT).as("drops and comments keep the short bound")
            .isEqualTo(java.time.Duration.ofMinutes(30));
        assertThat(java.time.Duration.ofSeconds(PciBuilderSession.SOCKET_TIMEOUT_SECONDS))
            .isGreaterThan(PciBuilderSession.BUILD_STATEMENT_TIMEOUT);
    }
}
