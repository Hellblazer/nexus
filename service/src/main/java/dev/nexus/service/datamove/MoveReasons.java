// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Words an operator reads after {@code blocked:} and {@code failed:} (nexus-f3yqd.2.17): the recovery added to a library
 * install that failed its checksum validation, and the reduction of a reason to what {@code boot_work} may say.
 */
final class MoveReasons {

    private MoveReasons() { /* static */ }

    /**
     * Liquibase names a changeset as {@code <file>::<id>::<author>}; the file is a changelog (an extension), so text a run or a
     * tenant wrote that happens to hold two {@code ::} is not taken for one.
     */
    private static final Pattern CHANGESET =
        Pattern.compile("([\\w./-]+\\.(?:xml|ya?ml|json|sql))::([\\w.-]+)::([\\w.-]+)");
    /**
     * The installer's own reason for a library changelog that failed ({@code DmlUtilsInstaller}: "a library changelog failed: "
     * and the exception), alone or after the retry count the failure handler puts in front. A checksum failure is only ever
     * that reason, so the checksum branch anchors on it.
     */
    private static final Pattern INSTALL_FAILED = Pattern.compile("^(?:retry \\d+.*?: )?a library changelog failed: ");
    private static final Pattern NEEDS_OPERATOR = Pattern.compile(";\\s*needs an operator\\s*$");
    private static final Pattern SQLSTATE = Pattern.compile("sqlstate=([0-9A-Z]{5})");
    /**
     * The failure handler's prefix on a transient reason ({@code MoveRetryPolicy.blockedReason}): {@code retry N, last <instant>: }.
     * {@link #phrase} reads the reason without it, since every phrase it knows is anchored on the reason's own first words.
     */
    private static final Pattern RETRY = Pattern.compile("^\\s*retry (\\d+)(?:, last \\S+)?: ");
    private static final Pattern LIBRARY_ROLE = Pattern.compile("^library role ([\\w, ]+) is missing");
    private static final Pattern NOT_INSTALLED = Pattern.compile("^([\\w.]+) is not installed\\s*$");

    /**
     * The {@code boot_work} {@code reason_class} of a move blocked because the {@code pg_background} extension is not installed
     * (nexus-f3yqd.2.23): {@code nx doctor} shows it as a warning, since the move waits for the install (and an engine restart,
     * which is when {@code DmlUtilsInstaller} looks) and nothing is wrong with the data. A client keys on this value, never on
     * the words of the reason. Not given to the {@code dml_utils} entry-point miss: that one comes after the library install
     * passed, so it points at an engine or library defect and stays red.
     */
    static final String MISSING_PREREQUISITE = "missing_prerequisite";

    /**
     * The {@code reason_class} of the name-key drop move while its completion waits for an in-flight index build
     * ({@link BuildWait}; nexus-p2m1x.27): nothing is wrong and nothing needs doing, the move continues by itself once no build
     * runs. {@code nx doctor} shows a note, not a failure.
     */
    static final String WAITING_FOR_BUILDS = "waiting_for_builds";

    /**
     * {@link #WAITING_FOR_BUILDS} once the wait has gone on past {@link BuildWait#OVERDUE_AFTER}: still no failure, no page and no
     * build ended, but a build that long is worth a look, so {@code nx doctor} shows a warning that names the age, the count and the
     * builders' pids.
     */
    static final String WAITING_FOR_BUILDS_OVERDUE = "waiting_for_builds_overdue";

    private static final Pattern DEFERRAL_OVERDUE = Pattern.compile(";\\s*overdue\\s*;");
    private static final Pattern DEFERRAL_RUNNING = Pattern.compile("running=(\\d+)");
    private static final Pattern DEFERRAL_MINUTES = Pattern.compile("deferred_minutes=(\\d+)");
    private static final Pattern DEFERRAL_PIDS = Pattern.compile("pids=([0-9,]+)");

    /** The one name whose "is not installed" is a missing prerequisite: the extension the library install checks first. */
    private static final String PREREQUISITE_EXTENSION = "pg_background";

    /**
     * The class of a blocked reason, or null when it has none: {@link #MISSING_PREREQUISITE} when the reason, behind the retry
     * prefix, is exactly "pg_background is not installed". Null once the reason carries "; needs an operator" (the retry
     * policy adds it from the {@value MoveRetryPolicy#NEEDS_OPERATOR_AT}th consecutive transient, about four hours): a block that
     * long is a failure to look at, so the entry loses its class and the doctor row turns red. Anchored, so run text that
     * mentions the phrase is not taken for the install's reason.
     */
    static String classOf(String reason) {
        if (reason == null || reason.isBlank()) {
            return null;
        }
        if (reason.strip().startsWith(BuildWait.REASON_PREFIX)) {
            return DEFERRAL_OVERDUE.matcher(reason).find() ? WAITING_FOR_BUILDS_OVERDUE : WAITING_FOR_BUILDS;
        }
        String body = reason.strip();
        Matcher retry = RETRY.matcher(body);
        if (retry.find()) {
            body = body.substring(retry.end());
        }
        if (NEEDS_OPERATOR.matcher(body).find()) {
            return null;
        }
        Matcher missing = NOT_INSTALLED.matcher(body);
        return missing.find() && PREREQUISITE_EXTENSION.equals(missing.group(1)) ? MISSING_PREREQUISITE : null;
    }

    /**
     * A library install that failed on Liquibase's checksum validation leaves the libraries FAILED on every boot, caught and
     * serving: a re-pin of the library changed a changeset the install had already applied, and library changesets are not
     * {@code runOnChange}. The reason Liquibase gave names the changeset; this says what happened and what to do, so
     * the operator reads a recovery and not a generic {@code blocked:}. A reason that is not a checksum failure is returned
     * as it came.
     */
    static String withInstallHint(String installReason) {
        if (!isChecksumFailure(installReason)) {
            return installReason;
        }
        Matcher m = CHANGESET.matcher(installReason);
        boolean found = m.find();
        return installReason + " | " + recovery(found ? m.group(2) : null, found ? m.group(1) : null);
    }

    private static boolean isChecksumFailure(String reason) {
        return reason != null && INSTALL_FAILED.matcher(reason.strip()).find()
            && reason.toLowerCase(Locale.ROOT).contains("check sum") && !reason.contains("raised sqlstate=");
    }

    private static String recovery(String id, String file) {
        String changeset = id == null ? "a changeset" : "changeset " + id + (file == null ? "" : " (" + file + ")");
        return "recovery: a library re-pin changed " + changeset + " after the install had applied it "
            + "(library changesets are not runOnChange). Pin the library version the install was made with, or, once the "
            + "change is known to be safe, correct that row's md5sum in the library's tracking table "
            + "(liquibase.ddl_utils_databasechangelog or liquibase.dml_utils_databasechangelog) and restart the engine";
    }

    /**
     * The reason reduced to what {@code boot_work} may carry, since {@code /v1/status} is unauthenticated and names no
     * collection, tenant or index. The worker's own reasons mix three kinds of text: fixed phrases of its own, a run label
     * ({@code <label>/<tenant>[/<leaf>]}) and PostgreSQL's message, which can echo a row. Only the first survives: a
     * recognised phrase is kept (or rebuilt from its safe parts: an SQLSTATE, a role name, a changeset id), a retry count and
     * "needs an operator" are carried over, and anything else becomes a fixed sentence that points at the engine log, where
     * the whole reason is logged. The one backend id it carries is the pids of an overdue {@link BuildWait} deferral (linda 2026-11-24).
     */
    static String publicForm(String reason) {
        if (reason == null || reason.isBlank()) {
            return "stopped for a reason kept in the engine log";
        }
        String text = reason.strip();
        StringBuilder out = new StringBuilder();
        Matcher retry = RETRY.matcher(text);
        String body = text;
        if (retry.find()) {
            out.append("retry ").append(retry.group(1)).append(": ");
            body = text.substring(retry.end());
        }
        // the retry policy appends "; needs an operator" from the 20th retry; the phrases that end their text (an anchored
        // "is not installed") read the reason without it, and it is put back after
        Matcher operator = NEEDS_OPERATOR.matcher(body);
        boolean needsOperator = operator.find();
        if (needsOperator) {
            body = body.substring(0, operator.start());
        }
        boolean indexBuilds = body.contains("; " + MoveRetryPolicy.INDEX_BUILDS_NOTE);
        if (indexBuilds) {   // a fixed note the retry policy adds: kept, and the run's own text is read without it
            body = body.replace("; " + MoveRetryPolicy.INDEX_BUILDS_NOTE, "");
        }
        out.append(phrase(body));
        if (indexBuilds) {
            out.append("; ").append(MoveRetryPolicy.INDEX_BUILDS_NOTE);
        }
        if (needsOperator || text.contains("needs an operator")) {
            out.append("; needs an operator");
        }
        return out.toString();
    }

    /**
     * The public form of a {@link BuildWait} deferral, rebuilt from its numbers: how many builds run, how long the wait has gone on and,
     * past {@link BuildWait#OVERDUE_AFTER}, the builders' pids (linda 2026-11-24: the one backend id {@code /v1/status} carries, so
     * an operator can find the build; a number, nothing a tenant wrote).
     */
    private static String deferral(String text) {
        Matcher running = DEFERRAL_RUNNING.matcher(text);
        Matcher minutes = DEFERRAL_MINUTES.matcher(text);
        StringBuilder out = new StringBuilder("waiting for in-flight index builds to finish");
        if (running.find()) {
            out.append(" (").append(running.group(1)).append(" running)");
        }
        out.append("; none is ended and no failure is counted");
        if (minutes.find()) {
            out.append("; waiting for ").append(duration(Long.parseLong(minutes.group(1))));
        }
        if (DEFERRAL_OVERDUE.matcher(text).find()) {
            out.append("; overdue, more than ").append(BuildWait.OVERDUE_AFTER.toHours()).append(" h");
            Matcher pids = DEFERRAL_PIDS.matcher(text);
            if (pids.find()) {
                out.append("; builder pids ").append(pids.group(1));
            }
        }
        return out.toString();
    }

    private static String duration(long minutes) {
        return minutes >= 60 ? (minutes / 60) + " h " + (minutes % 60) + " m" : minutes + " m";
    }

    private static String phrase(String text) {
        String lower = text.toLowerCase(Locale.ROOT);
        String state = sqlstate(text);
        if (text.startsWith(BuildWait.REASON_PREFIX)) {
            return deferral(text);
        }
        Matcher role = LIBRARY_ROLE.matcher(text);
        Matcher notInstalled = NOT_INSTALLED.matcher(text);
        if (lower.contains("waiting for a background worker slot")) {
            return "waiting for a background worker slot";
        }
        if (role.find()) {
            return "library role " + role.group(1) + " is missing";
        }
        if (notInstalled.find()) {
            return notInstalled.group(1) + " is not installed";
        }
        if (lower.startsWith("the library install has not run")) {
            return "the library install has not run";
        }
        if (lower.startsWith("no separate admin role")) {
            return "the library install was skipped: no separate admin role (NX_DB_ADMIN_* unset)";
        }
        if (isChecksumFailure(text)) {
            Matcher m = CHANGESET.matcher(text);
            boolean found = m.find();
            return "a library changelog failed Liquibase's checksum validation; " + recovery(found ? m.group(2) : null,
                found ? m.group(1) : null);
        }
        if (lower.contains("a library changelog failed")) {
            return "a library changelog failed (see the engine log)";
        }
        if (lower.contains("the install checks could not run")) {
            return "the library install checks could not run (see the engine log)";
        }
        if (lower.startsWith(MovePreflight.SERVING_ROLE + " holds a privilege on nexus.data_moves")) {
            return text;   // names roles and one statement, nothing a tenant wrote
        }
        if (lower.contains("does not read every tenant's rows")) {
            return "the admin session does not read every tenant's rows of the driving table (see the engine log)";
        }
        if (lower.contains("could not grant dml_utils_owner its access")) {
            return "the pass could not grant dml_utils_owner its access to the driving table (see the engine log)";
        }
        if (lower.contains("a preflight check on")) {
            return "a preflight check failed" + state;
        }
        if (lower.contains("raised sqlstate=")) {
            return "a CALL raised an error" + state;
        }
        if (lower.contains("recorded sqlstate=")) {
            return "a chunk recorded an error" + state;
        }
        if (lower.contains("found old-shape rows")) {   // the verification names each tenant's count: those stay in the log
            return "the verification found old-shape rows in the driving table (the counts are in the engine log, "
                + "event=data_move_verification_failed)";
        }
        if (lower.startsWith("the verification of ") && lower.contains("was not run")) {
            return "the verification text was not run (see the engine log)";
        }
        if (lower.contains("the verification connection could not be opened")) {
            return "the verification connection could not be opened" + state;
        }
        if (lower.contains("verification connection carries nexus.tenant")) {
            return "the table-wide verification connection carried a tenant setting, so it was not trusted";
        }
        if (lower.startsWith("adoption timed out at ")) {
            return text;   // the bound in ms, a leaf count and what to do: no collection, tenant or index is named
        }
        if (lower.startsWith("on_complete of ") && lower.contains("did not complete")) {
            return "on_complete did not complete" + state;
        }
        if (lower.contains("the driving table") && lower.contains("does not exist")) {
            return "the driving table does not exist";
        }
        if (lower.contains("the admin connection could not be opened")) {
            return "the admin connection could not be opened (see the engine log)";
        }
        if (lower.contains("returned without completing")) {
            return "a CALL returned without completing its run";
        }
        if (lower.contains("cancelled at")) {
            return "a run was cancelled";
        }
        if (lower.contains("the call connection could not be opened")) {
            return "the call connection could not be opened" + state;
        }
        if (lower.contains("the tenants could not be listed")) {
            return "the tenants of the driving table could not be listed" + state;
        }
        if (lower.contains("the worker is stopped")) {
            return "the worker was stopped";
        }
        if (lower.contains("the pass could not run")) {
            return "the pass could not run (see the engine log)";
        }
        return "stopped for a reason kept in the engine log" + state;
    }

    /** The five-character SQLSTATE a reason carries ({@code sqlstate=57014}), or null: a code, so safe on {@code /v1/status}. */
    static String sqlstateOf(String reason) {
        if (reason == null) {
            return null;
        }
        Matcher m = SQLSTATE.matcher(reason);
        return m.find() ? m.group(1) : null;
    }

    /** {@code " sqlstate=XXXXX"} when the reason carries one, else empty. */
    private static String sqlstate(String text) {
        Matcher m = SQLSTATE.matcher(text);
        return m.find() ? " sqlstate=" + m.group(1) : "";
    }
}
