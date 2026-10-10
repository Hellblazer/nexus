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

    @Test
    void scrub_masksThePasswordAndAnyPasswordUrlParameter_andKeepsTheLineOnOneRow() {
        var session = new PciBuilderSession("jdbc:postgresql://h/d", "u", "hunter2", "n1", ON);

        String out = session.scrub("No suitable driver found for jdbc:x://h/d?user=u&password=hunter2&ssl=true"
            + "\nFATAL: password authentication failed for user \"u\" (hunter2)");

        assertThat(out).doesNotContain("hunter2").doesNotContain("\n").doesNotContain("\"");
        assertThat(out).contains("password=***&ssl=true").contains("password authentication failed for user 'u'");
        assertThat(session.scrub(null)).isEmpty();
    }
}
