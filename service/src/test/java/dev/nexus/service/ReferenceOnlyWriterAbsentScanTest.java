// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * RDR-223 Phase 3 (nexus-z0o2p.36): the engine has no reference-only writer. The pin over the real
 * main tree lives in {@link OwnershipGuardCoverageScan#assertTheReferenceOnlyWriterIsAbsentFromMain};
 * the planted-tree tests below show that the scan CAN fail, each by one forged shape, so a green pin
 * on the real tree is not a scan that matches nothing.
 */
class ReferenceOnlyWriterAbsentScanTest {

    @Test
    void theMainTreeHasNoReferenceOnlyWriter() throws IOException {
        OwnershipGuardCoverageScan.assertTheReferenceOnlyWriterIsAbsentFromMain();
    }

    private static List<String> scan(Path dir, String fileName, String body) throws IOException {
        Files.writeString(dir.resolve(fileName), body);
        return OwnershipGuardCoverageScan.problemsOfTheReferenceOnlyPin(dir);
    }

    @Test
    void aMethodWithTheRemovedNameFails(@TempDir Path dir) throws IOException {
        assertThat(scan(dir, "Repo.java", "class Repo { void upsertReferenceOnlyChunk() {} }"))
            .hasSize(1).allMatch(p -> p.contains("upsertReferenceOnlyChunk"));
    }

    @Test
    void aCommentWithTheRemovedNameFails(@TempDir Path dir) throws IOException {
        assertThat(scan(dir, "Handler.java", "class H { /* calls referenceOnlyInsertQuery */ }"))
            .hasSize(1);
    }

    @Test
    void aResourceFileWithTheRemovedNameFails(@TempDir Path dir) throws IOException {
        assertThat(scan(dir, "changelog.xml", "<!-- REFERENCE_ONLY_WRITES_ENABLED -->")).hasSize(1);
    }

    @Test
    void aRetentionWriteOtherThanFullFails(@TempDir Path dir) throws IOException {
        assertThat(scan(dir, "W.java", "class W { void w() { q.set(ch.retention(), \"reference-only\"); } }"))
            .hasSize(1).allMatch(p -> p.contains("retention"));
    }

    @Test
    void aNewUseOfTheRetentionAccessorFails(@TempDir Path dir) throws IOException {
        assertThat(scan(dir, "R.java", "class R { void r() { ctx.select(ch.retention()); } }")).hasSize(1);
    }

    @Test
    void theFullPromotionAndAnUnrelatedFileAreClean(@TempDir Path dir) throws IOException {
        Files.writeString(dir.resolve("Other.java"), "class Other { }");
        assertThat(scan(dir, "Upsert.java", "class U { void u() { q.set(ch.retention(), \"full\"); } }"))
            .isEmpty();
    }
}
