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
        assertThat(code.indexOf("v_cand, true)")).as("the live plan is computed before THE MOVE")
            .isLessThan(code.indexOf("WITH req AS"));
        assertThat(code.indexOf("v_attachable := v_restored || v_present")).as("and the manifest rows are written after it")
            .isGreaterThan(code.indexOf("WITH req AS"));
    }
}
