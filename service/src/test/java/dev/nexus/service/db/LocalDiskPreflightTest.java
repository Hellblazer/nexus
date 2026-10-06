// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.OptionalLong;

/**
 * RDR-225 P1.4 (nexus-3wh8d.9): the production free-space source and the requirement arithmetic. No database:
 * the {@code NX_PG_DATA_DIR} value is the only input, so the managed/remote branch (unset or unseeable) and
 * the local branch are both exercised for real, against the real filesystem.
 */
class LocalDiskPreflightTest {

    @Test
    void unsetOrBlankEnvMeansNoLocalDataDirectory_soTheCheckSkips() {
        assertThat(LocalDiskPreflight.dataDirFreeBytes(null)).isEmpty();
        assertThat(LocalDiskPreflight.dataDirFreeBytes("")).isEmpty();
        assertThat(LocalDiskPreflight.dataDirFreeBytes("   ")).isEmpty();
    }

    @Test
    void aPathTheEngineCannotSeeSkips(@TempDir Path tmp) {
        assertThat(LocalDiskPreflight.dataDirFreeBytes(tmp.resolve("not-there").toString())).isEmpty();
    }

    @Test
    void aRegularFileIsNotADataDirectory(@TempDir Path tmp) throws IOException {
        Path f = Files.writeString(tmp.resolve("afile"), "x");
        assertThat(LocalDiskPreflight.dataDirFreeBytes(f.toString())).isEmpty();
    }

    @Test
    void aVisibleDirectoryReportsItsFilesystemsUsableBytes(@TempDir Path tmp) throws IOException {
        OptionalLong free = LocalDiskPreflight.dataDirFreeBytes(tmp.toString());
        assertThat(free).isPresent();
        long expected = Files.getFileStore(tmp).getUsableSpace();
        // The free count moves between two reads on a live disk; it is the same filesystem, within a slack.
        assertThat(free.getAsLong()).isBetween(Math.max(0, expected - (64L << 20)), expected + (64L << 20));
    }

    @Test
    void requiredBytesIsTwoPointTwoTimesTheTotal_roundedUp() {
        assertThat(LocalDiskPreflight.requiredBytes(0)).isZero();
        assertThat(LocalDiskPreflight.requiredBytes(10)).isEqualTo(22);
        assertThat(LocalDiskPreflight.requiredBytes(1)).isEqualTo(3);     // 2.2 rounds up, never down
        assertThat(LocalDiskPreflight.requiredBytes(100_000_000_000L)).isEqualTo(220_000_000_000L);
    }
}
