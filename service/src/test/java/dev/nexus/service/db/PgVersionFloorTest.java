// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.sql.SQLException;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-3s5r1: the engine refuses to boot below PostgreSQL 17. The decision and the version source are separate, so
 * this class needs no database; {@code PgVersionFloorIntegrationTest} reads the version off a real server.
 */
class PgVersionFloorTest {

    @Test
    void theFloorIsPostgres17() {
        assertThat(PgVersionFloor.MIN_SERVER_VERSION_NUM).isEqualTo(170000);
    }

    @Test
    void aServerBelowTheFloorIsRefused_naming_theVersionFoundAndTheFloor() {
        for (int below : new int[] {160000, 160004, 150010, 169999, 90624}) {
            Optional<String> refusal = PgVersionFloor.refusal(new PgVersionFloor.Found(below, "x"));
            assertThat(refusal).as("server_version_num " + below).isPresent();
        }
        String msg = PgVersionFloor.refusal(new PgVersionFloor.Found(160004, "16.4 (Debian 16.4-1)")).orElseThrow();
        assertThat(msg).contains("16.4 (Debian 16.4-1)").contains("160004")
            .contains("PostgreSQL 17").contains("170000")
            .contains("https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md#storage-service-postgres-prerequisites");
    }

    @Test
    void aServerAtOrAboveTheFloorPasses() {
        for (int ok : new int[] {170000, 170005, 170010, 180000, 190001}) {
            assertThat(PgVersionFloor.refusal(new PgVersionFloor.Found(ok, "x")))
                .as("server_version_num " + ok).isEmpty();
        }
    }

    @Test
    void verifyThrowsOnAServerBelowTheFloor_andReturnsTheVersionOtherwise() {
        assertThatThrownBy(() -> PgVersionFloor.verify(() -> new PgVersionFloor.Found(160004, "16.4")))
            .isInstanceOf(PgVersionFloor.FloorException.class)
            .hasMessageContaining("16.4").hasMessageContaining("170000");
        assertThat(PgVersionFloor.verify(() -> new PgVersionFloor.Found(170005, "17.5")).versionNum())
            .isEqualTo(170005);
    }

    @Test
    void aVersionThatCannotBeReadIsARefusal_notAPass() {
        assertThatThrownBy(() -> PgVersionFloor.verify(() -> { throw new SQLException("pool is down"); }))
            .isInstanceOf(PgVersionFloor.FloorException.class)
            .hasMessageContaining("pool is down").hasMessageContaining("server_version_num");
        assertThatCode(() -> PgVersionFloor.verify(() -> new PgVersionFloor.Found(170000, "17.0")))
            .doesNotThrowAnyException();
    }
}
