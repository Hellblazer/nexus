// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.ChunkMetadataKeys;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-l003p: the SQL of vectors-053 cannot read {@link ChunkMetadataKeys}, so it names the CHUNK keys as literals in
 * the INSERT that puts a restored chunk back into its origin. This holds those literals equal to the generated set (the
 * RDR-236 key table), the way {@code GcRestoreChunkKeysSqlPinTest} does for the two gc restores, so a key-table change
 * that adds a CHUNK key fails here rather than silently dropping the key on a restore. It also holds the changeset to
 * the contract the Java integration test drives: the plan is judged BEFORE the move, from the quarantine copy. Pure
 * text, no database.
 */
class QuarantineRestoreChunkKeysSqlPinTest {

    private static final String CHANGESET = "db/changelog/vectors-053-quarantine-restore-chunk-keys-only.xml";

    /** A code line that builds the restored row's metadata; comment lines never match (they start with dashes). */
    private static final Pattern BUILD = Pattern.compile("^\\s+jsonb_build_object\\((.*)\\),\\s*$", Pattern.MULTILINE);
    private static final Pattern KEY = Pattern.compile("'([a-z_]+)',\\s*(?:encode\\(|d\\.)");

    private static String forwardSql() throws IOException {
        try (var in = QuarantineRestoreChunkKeysSqlPinTest.class.getClassLoader().getResourceAsStream(CHANGESET)) {
            assertThat(in).as("the changeset is on the classpath").isNotNull();
            String xml = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            int sql = xml.indexOf("<sql ");
            int rollback = xml.indexOf("<rollback>");
            assertThat(sql).as("the changeset carries a forward sql element").isPositive();
            assertThat(rollback).as("the changeset carries a rollback element").isGreaterThan(sql);
            return xml.substring(sql, rollback);
        }
    }

    private static String codeOnly(String sql) {
        return sql.lines().filter(l -> !l.trim().startsWith("--")).reduce("", (a, b) -> a + b + "\n");
    }

    @Test
    void theRestoredRowIsBuiltFromExactlyTheChunkKeys() throws IOException {
        String sql = forwardSql();
        List<String> builds = BUILD.matcher(sql).results().map(m -> m.group(1)).toList();
        assertThat(builds).as("one metadata expression, in the move's INSERT").hasSize(1);
        Set<String> keys = new HashSet<>();
        Matcher k = KEY.matcher(builds.get(0));
        while (k.find()) keys.add(k.group(1));
        assertThat(keys).as("the keys built from the columns in: " + builds.get(0)).isEqualTo(ChunkMetadataKeys.CHUNK);
        assertThat(sql).as("the function is redefined").contains("nexus.quarantine_restore_chunks(");
    }

    @Test
    void theQuarantinedMetadataIsNeverCopiedBack() throws IOException {
        // The vectors-030-1 expression started from the deleted row's metadata and subtracted the four quarantine keys.
        assertThat(codeOnly(forwardSql())).doesNotContain("COALESCE(d.metadata, '{}'::jsonb)");
    }

    @Test
    void thePlanIsJudgedFromTheQuarantineCopyBeforeTheMoveAndNotAfterIt() throws IOException {
        // After the move the quarantine copy is gone and the restored row holds no document key, so a plan computed
        // after it (p_from_quarantine false) would find no owner. Every plan call in the live path therefore names the
        // quarantine copy; the dry run's call already did.
        String code = codeOnly(forwardSql());
        Pattern call = Pattern.compile("quarantine_reattach_plan\\([^;]*?(true|false)\\)\\s*;", Pattern.DOTALL);
        List<String> flags = call.matcher(code).results().map(m -> m.group(1)).toList();
        assertThat(flags).as("every call of the plan in the forward body").isNotEmpty().allMatch("true"::equals);
        int plan = code.indexOf("v_cand, true)");
        int move = code.indexOf("WITH req AS");
        int write = code.indexOf("v_attachable := v_restored || v_present");
        // indexOf answers -1 for a marker that is not there, and -1 is "less than" everything: pin that all three exist.
        assertThat(plan).as("the live plan call is in the forward body").isGreaterThanOrEqualTo(0);
        assertThat(move).as("THE MOVE statement is in the forward body").isGreaterThanOrEqualTo(0);
        assertThat(write).as("the manifest write is in the forward body").isGreaterThanOrEqualTo(0);
        assertThat(plan).as("the live plan is computed before THE MOVE").isLessThan(move);
        assertThat(write).as("and the manifest rows are written after it").isGreaterThan(move);
    }

    @Test
    void aRivalIsLeftOutOfTheMove_andTheFunctionKeepsItsEightArguments() throws IOException {
        // nexus-l003p: a chunk the plan refuses as rival is not in `req`, so the DELETE never sees it and it stays in
        // quarantine with its keys; and there is no ninth p_rivals parameter (the cross-sibling pre-pass is gone).
        String code = codeOnly(forwardSql());
        int kept = code.indexOf("v_kept := ARRAY(");
        int req = code.indexOf("WITH req AS");
        assertThat(kept).as("the live body computes the kept set").isGreaterThanOrEqualTo(0);
        assertThat(req).isGreaterThanOrEqualTo(0);
        assertThat(code.substring(req, code.indexOf("del AS", req)))
            .as("req excludes the kept chashes").contains("NOT (r.h = ANY (v_kept))");
        assertThat(code).doesNotContain("p_rivals");
        // linda's decision (round 1): the two ending states are kept like a rival, so the "run again" remedy stays true.
        assertThat(code).as("both kept sets (dry run and live) name the three reasons")
            .contains("pl.why IN ('rival', 'indexing', 'failed')");
        assertThat(code.split("pl.why IN \\('rival', 'indexing', 'failed'\\)", -1).length - 1).isEqualTo(2);
        assertThat(code).as("no overload is dropped or granted: CREATE OR REPLACE keeps the signature")
            .doesNotContain("DROP FUNCTION").doesNotContain("GRANT EXECUTE");
    }
}
