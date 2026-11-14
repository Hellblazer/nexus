// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

/**
 * The CHECK constraints of RDR-236 (nexus-6pfgb.42) that an operator may put on the manifest table, one constant each.
 * {@link ManifestCheckAdmin} runs a constant's census, add-NOT-VALID and VALIDATE on the engine's admin connection and
 * {@code ManifestCheckHandler} serves them under {@link #path()}; this class holds everything that differs between the
 * checks: the predicate, the constraint's name, the run-lock key, the route and the words the routes use.
 *
 * <p><b>The predicate</b> ({@link #holds()}) is one {@link Condition}, built once. The CHECK is its rendering
 * ({@link #checkExpression()}); the census counts the rows for which it is FALSE ({@link #violation()}, its negation).
 * One object, so they cannot disagree. A predicate must never be NULL for any row (a NULL passes a CHECK, so the census
 * would count a row the constraint lets through, or the other way about): both predicates below are built from NOT NULL
 * columns or from IS NULL tests, so each is TRUE or FALSE for every row.
 *
 * <p><b>Run-lock keys</b> are session advisory locks, one per constant, each distinct from every other advisory key the
 * engine takes (the bytes of an eight-letter tag as a bigint; {@code ManifestCheckAdminIntegrationBase} compares them
 * with the other keys and {@code EngineSingletonLockSettingsTest} finds them as {@code *_ADVISORY_LOCK_KEY} constants),
 * so a run of one step never holds up another's.
 */
public enum ManifestCheck {

    /**
     * {@code chunk_index = position}. {@code chunk_index} is NOT NULL since hygiene-001, and a BEFORE INSERT trigger
     * fills it from {@code position} when a writer omits it. Wire shape: {@code /v1/admin/manifest-chunk-index-check}.
     */
    CHUNK_INDEX(
        "catalog_document_chunks_chunk_index_position_check",
        ManifestCheck.CHUNK_INDEX_ADVISORY_LOCK_KEY,   // "mfxidxck"
        "manifest-chunk-index-check",
        "manifest chunk-index check",
        "a manifest row whose chunk_index differs from its position",
        "with chunk_index different from position",
        "until chunk_index is set to position",
        DSL.field(DSL.name("chunk_index"), SQLDataType.INTEGER).eq(DSL.field(DSL.name("position"), SQLDataType.INTEGER))),

    /**
     * {@code labels_written OR (section_title IS NULL AND section_type IS NULL AND page_number IS NULL)} (catalog-046):
     * a row whose labels are not its document's own answer holds no label. {@code labels_written} is NOT NULL.
     * Wire shape: {@code /v1/admin/manifest-labels-check}.
     */
    LABELS(
        "catalog_document_chunks_labels_written_check",
        ManifestCheck.LABELS_ADVISORY_LOCK_KEY,   // "mflblsck"
        "manifest-labels-check",
        "manifest labels check",
        "a manifest row with a section label while labels_written is false",
        "with a section label and labels_written false",
        "until labels_written is set true (the labels are then the document's own answer) or the labels are set to NULL "
            + "(the row is legacy again)",
        DSL.condition(DSL.field(DSL.name("labels_written"), SQLDataType.BOOLEAN)).or(
            DSL.field(DSL.name("section_title"), SQLDataType.VARCHAR).isNull()
                .and(DSL.field(DSL.name("section_type"), SQLDataType.VARCHAR).isNull())
                .and(DSL.field(DSL.name("page_number"), SQLDataType.INTEGER).isNull())));

    /** The chunk-index step's run lock; a named constant so the engine's advisory-key census sees it. */
    static final long CHUNK_INDEX_ADVISORY_LOCK_KEY = 0x6d6678696478636bL;
    /** The labels step's run lock; a named constant so the engine's advisory-key census sees it. */
    static final long LABELS_ADVISORY_LOCK_KEY = 0x6d666c626c73636bL;

    private final String constraint;
    private final long lockKey;
    private final String route;
    private final String subject;
    private final String violatorRow;
    private final String violatorsPhrase;
    private final String fixHint;
    private final Condition holds;

    /**
     * @param constraint      the constraint's name
     * @param lockKey         the session advisory lock one run holds
     * @param route           the segment after {@code /v1/admin/} (also the run's label in a lock refusal)
     * @param subject         what the routes call themselves in an error ("the {subject} routes need the root token")
     * @param violatorRow     one row that breaks the CHECK, as an error names it ("a manifest row whose chunk_index
     *                        differs from its position")
     * @param violatorsPhrase "N manifest rows {violatorsPhrase}" in the warning after an add
     * @param fixHint         "they cannot be updated {fixHint}" in that warning
     * @param holds           the CHECK predicate
     */
    ManifestCheck(String constraint, long lockKey, String route, String subject, String violatorRow, String violatorsPhrase,
                  String fixHint, Condition holds) {
        this.constraint = constraint;
        this.lockKey = lockKey;
        this.route = route;
        this.subject = subject;
        this.violatorRow = violatorRow;
        this.violatorsPhrase = violatorsPhrase;
        this.fixHint = fixHint;
        this.holds = holds;
    }

    /** The constraint's name on the manifest table. */
    public String constraint() { return constraint; }

    /** The session advisory lock one run of this step holds. */
    public long lockKey() { return lockKey; }

    /** The route family's path: {@code /v1/admin/<route>}. */
    public String path() { return "/v1/admin/" + route; }

    /** The route segment after {@code /v1/admin/}; it also labels a run in a lock refusal. */
    public String route() { return route; }

    /** The step's log-event prefix: the route with underscores ({@code manifest_chunk_index_check}). */
    public String event() { return route.replace('-', '_'); }

    /** What the routes call themselves in an error message. */
    public String subject() { return subject; }

    /** The application name of the admin session: {@code nexus-<route>}. */
    public String applicationName() { return "nexus-" + route; }

    /** One violating row, as an error names it ("a manifest row whose chunk_index differs from its position"). */
    public String violatorRow() { return violatorRow; }

    /** The warning's count phrase: "N manifest row(s) {phrase}". */
    public String violatorsPhrase() { return violatorsPhrase; }

    /** The warning's fix: "they cannot be updated {hint}". */
    public String fixHint() { return fixHint; }

    // ---- the predicate -------------------------------------------------------------------------------------------

    /** The CHECK predicate. Unqualified names, so the stored definition reads as a column check. */
    Condition holds() {
        return holds;
    }

    /**
     * The rows the CHECK refuses: the predicate is FALSE (it is never NULL, see the class javadoc). Public so the key
     * sweep's {@code manifest_unwritten_label_rows} counts with this one predicate and not a copy.
     */
    public Condition violation() {
        return DSL.not(holds);
    }

    /** The CHECK expression as the server is sent it: the predicate, inlined. */
    String checkExpression() {
        return DSL.using(SQLDialect.POSTGRES).renderInlined(holds);
    }

    /** The {@code format()} template of the ADD; the expression is its one {@code %s}, never concatenated. */
    static String addStatement() {
        return "ALTER TABLE %I.%I ADD CONSTRAINT %I CHECK (%s) NOT VALID";
    }
}
