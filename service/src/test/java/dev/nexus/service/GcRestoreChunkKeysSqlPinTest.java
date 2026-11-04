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
 * nexus-6pfgb.40: the SQL of vectors-050 cannot read {@link ChunkMetadataKeys}, so it names the CHUNK keys as
 * literals. This holds those literals equal to the generated set (the RDR-236 key table), in both gc restore
 * functions, so a key-table change that adds a CHUNK key fails here rather than silently dropping the key on a
 * restore. Pure text, no database.
 */
class GcRestoreChunkKeysSqlPinTest {

    private static final String CHANGESET = "db/changelog/vectors-050-gc-restore-chunk-keys-only.xml";

    /** A code line that builds the restored row's metadata; comment lines are skipped by the leading-dashes test. */
    private static final Pattern BUILD = Pattern.compile("^\\s+jsonb_build_object\\((.*)\\),\\s*$", Pattern.MULTILINE);
    private static final Pattern KEY = Pattern.compile("'([a-z_]+)',\\s*(?:encode\\(|c\\.)");

    private static String forwardSql() throws IOException {
        try (var in = GcRestoreChunkKeysSqlPinTest.class.getClassLoader().getResourceAsStream(CHANGESET)) {
            assertThat(in).as("the changeset is on the classpath").isNotNull();
            String xml = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            int rollback = xml.indexOf("<rollback>");
            assertThat(rollback).as("the changeset carries a rollback element").isPositive();
            return xml.substring(0, rollback);
        }
    }

    @Test
    void bothRestoreFunctionsBuildTheRestoredMetadataFromExactlyTheChunkKeys() throws IOException {
        String sql = forwardSql();
        List<String> builds = BUILD.matcher(sql).results().map(m -> m.group(1)).toList();
        assertThat(builds).as("one metadata expression in each of the two restore functions").hasSize(2);
        for (String expr : builds) {
            Set<String> keys = new HashSet<>();
            Matcher k = KEY.matcher(expr);
            while (k.find()) keys.add(k.group(1));
            assertThat(keys).as("the keys built from the columns in: " + expr).isEqualTo(ChunkMetadataKeys.CHUNK);
        }
        assertThat(sql).as("both functions are redefined")
            .contains("nexus.gc_restore_rereferenced(")
            .contains("nexus.gc_restore_rereferenced_bounded(");
    }

    @Test
    void neitherRestoreFunctionCopiesTheQuarantinedMetadataBack() throws IOException {
        // The pre-fix expression started from the quarantined row's metadata and subtracted keys; the fix builds the
        // object from the columns, so no code line may read c.metadata into the INSERT's metadata value.
        String codeOnly = forwardSql().lines().filter(l -> !l.trim().startsWith("--")).reduce("", (a, b) -> a + b + "\n");
        assertThat(codeOnly).doesNotContain("COALESCE(c.metadata, '{}'::jsonb) -");
    }
}
