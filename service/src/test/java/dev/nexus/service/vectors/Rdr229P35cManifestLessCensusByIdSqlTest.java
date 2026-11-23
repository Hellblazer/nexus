// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;

/**
 * RDR-229 P3.5c: the manifest-less census statement the engine runs from gate 3 is the published one with its four collection
 * predicates on the id columns, and nothing else changed. The published text is pinned byte-identical to the operator's copy
 * elsewhere ({@code ManifestLessCensusSqlIdentityTest}), so the id statement is derived from it and checked here.
 */
class Rdr229P35cManifestLessCensusByIdSqlTest {

    /** A collection predicate on the name column: {@code collection = ?}, {@code collection = c.collection}. */
    private static final Pattern NAME_PREDICATE = Pattern.compile("\\b(?:physical_)?collection\\s*=\\s*(?:\\?|c\\.collection\\b)");

    @Test
    void thePublishedStatementHoldsFourNamePredicatesAndTheIdStatementNone() {
        String published = PgVectorRepository.MANIFEST_LESS_CENSUS_SQL;
        String byId = PgVectorRepository.MANIFEST_LESS_CENSUS_BY_ID_SQL;

        assertThat(NAME_PREDICATE.matcher(published).results().count()).as("name predicates of the published text").isEqualTo(4);
        assertThat(NAME_PREDICATE.matcher(byId).results().count()).as("name predicates left in the id text").isZero();
        assertThat(byId).isNotEqualTo(published);
        assertThat(byId.length() - published.length()).as("four predicates each gain the characters '_id'")
            .isGreaterThanOrEqualTo(4 * "_id".length());
        // only those predicates differ: undoing the four rewrites gives the published text back
        String undone = byId.replace("d2.physical_collection_id = ?", "d2.physical_collection = ?")
            .replace("own_manifest.collection_id = c.collection_id", "own_manifest.collection = c.collection")
            .replace("m.collection_id = c.collection_id", "m.collection = c.collection")
            .replace("c.collection_id = ?", "c.collection = ?");
        assertThat(undone).isEqualTo(published);
    }
}
