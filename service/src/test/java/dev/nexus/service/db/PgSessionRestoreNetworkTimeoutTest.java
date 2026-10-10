// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * {@link PgSession#restoreNetworkTimeout} reports whether the timeout is back: the index builder owns its connection
 * and ends its pass when it is not (a short socket bound left under a 30-minute build cuts the build off).
 */
class PgSessionRestoreNetworkTimeoutTest {

    private static Connection connection(List<Integer> seen, boolean fail) {
        return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> {
                if (method.getName().equals("setNetworkTimeout")) {
                    seen.add((Integer) args[1]);
                    if (fail) {
                        throw new SQLException("connection closed", "08003");
                    }
                    return null;
                }
                throw new UnsupportedOperationException(method.getName());
            });
    }

    @Test
    void returnsTrueAndSetsTheTimeout() {
        List<Integer> seen = new ArrayList<>();

        assertThat(PgSession.restoreNetworkTimeout(connection(seen, false), 2_100_000)).isTrue();
        assertThat(seen).containsExactly(2_100_000);
    }

    @Test
    void returnsFalse_ratherThanThrowing_whenTheConnectionRefusesIt() {
        List<Integer> seen = new ArrayList<>();

        assertThat(PgSession.restoreNetworkTimeout(connection(seen, true), 2_100_000)).isFalse();
        assertThat(seen).containsExactly(2_100_000);
    }
}
