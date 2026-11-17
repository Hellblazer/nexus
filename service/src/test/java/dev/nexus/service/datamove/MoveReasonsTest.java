// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** nexus-f3yqd.2.17: the recovery words a checksum-drifted library install gets after {@code blocked:}. */
class MoveReasonsTest {

    @Test
    void aChecksumFailure_namesTheChangesetAndWhatToDo() {
        String liquibase = "a library changelog failed: liquibase.exception.ValidationFailedException: Validation Failed: "
            + "1 changesets check sum db/dml_utils/changelog.xml::001-create-schema::alice was: 9:aaa but is now: 9:bbb";

        String hinted = MoveReasons.withInstallHint(liquibase);

        assertThat(hinted).startsWith(liquibase).contains("recovery:")
            .contains("changeset 001-create-schema (db/dml_utils/changelog.xml)")
            .contains("not runOnChange").contains("md5sum");
    }

    @Test
    void aChecksumFailureWithNoChangesetInIt_stillGetsTheRecovery() {
        assertThat(MoveReasons.withInstallHint("a library changelog failed: Validation Failed: 1 changesets check sum"))
            .contains("a library re-pin changed a changeset after the install had applied it");
    }

    @Test
    void anyOtherReason_isReturnedAsItCame() {
        assertThat(MoveReasons.withInstallHint("library role dml_utils_owner is missing"))
            .isEqualTo("library role dml_utils_owner is missing");
        assertThat(MoveReasons.withInstallHint("the library install has not run"))
            .isEqualTo("the library install has not run");
    }
}
