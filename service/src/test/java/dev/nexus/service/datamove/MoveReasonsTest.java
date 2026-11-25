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

    // nexus-f3yqd.2.23: the class a client keys on, never the words

    @Test
    void classOf_isMissingPrerequisiteForAMissingExtension_behindTheRetryPrefix() {
        String mp = MoveReasons.MISSING_PREREQUISITE;
        assertThat(MoveReasons.classOf("pg_background is not installed")).isEqualTo(mp);
        assertThat(MoveReasons.classOf("retry 2, last 2026-11-16T23:00:00Z: pg_background is not installed")).isEqualTo(mp);
        assertThat(MoveReasons.classOf("retry 19, last 2026-11-16T23:00:00Z: pg_background is not installed")).isEqualTo(mp);
    }

    @Test
    void classOf_dropsTheClassOnceTheRetryPolicySaysNeedsAnOperator() {
        assertThat(MoveReasons.classOf("retry 20, last 2026-11-16T23:00:00Z: pg_background is not installed; needs an operator"))
            .isNull();
        assertThat(MoveReasons.classOf("pg_background is not installed; needs an operator")).isNull();
    }

    @Test
    void classOf_readsTheRetryPolicysOwnOutput_beforeAndAtTheCap() {
        var last = java.time.Instant.parse("2026-11-16T23:00:00Z");
        String miss = "pg_background is not installed";
        assertThat(MoveReasons.classOf(MoveRetryPolicy.blockedReason(MoveRetryPolicy.NEEDS_OPERATOR_AT - 1, last, miss)))
            .as("one retry short of the cap").isEqualTo(MoveReasons.MISSING_PREREQUISITE);
        String atCap = MoveRetryPolicy.blockedReason(MoveRetryPolicy.NEEDS_OPERATOR_AT, last, miss);
        assertThat(atCap).endsWith("; needs an operator");
        assertThat(MoveReasons.classOf(atCap)).as("at the cap").isNull();
        assertThat(MoveReasons.classOf(MoveRetryPolicy.blockedReason(1, last, miss))).isEqualTo(MoveReasons.MISSING_PREREQUISITE);
    }

    @Test
    void classOf_isNullForEveryOtherReason() {
        assertThat(MoveReasons.classOf(null)).isNull();
        assertThat(MoveReasons.classOf("")).isNull();
        assertThat(MoveReasons.classOf("waiting for a background worker slot")).isNull();
        assertThat(MoveReasons.classOf("dml_utils.run_migration_chunks_committing is not installed"))
            .as("the library install passed before this miss: an engine or library defect, not a missing prerequisite").isNull();
        assertThat(MoveReasons.classOf("retry 2, last 2026-11-16T23:00:00Z: dml_utils.run_migration_chunks_committing is not installed"))
            .isNull();
        assertThat(MoveReasons.classOf("library role dml_utils_owner is missing")).isNull();
        assertThat(MoveReasons.classOf("the library install has not run")).isNull();
        assertThat(MoveReasons.classOf("no separate admin role (NX_DB_ADMIN_* unset): the engine's one role holds no "
            + "dml_utils_owner grant")).isNull();
        assertThat(MoveReasons.classOf("the driving table nexus.t does not exist")).isNull();
        assertThat(MoveReasons.classOf("run m/acme/leaf raised sqlstate=42883: pg_background is not installed"))
            .as("run text that merely says it is not the install's reason").isNull();
        assertThat(MoveReasons.classOf("some_other_extension is not installed")).isNull();
    }

    // nexus-p2m1x.27 row 23: the name-key drop's deferral reads as a wait, not as an unknown failure

    private static String deferral(String tail) {
        return BuildWait.REASON_PREFIX + ": running=2; deferred_minutes=" + tail + "; " + BuildWait.Path.BEFORE_LOCKS.text
            + "; the completion tries again later and ends none";
    }

    @Test
    void aDeferralHasAPhraseOfItsOwn_notTheFallbackThatPointsAtTheLog() {
        String text = MoveReasons.publicForm(deferral("75"));

        assertThat(text).startsWith("waiting for in-flight index builds to finish (2 running)").contains("none is ended")
            .contains("waiting for 1 h 15 m").doesNotContain("engine log").doesNotContain("pids").doesNotContain("overdue");
    }

    @Test
    void anOverdueDeferralNamesTheAgeTheCountAndThePids_andNothingElse() {
        String reason = BuildWait.REASON_PREFIX + ": running=2; deferred_minutes=432; overdue; pids=4101,4102; "
            + BuildWait.Path.LOCK_WAIT.text + "; the completion tries again later and ends none";

        String text = MoveReasons.publicForm(reason);

        assertThat(text).contains("(2 running)").contains("waiting for 7 h 12 m").contains("overdue, more than 6 h")
            .contains("builder pids 4101,4102").doesNotContain("lock wait");
        assertThat(MoveReasons.publicForm(reason + "; pids=oops")).as("a number list only: the rest is not carried").doesNotContain("oops");
    }

    @Test
    void classOf_giveADeferralItsOwnClass_neverNull_andOverdueIsAClassOfItsOwn() {
        assertThat(MoveReasons.classOf(deferral("0"))).isEqualTo(MoveReasons.WAITING_FOR_BUILDS).isEqualTo("waiting_for_builds");
        assertThat(MoveReasons.classOf(BuildWait.REASON_PREFIX + ": running=1; deferred_minutes=400; overdue; pids=9; x"))
            .isEqualTo(MoveReasons.WAITING_FOR_BUILDS_OVERDUE).isEqualTo("waiting_for_builds_overdue");
        assertThat(MoveReasons.classOf("run m/acme raised sqlstate=55P03: " + BuildWait.REASON_PREFIX))
            .as("anchored: text that merely mentions the phrase is not a deferral").isNull();
    }
}
