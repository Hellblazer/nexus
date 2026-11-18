// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** nexus-f3yqd.2.17: which ACL entries the registry check counts as a privilege held by the serving role. */
class MovePreflightGrantsToTest {

    @Test
    void aGrantToTheRoleCounts_whateverThePrivilegeAndTheGrantor() {
        assertThat(MovePreflight.grantsTo(new String[] {"nexus_admin=arwdDxtm/nexus_admin", "nexus_svc=r/nexus_admin"},
            "nexus_svc")).isTrue();
        assertThat(MovePreflight.grantsTo(new String[] {"nexus_svc=m/postgres"}, "nexus_svc")).isTrue();
    }

    @Test
    void theOwnersOwnEntryPublicAndAnotherRoleDoNot() {
        assertThat(MovePreflight.grantsTo(new String[] {"nexus_admin=arwdDxtm/nexus_admin"}, "nexus_svc")).isFalse();
        assertThat(MovePreflight.grantsTo(new String[] {"=r/nexus_admin"}, "nexus_svc")).as("PUBLIC is not the role").isFalse();
        assertThat(MovePreflight.grantsTo(new String[] {"nexus_svc_other=r/nexus_admin"}, "nexus_svc"))
            .as("a role whose name begins with the role's").isFalse();
    }

    @Test
    void anEmptyPrivilegeListOrNoAclIsNothingHeld() {
        assertThat(MovePreflight.grantsTo(null, "nexus_svc")).isFalse();
        assertThat(MovePreflight.grantsTo(new String[0], "nexus_svc")).isFalse();
        assertThat(MovePreflight.grantsTo(new String[] {"nexus_svc=/nexus_admin"}, "nexus_svc")).isFalse();
    }
}
