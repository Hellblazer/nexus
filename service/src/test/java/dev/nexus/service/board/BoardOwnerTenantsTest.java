// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** RDR-252: the rebuild's tenant source is the token tenants and the tuple tenants, each once. */
class BoardOwnerTenantsTest {

    @Test
    void theUnionKeepsTokenTenantsFirstAndEachTenantOnce() {
        var tenants = new BoardOwnerTenants(() -> List.of("a", "b"), () -> List.of("b", "c"));

        assertThat(tenants.get()).containsExactly("a", "b", "c");
    }

    @Test
    void aTenantOnlyInTupleTenantsIsVisited() {
        assertThat(new BoardOwnerTenants(List::of, () -> List.of("no-token")).get()).containsExactly("no-token");
    }

    @Test
    void eitherReadThatThrowsPropagatesSoTheCensusIsNotFull() {
        assertThatThrownBy(() -> new BoardOwnerTenants(() -> { throw new IllegalStateException("tokens"); }, List::of).get())
            .hasMessage("tokens");
        assertThatThrownBy(() -> new BoardOwnerTenants(List::of, () -> { throw new IllegalStateException("tuples"); }).get())
            .hasMessage("tuples");
    }
}
