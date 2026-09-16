/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-r0vkh: the two env-resolved bounds on the taxonomy assign transaction
 * parse the way {@code NX_SEARCH_STATEMENT_TIMEOUT_MS} does, zero refused.
 */
class PgSessionTaxonomyAssignBoundsTest {

    @Test
    void unsetOrBlankEnvYieldsTheDefaults() {
        assertThat(PgSession.boundedTimeoutMs("X", null, 30_000)).isEqualTo(30_000);
        assertThat(PgSession.boundedTimeoutMs("X", "", 5_000)).isEqualTo(5_000);
        assertThat(PgSession.boundedTimeoutMs("X", "   ", 5_000)).isEqualTo(5_000);
    }

    @Test
    void theDefaultsAreTheDocumentedOnes() {
        assertThat(PgSession.DEFAULT_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS)
            .as("sized to the client's 30s per-request timeout").isEqualTo(30_000);
        assertThat(PgSession.DEFAULT_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS).isEqualTo(5_000);
        assertThat(PgSession.startupTaxonomyAssignStatementTimeoutMs()).isBetween(1, PgSession.SEARCH_STATEMENT_TIMEOUT_MAX_MS);
        assertThat(PgSession.startupTaxonomyAssignLockTimeoutMs()).isBetween(1, PgSession.SEARCH_STATEMENT_TIMEOUT_MAX_MS);
    }

    @Test
    void explicitOverrideParses() {
        assertThat(PgSession.boundedTimeoutMs("X", "250", 1)).isEqualTo(250);
        assertThat(PgSession.boundedTimeoutMs("X", " 1 ", 1)).isEqualTo(1);
        assertThat(PgSession.boundedTimeoutMs("X", "600000", 1)).isEqualTo(600_000);
    }

    @Test
    void malformedOverrideFailsLoudNamingTheVariable() {
        assertThatThrownBy(() -> PgSession.boundedTimeoutMs("NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS", "soon", 1))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS");
    }

    @Test
    void zeroIsRefusedBecauseItMeansDisabled() {
        assertThatThrownBy(() -> PgSession.boundedTimeoutMs("NX_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS", "0", 1))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("DISABLE");
        assertThatThrownBy(() -> PgSession.boundedTimeoutMs("X", "-5", 1))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PgSession.boundedTimeoutMs("X", "600001", 1))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
