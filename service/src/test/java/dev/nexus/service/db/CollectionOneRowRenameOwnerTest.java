// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** RDR-229 P3.7 review S3: which owner a one-row rename may change, without a database. */
class CollectionOneRowRenameOwnerTest {

    @Test
    void aSettledTumblerOwnerIsNeverReplacedByTheClients() {
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("1-2", "slug")).isNull();
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("1", "slug")).isNull();
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("12-3-4", "slug")).isNull();
    }

    @Test
    void aSlugOrAbsentOwnerTakesTheClients_andNothingRequestedChangesNothing() {
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("oldslug", "newslug")).isEqualTo("newslug");
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange(null, "newslug")).isEqualTo("newslug");
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("1-2", null)).isNull();
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("oldslug", null)).isNull();
    }
}
