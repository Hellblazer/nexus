// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** RDR-229 P3.7 review S3 and S2b: which owner a one-row rename may change, without a database. */
class CollectionOneRowRenameOwnerTest {

    @Test
    void aSettledTumblerOwnerOfADocumentOwnedCollectionIsNeverReplacedByAClientSlug() {
        for (String type : new String[] {"code", "docs", "rdr"}) {
            assertThat(CollectionOneRowRename.ownerTheRenameMayChange(type, "1-2", "slug")).as(type).isNull();
            assertThat(CollectionOneRowRename.ownerTheRenameMayChange(type, "1", "slug")).as(type).isNull();
            assertThat(CollectionOneRowRename.ownerTheRenameMayChange(type, "12-3-4", "slug")).as(type).isNull();
        }
    }

    @Test
    void anExplicitTumblerShapedRequestIsApplied_asTheNamePathAppliesIt() {
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("code", "1-2", "1-5")).isEqualTo("1-5");
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("code", "slug", "1-5")).isEqualTo("1-5");
    }

    @Test
    void aKnowledgeCollectionWithAnAllDigitOwnerTakesTheClients() {
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("knowledge", "123", "abc")).isEqualTo("abc");
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("knowledge", "1-2", "abc")).isEqualTo("abc");
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("unknown", "7", "abc")).isEqualTo("abc");
    }

    @Test
    void aSlugOrAbsentOwnerTakesTheClients_andNothingRequestedChangesNothing() {
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("code", "oldslug", "newslug")).isEqualTo("newslug");
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("code", null, "newslug")).isEqualTo("newslug");
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("code", "1-2", null)).isNull();
        assertThat(CollectionOneRowRename.ownerTheRenameMayChange("code", "oldslug", null)).isNull();
    }
}
