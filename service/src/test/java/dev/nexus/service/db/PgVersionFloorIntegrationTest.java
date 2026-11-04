// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-3s5r1: the version the check reads off a real server. The test image must pass the floor; the assertion
 * that it is not merely "not refused" is that the version read is at or above the floor. No upper bound, so a move
 * of the test image to a later major does not turn this red.
 */
class PgVersionFloorIntegrationTest {

    @Test
    void theRealServerPassesTheFloor_andTheVersionIsReadFromIt() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.start();
             var ds = PgContainerHelper.superuserDataSource(pg)) {
            PgVersionFloor.Found found = PgVersionFloor.verifyAtBoot(ds);
            assertThat(found.versionNum()).as("server_version_num of the test image")
                .isGreaterThanOrEqualTo(PgVersionFloor.MIN_SERVER_VERSION_NUM);
            assertThat(found.versionText()).as("server_version text").isNotBlank();
        }
    }
}
