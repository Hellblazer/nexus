// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;

/**
 * nexus-xtmtf review finding: {@code CatalogRepository.tsOrNull} replaced
 * the {@code ?::timestamptz} cast in the fidelity-import/supersede paths
 * and its javadoc claims the same lenient input shapes — but no test
 * exercised ANY of them, and the date-only shape (which the PG cast
 * accepted as midnight) originally threw uncaught. Pure unit test — no DB.
 */
class CatalogTsOrNullTest {

    @Test
    void blankAndNull_areNull() {
        assertThat(CatalogRepository.tsOrNull(null)).isNull();
        assertThat(CatalogRepository.tsOrNull("")).isNull();
        assertThat(CatalogRepository.tsOrNull("   ")).isNull();
    }

    @Test
    void fullIso_parses() {
        assertThat(CatalogRepository.tsOrNull("2026-05-01T12:30:45.123456+00:00"))
            .isEqualTo(OffsetDateTime.of(2026, 5, 1, 12, 30, 45, 123_456_000, ZoneOffset.UTC));
        assertThat(CatalogRepository.tsOrNull("2026-05-01T12:30:45Z"))
            .isEqualTo(OffsetDateTime.of(2026, 5, 1, 12, 30, 45, 0, ZoneOffset.UTC));
    }

    @Test
    void spaceSeparated_legacySqliteShape_parses() {
        assertThat(CatalogRepository.tsOrNull("2026-05-01 12:30:45+00:00"))
            .isEqualTo(OffsetDateTime.of(2026, 5, 1, 12, 30, 45, 0, ZoneOffset.UTC));
    }

    @Test
    void offsetless_resolvesAsUtc() {
        assertThat(CatalogRepository.tsOrNull("2026-05-01 12:30:45"))
            .isEqualTo(OffsetDateTime.of(2026, 5, 1, 12, 30, 45, 0, ZoneOffset.UTC));
        assertThat(CatalogRepository.tsOrNull("2026-05-01T12:30:45"))
            .isEqualTo(OffsetDateTime.of(2026, 5, 1, 12, 30, 45, 0, ZoneOffset.UTC));
    }

    @Test
    void bareHourOffset_parses() {
        // PG accepts "+00" (no minutes); java.time's lenient offset parser does too.
        assertThat(CatalogRepository.tsOrNull("2026-05-01T12:30:45+00"))
            .isEqualTo(OffsetDateTime.of(2026, 5, 1, 12, 30, 45, 0, ZoneOffset.UTC));
    }

    @Test
    void dateOnly_isMidnightUtc_matchingTheRetiredPgCast() {
        assertThat(CatalogRepository.tsOrNull("2026-05-01"))
            .isEqualTo(OffsetDateTime.of(2026, 5, 1, 0, 0, 0, 0, ZoneOffset.UTC));
    }

    @Test
    void garbage_failsLoud() {
        // The PG cast errored on garbage too — never substitute now().
        assertThatThrownBy(() -> CatalogRepository.tsOrNull("not-a-timestamp"))
            .isInstanceOf(java.time.format.DateTimeParseException.class);
    }
}
