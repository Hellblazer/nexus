// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-7fku2.1.2: the body keys {@link CoverageInvalidation} reads as "the collections this route changed". A body
 * that names a subset of the real set narrows the drop below it (stale-high, the unsafe direction), so each spelling a
 * listed route accepts is pinned: both names of a rename under either spelling, and the origin of a restore.
 */
class CoverageInvalidationTest {

    @Test
    void aRenameNamesBothCollections_underEitherSpelling() {
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("old", "a", "new", "b")))
            .containsExactlyInAnyOrder("a", "b");
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("old_collection", "a", "new_collection", "b")))
            .containsExactlyInAnyOrder("a", "b");
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("old_collection", "a", "new", "b")))
            .as("the two spellings may be mixed, as TaxonomyHandler#handleRenameCollection accepts")
            .containsExactlyInAnyOrder("a", "b");
    }

    @Test
    void aRestoreNamesItsOrigin_aStoreItsCollection_andAnIdOnlyBodyNamesNone() {
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("origin_collection", "o", "quarantine_collection", "q")))
            .containsExactly("o");
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("collection", "c"))).containsExactly("c");
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("doc_id", "d", "topic_id", 3)))
            .as("none named: the caller then drops the whole tenant").isEmpty();
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("collection", List.of("not", "a", "string"))))
            .as("a non-string value is not a name").isEmpty();
    }
}
