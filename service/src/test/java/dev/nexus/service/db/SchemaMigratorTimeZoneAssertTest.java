// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.util.TimeZone;

/**
 * nexus-9gaj7: {@code SchemaMigrator.pinJvmTimeZoneToUtc()} pins the JVM
 * default zone but a bare {@code TimeZone.setDefault} call has no return
 * signal — nothing previously verified the pin actually took. Pure unit
 * test — no DB — exercising {@code assertJvmTimeZoneIsUtc()} directly so
 * both branches (pass and fail-loud) are covered without depending on
 * platform behavior actually being able to defeat {@code setDefault}.
 *
 * <p>Package-private access mirrors {@code CatalogTsOrNullTest}'s
 * convention for {@code CatalogRepository.tsOrNull}.
 */
class SchemaMigratorTimeZoneAssertTest {

    private static final TimeZone ORIGINAL = TimeZone.getDefault();

    @AfterEach
    void restoreZone() {
        TimeZone.setDefault(ORIGINAL);
    }

    @Test
    void assertJvmTimeZoneIsUtc_passes_whenZoneIsUtc() {
        TimeZone.setDefault(TimeZone.getTimeZone("UTC"));
        assertThatCode(SchemaMigrator::assertJvmTimeZoneIsUtc).doesNotThrowAnyException();
    }

    @Test
    void assertJvmTimeZoneIsUtc_passes_forZeroOffsetNoDstAliases() {
        // Etc/UTC and GMT share UTC's rules (zero offset, no DST) even though
        // their IDs differ from "UTC" — the check compares rules, not ID strings.
        TimeZone.setDefault(TimeZone.getTimeZone("Etc/UTC"));
        assertThatCode(SchemaMigrator::assertJvmTimeZoneIsUtc).doesNotThrowAnyException();

        TimeZone.setDefault(TimeZone.getTimeZone("GMT"));
        assertThatCode(SchemaMigrator::assertJvmTimeZoneIsUtc).doesNotThrowAnyException();
    }

    @Test
    void assertJvmTimeZoneIsUtc_failsLoud_whenZoneIsNotUtc() {
        TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"));
        assertThatThrownBy(SchemaMigrator::assertJvmTimeZoneIsUtc)
            .isInstanceOf(SchemaMigrator.TimeZonePinFailedException.class)
            .hasMessageContaining("America/Los_Angeles")
            .hasMessageContaining("UTC");
    }

    @Test
    void pinJvmTimeZoneToUtc_pinsAndPasses_evenStartingFromAnotherZone() {
        TimeZone.setDefault(TimeZone.getTimeZone("Asia/Tokyo"));
        SchemaMigrator.pinJvmTimeZoneToUtc();
        assertThat(TimeZone.getDefault().getID()).isEqualTo("UTC");
    }
}
