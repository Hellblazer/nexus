// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import liquibase.changelog.ChangeSet;
import liquibase.precondition.core.PreconditionContainer;
import liquibase.precondition.core.PreconditionContainer.FailOption;

/**
 * Which pending changesets {@code SchemaMigrator} does not count as schema work when it decides whether to end the
 * per-collection index builders (nexus-f3yqd.3).
 *
 * <p>A changeset whose preconditions are {@code onFail="CONTINUE"} is skipped without a {@code DATABASECHANGELOG}
 * row, so it is unrun on every boot until its guard passes ({@code aspects-005-2} waits for the retired aspect queue
 * to empty). Counting it would end the build on every boot of such a store, and once more after the guard passes.
 * The exemption is sound only while the body of such a changeset takes no lock a {@code CREATE INDEX CONCURRENTLY}
 * waits behind; {@code tests/test_changelog_continue_precondition_chunks_lint.py} holds every such body off the
 * chunk tables, the partition helpers and the builder.
 */
final class RetryingChangesets {
    private RetryingChangesets() {}

    /** True when the changeset's preconditions say {@code onFail="CONTINUE"}. */
    static boolean isSelfRetrying(ChangeSet changeSet) {
        PreconditionContainer preconditions = changeSet.getPreconditions();
        return preconditions != null && preconditions.getOnFail() == FailOption.CONTINUE;
    }

    /** {@code <file>::<id>}, the form the boot log names an excluded changeset in. */
    static String describe(ChangeSet changeSet) {
        return changeSet.getFilePath() + "::" + changeSet.getId();
    }
}
