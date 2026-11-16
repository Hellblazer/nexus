// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-7fku2.1.2: the body keys {@link CoverageInvalidation} reads as "the collections this route changed", and the
 * routes it must not hook. A body that names a subset of the real set narrows the drop below it (stale-high, the unsafe
 * direction), so each spelling a listed route accepts is pinned.
 */
class CoverageInvalidationTest {

    @Test
    void aRenameNamesBothCollections_underEachSpellingTheRenameRoutesAccept() {
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("old", "a", "new", "b")))
            .as("TaxonomyHandler#handleRenameCollection, and the catalog rename's short form")
            .containsExactlyInAnyOrder("a", "b");
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("old_name", "a", "new_name", "b")))
            .as("CatalogHandler#handleCollectionRename").containsExactlyInAnyOrder("a", "b");
        assertThat(CoverageInvalidation.collectionsNamedBy(Map.of("old", "a", "new_name", "b")))
            .as("the catalog rename reads each name under either spelling, so a mixed body names both")
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

    /**
     * The two 202 job routes drop in the job's body ({@link CoverageInvalidation#dropAfter}). Listed, their hook would
     * drop at the 202, before the job writes, and the route test's post-job read could not tell that from a drop at the
     * job's end; this pin is what makes that read discriminate.
     */
    @Test
    void theTwoBackgroundJobRoutes_areNotHookedAtTheResponse() {
        assertThat(CatalogHandler.COVERAGE_OPS).doesNotContain("/reconcile/commit", "/manifest/fill");
    }
}
