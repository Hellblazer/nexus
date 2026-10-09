// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** RDR-227 (nexus-43ulx.16): the admin values Main reads once and hands to migration and the index builder. */
class AdminConnectionTest {

    @Test
    void allThreeSet_areTheAdminValues() {
        var env = Map.of("NX_DB_ADMIN_URL", "jdbc:postgresql://admin/db", "NX_DB_ADMIN_USER", "admin",
            "NX_DB_ADMIN_PASS", "admin-pass");
        assertThat(AdminConnection.resolve(env::get, "jdbc:postgresql://app/db", "app", "app-pass"))
            .isEqualTo(new AdminConnection("jdbc:postgresql://admin/db", "admin", "admin-pass"));
    }

    @Test
    void noneSet_fallsBackToTheApplicationValues() {
        assertThat(AdminConnection.resolve(k -> null, "jdbc:postgresql://app/db", "app", "app-pass"))
            .isEqualTo(new AdminConnection("jdbc:postgresql://app/db", "app", "app-pass"));
    }

    @Test
    void oneOrTwoSet_refuses() {
        assertThatThrownBy(() -> AdminConnection.resolve(Map.of("NX_DB_ADMIN_USER", "admin")::get, "u", "a", "p"))
            .isInstanceOf(IllegalStateException.class).hasMessageContaining("1/3");
        assertThatThrownBy(() -> AdminConnection.resolve(
            Map.of("NX_DB_ADMIN_USER", "admin", "NX_DB_ADMIN_PASS", "x")::get, "u", "a", "p"))
            .isInstanceOf(IllegalStateException.class).hasMessageContaining("2/3");
    }

    @Test
    void toString_neverShowsThePassword() {
        assertThat(new AdminConnection("u", "admin", "s3cret-value").toString()).doesNotContain("s3cret-value");
    }

    @Test
    void bootNonce_isTheLastSegmentOfThisBootsApplicationName() {
        String name = BackendReaper.newApplicationName("7.76.0");
        assertThat(BackendReaper.bootNonce(name)).matches("[0-9a-f]{8}");
        assertThat(name).endsWith("/" + BackendReaper.bootNonce(name));
        assertThatThrownBy(() -> BackendReaper.bootNonce("no-slash")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> BackendReaper.bootNonce("trailing/")).isInstanceOf(IllegalArgumentException.class);
    }
}
