// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import liquibase.changelog.ChangeSet;
import liquibase.precondition.core.PreconditionContainer;
import liquibase.precondition.core.PreconditionContainer.FailOption;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd.3: which pending changesets SchemaMigrator does not count as schema work. A changeset whose
 * precondition is {@code onFail="CONTINUE"} is skipped without a DATABASECHANGELOG row, so it is unrun on every boot
 * until its guard passes; it is self-retrying, and its body is held (by the changelog lint) off the chunk tables.
 */
class RetryingChangesetsTest {

    private static ChangeSet changeSet(FailOption onFail) {
        var cs = new ChangeSet("cs-1", "t", false, false, "db/changelog/x.xml", null, null, null);
        if (onFail != null) {
            var pre = new PreconditionContainer();
            pre.setOnFail(onFail.toString());
            cs.setPreconditions(pre);
        }
        return cs;
    }

    @Test
    void onFailContinue_isSelfRetrying() {
        assertThat(RetryingChangesets.isSelfRetrying(changeSet(FailOption.CONTINUE))).isTrue();
    }

    @Test
    void otherOnFailOptions_andNoPrecondition_areNot() {
        assertThat(RetryingChangesets.isSelfRetrying(changeSet(FailOption.HALT))).isFalse();
        assertThat(RetryingChangesets.isSelfRetrying(changeSet(FailOption.MARK_RAN))).isFalse();
        assertThat(RetryingChangesets.isSelfRetrying(changeSet(FailOption.WARN))).isFalse();
        assertThat(RetryingChangesets.isSelfRetrying(changeSet(null))).isFalse();
    }

    @Test
    void anIdentifier_namesTheFileAndTheId() {
        assertThat(RetryingChangesets.describe(changeSet(FailOption.CONTINUE))).isEqualTo("db/changelog/x.xml::cs-1");
    }
}
