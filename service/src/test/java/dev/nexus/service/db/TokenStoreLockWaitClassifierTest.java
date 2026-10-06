// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 (nexus-3wh8d.10 I2): the token insert's retry classifier.
 *
 * <p>The partition creation holds ACCESS EXCLUSIVE on a model partition and then wants
 * ShareRowExclusive on the tables that reference chunks; a writer holding RowExclusive on one of them
 * and needing RowShare on that partition closes a lock cycle. PostgreSQL ends it at
 * {@code deadlock_timeout}, and the token transaction can be the victim (40P01). A victim is a lock
 * wait like any other: it must retry and end as the retryable 503, never as an opaque 500.
 */
class TokenStoreLockWaitClassifierTest {

    @Test
    void theThreeLockWaitStatesAreRetried() {
        assertThat(TokenStore.lockWaitState(new SQLException("lock not available", "55P03"))).isEqualTo("55P03");
        assertThat(TokenStore.lockWaitState(new SQLException("canceling statement", "57014"))).isEqualTo("57014");
        assertThat(TokenStore.lockWaitState(new SQLException("deadlock detected", "40P01"))).isEqualTo("40P01");
    }

    @Test
    void aStateFoundInTheCauseChainIsRetriedToo() {
        var wrapped = new RuntimeException("jOOQ", new RuntimeException("tx", new SQLException("d", "40P01")));
        assertThat(TokenStore.lockWaitState(wrapped)).isEqualTo("40P01");
    }

    @Test
    void otherStatesPropagate() {
        assertThat(TokenStore.lockWaitState(new SQLException("unique", "23505"))).isNull();
        assertThat(TokenStore.lockWaitState(new SQLException("serialization", "40001"))).isNull();
        assertThat(TokenStore.lockWaitState(new RuntimeException("no sql state"))).isNull();
    }
}
