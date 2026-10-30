// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 2 (bead nexus-wbfpw.4) — Sam's ruling 2026-09-26: the census
 * classification is ONE SQL statement kept in one place, so an operator can run
 * the identical text by hand in psql (no engine tag will carry the route until
 * the rest of RDR-192 ships; until then the production census runs from the
 * standalone script directly). This test pins {@code scripts/sql/manifest_less_census.sql}
 * (the operator-facing copy) and {@link PgVectorRepository#MANIFEST_LESS_CENSUS_SQL}
 * (what the route actually executes) to the SAME statement — a hand edit to either
 * copy without the other fails this test instead of silently drifting.
 *
 * <p>No database needed: this is a pure text comparison, run from the repo root
 * (the working directory {@code mvnw-leased.sh}/surefire use for every module).
 */
class ManifestLessCensusSqlIdentityTest {

    private static final Path STANDALONE_SCRIPT =
        Path.of("..", "scripts", "sql", "manifest_less_census.sql").normalize();

    @Test
    void routeSqlAndStandaloneScript_areTheIdenticalStatement() throws IOException {
        assertThat(STANDALONE_SCRIPT)
            .as("standalone script must exist at %s (repo-root-relative scripts/sql/manifest_less_census.sql)",
                STANDALONE_SCRIPT)
            .exists();

        String fileText = Files.readString(STANDALONE_SCRIPT);

        assertThat(PgVectorRepository.MANIFEST_LESS_CENSUS_SQL)
            .as("PgVectorRepository.MANIFEST_LESS_CENSUS_SQL must be byte-identical to "
                + "scripts/sql/manifest_less_census.sql -- no second copy that can drift")
            .isEqualTo(fileText);
    }

    @Test
    void standaloneScript_namesAllFiveBucketsAndTheBindOrder() throws IOException {
        String fileText = Files.readString(STANDALONE_SCRIPT);

        assertThat(fileText).contains("'superseded'");
        assertThat(fileText).contains("'legacy-unmanifested'");
        assertThat(fileText).contains("'dead-owner'");
        assertThat(fileText).contains("'no-owner'");
        assertThat(fileText).contains("'unclassified'");
        // Exactly six positional binds (round 3, the live_notes/rev_candidates
        // rewrite): tenant_id, collection (live_notes scope), tenant_id, collection
        // (base scope), limit, offset.
        assertThat(fileText.chars().filter(c -> c == '?').count()).isEqualTo(6);
    }
}
