// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

/**
 * House-rule gate (nexus-9gaj7): no column whose name LOOKS like a timestamp
 * lands as {@code text}/{@code character varying} in the applied schema. The
 * engine assumes every timestamp it reads is directly comparable (session
 * UTC, {@code now()}, other timestamptz columns) — a TEXT column with a
 * timestamp-shaped name defeats that at the type-system level: nothing stops
 * a caller writing an unparseable, wrong-zone, or lexicographically-wrong-
 * order value, and every consumer must remember to cast rather than the
 * database enforcing it. This is the sibling of {@link RawSqlGateTest} for
 * SCHEMA shape rather than SOURCE text.
 *
 * <p><b>Ground truth over static analysis, deliberately.</b> A Liquibase
 * changelog file is immutable history (this project's own hot rule: never
 * edit an applied changeset) — a column born TEXT and later converted via
 * {@code ALTER COLUMN ... TYPE timestamptz} in a LATER file still has its
 * original {@code CREATE TABLE ... col TEXT} line sitting in the older file
 * forever. A textual grep across every changelog file cannot distinguish
 * "still TEXT today" from "was TEXT once, fixed three files later" without
 * re-deriving the whole migration history in Java. This gate instead applies
 * the REAL master changelog to a real Postgres ({@link PgContainerHelper})
 * and reads back {@code information_schema.columns} — the one place the
 * question "what type is this column RIGHT NOW" has an unambiguous answer.
 *
 * <p><b>Reduce-only allowlist.</b> {@link #ALLOWLIST} is a snapshot of every
 * timestamp-shaped TEXT/VARCHAR column this gate found on the tree at
 * authoring time (nexus-9gaj7), each with a one-line reason. Two of the
 * three shapes represented are deliberate, not oversights:
 * <ul>
 *   <li>{@code staging.*} landing-zone columns stay TEXT BY DESIGN — the
 *       RDR-180 land-then-transform architecture (staging-001-landing-
 *       tables.xml's own header comment; {@code StagingPromoteOps}' class
 *       javadoc: "staging stays typeless... a malformed staged value fails
 *       LOUD at promote time, not at land time"). Converting these would
 *       silently narrow what a guided migration can land verbatim.</li>
 *   <li>{@code *_by} columns ({@code catalog_links.created_by}) are usernames/
 *       role names, not timestamps — the {@code "created"} substring in the
 *       name-match list below catches them incidentally; they are not a
 *       timezone-reliance bug.</li>
 * </ul>
 * A future entry that is a genuine oversight (not one of the two shapes
 * above) should be fixed via a Liquibase changeset instead of allowlisted.
 * Both directions are checked: a NEW timestamp-shaped TEXT/VARCHAR column
 * not on the list fails loud ({@link #newTimestampShapedTextColumns_failLoud}
 * assertion 1), and a STALE entry — one that no longer appears in the live
 * schema because the column was fixed, renamed, or dropped — also fails loud
 * (assertion 2), so the list cannot silently grow stale in the other
 * direction and must shrink as columns get fixed.
 */
class TimestampColumnTypeGateTest {

    private static final List<String> SCHEMAS = List.of("nexus", "staging", "t1");

    /** Substrings (case-insensitive) that mark a column name as timestamp-shaped. */
    private static final List<String> TIMESTAMP_NAME_TOKENS =
        List.of("_at", "_ts", "timestamp", "created", "updated", "expires");

    private static final Set<String> TEXTUAL_TYPES = Set.of("text", "character varying");

    private static boolean looksLikeTimestamp(String columnName) {
        String lower = columnName.toLowerCase(java.util.Locale.ROOT);
        if (lower.equals("ts")) {
            return true;
        }
        for (String token : TIMESTAMP_NAME_TOKENS) {
            if (lower.contains(token)) {
                return true;
            }
        }
        return false;
    }

    /**
     * {@code schema.table.column -> reason} for every pre-existing offender
     * (nexus-9gaj7 authoring snapshot). REDUCE-ONLY: remove an entry when its
     * column is fixed (converted to timestamptz) or dropped; never add an
     * entry for a NEW column without first confirming it belongs to one of
     * the two deliberate shapes documented on the class above.
     */
    private static final java.util.Map<String, String> ALLOWLIST = java.util.Map.ofEntries(
        // staging.* — land-then-transform typeless-by-design (RDR-180).
        java.util.Map.entry("staging.frecency.embedded_at", "staging typeless-by-design (RDR-180)"),
        java.util.Map.entry("staging.frecency.last_hit_at", "staging typeless-by-design (RDR-180)"),
        java.util.Map.entry("staging.relevance_log.ts", "staging typeless-by-design (RDR-180)"),
        java.util.Map.entry("staging.document_aspects.extracted_at", "staging typeless-by-design (RDR-180)"),
        java.util.Map.entry("staging.aspect_extraction_queue.enqueued_at", "staging typeless-by-design (RDR-180)"),
        java.util.Map.entry("staging.aspect_extraction_queue.last_attempt_at", "staging typeless-by-design (RDR-180)"),
        // *_by — a username/role name, not a timestamp; "created" substring false-positive.
        java.util.Map.entry("nexus.catalog_links.created_by", "false positive: created_by is a username, not a timestamp")
    );

    @Test
    void newTimestampShapedTextColumns_failLoud() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.start()) {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }
            try (Connection su = pg.createConnection("")) {
                var ctx = DSL.using(su, SQLDialect.POSTGRES);
                List<PgCatalogProbes.ColumnRow> rows = PgCatalogProbes.columnsIn(ctx, SCHEMAS);

                Set<String> found = new TreeSet<>();
                for (PgCatalogProbes.ColumnRow row : rows) {
                    if (TEXTUAL_TYPES.contains(row.dataType()) && looksLikeTimestamp(row.column())) {
                        found.add(row.schema() + "." + row.table() + "." + row.column());
                    }
                }

                // Assertion 1: every finding is a KNOWN, reviewed entry. A new hit here
                // means either a genuine timezone-reliance bug (fix it with a Liquibase
                // changeset) or a deliberate staging/naming shape (add it to ALLOWLIST
                // with a reason, per the two documented shapes on the class javadoc).
                Set<String> unreviewed = new TreeSet<>(found);
                unreviewed.removeAll(ALLOWLIST.keySet());
                assertThat(unreviewed)
                    .as("new timestamp-shaped TEXT/VARCHAR column(s) not on the reviewed "
                        + "ALLOWLIST — either convert via a Liquibase changeset (timestamptz) "
                        + "or add a reviewed entry with its reason")
                    .isEmpty();

                // Assertion 2: every allowlist entry is still real. A column that was
                // fixed (converted) or dropped must have its entry REMOVED, not left to
                // rot — this is what makes the allowlist reduce-only rather than a place
                // stale exemptions accumulate forever.
                Set<String> stale = new LinkedHashSet<>(ALLOWLIST.keySet());
                stale.removeAll(found);
                assertThat(stale)
                    .as("stale ALLOWLIST entries — the column no longer matches (fixed or "
                        + "dropped); remove the entry")
                    .isEmpty();
            }
        }
    }
}
