// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.RenameRefusedException;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.util.List;
import java.util.Set;

/**
 * RDR-229 P3.7 (linda 2026-11-23, (b) and (c)): the rename readiness is total, cheap and fail closed. A readiness read that THROWS (the list cannot be
 * read) and one that STALLS past {@link CompletionCheck#READINESS_READ_TIMEOUT_MS} (a lock on {@code databasechangelog}) both give
 * {@link Missing#READINESS_UNREADABLE}, never NONE and never an exception to the caller. Before the latch a rename is the name path and the answer is not
 * consulted, so a name-path rename proceeds and nothing refuses with a missing latch row; after the latch the same failure refuses with
 * {@code readiness_unreadable}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37ReadinessFailClosedTest {

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    private static final CompletionCheck.ReadSet EMPTY = new CompletionCheck.ReadSet(List.of(), List.of());

    /** A readiness over {@code result}: the latch as given, the answer the sample's. */
    private static RenameReadiness readinessOver(CompletionCheck.Result result, boolean latched) {
        return new RenameReadiness() {
            @Override
            public Missing missing() {
                return result.readinessMissing();
            }

            @Override
            public boolean gate3Latched() {
                return latched;
            }

            @Override
            public boolean gate3LatchedUnderLock() {
                return latched;
            }

            @Override
            public Missing missingLatched() {
                return result.readinessMissing();
            }
        };
    }

    private void assertFailsClosed(CompletionCheck.Result r) {
        assertThat(r.rename().unreadable()).as("the state is marked unreadable").isTrue();
        assertThat(r.readinessMissing()).as("never NONE from an error").isEqualTo(Missing.READINESS_UNREADABLE);
        assertThat(RenameRefusedException.wireValue(r.readinessMissing())).isEqualTo("readiness_unreadable");

        // before the latch: the rename is the name path, the answer is not consulted, nothing refuses. (The "name-path rename proceeds" half is pinned here
        // against RenameGuard#pinned; it is re-pinned against slice C's runRename at the replay rebase.)
        var before = new RenameGuard(() -> false, readinessOver(r, false));
        assertThat(before.<Boolean>pinned(oneRow -> oneRow)).as("a name-path rename proceeds before the latch").isFalse();
        assertThat(r.renameMissing(false)).as("and /v1/status keeps saying none before the latch").isEqualTo(Missing.NONE);

        // after the latch: refused, naming the unreadable readiness
        var after = new RenameGuard(() -> true, readinessOver(r, true));
        assertThatThrownBy(() -> after.pinned(oneRow -> "ran")).isInstanceOf(RenameRefusedException.class)
            .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(Missing.READINESS_UNREADABLE);
        assertThat(r.renameMissing(true)).isEqualTo(Missing.READINESS_UNREADABLE);
    }

    @Test
    void aReadinessReadThatThrows_failsClosed() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);

            var r = CompletionCheck.evaluate(ctx, EMPTY, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT, () -> {
                throw new IllegalStateException("the P3.5b list cannot be read");
            });

            assertFailsClosed(r);
        }
    }

    @Test
    void aReadinessReadThatStallsPastTheTimeout_failsClosed() throws Exception {
        try (Connection holder = pg.createConnection(""); Connection c = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).lockTable(DSL.table(DSL.name("public", "databasechangelog"))).inAccessExclusiveMode().execute();
            c.setAutoCommit(true);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);

            long start = System.nanoTime();
            var r = CompletionCheck.evaluate(ctx, EMPTY, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT, () -> Set.of("vectors-075-1"));
            Duration took = Duration.ofNanos(System.nanoTime() - start);

            assertThat(took).as("the read was cut by the statement timeout, not left to wait on the lock").isGreaterThanOrEqualTo(Duration.ofMillis(1500))
                .isLessThan(Duration.ofSeconds(20));
            assertFailsClosed(r);
            holder.rollback();
        }
    }

    @Test
    void aReadReadinessIsNotUnreadable_theControl() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            var r = CompletionCheck.evaluate(DSL.using(c, SQLDialect.POSTGRES), EMPTY, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT,
                () -> Set.of("a-changeset-that-never-applied"));
            assertThat(r.rename().unreadable()).isFalse();
            assertThat(r.readinessMissing()).as("read, and not ready: the drop move is registered by nothing here").isEqualTo(Missing.NAME_KEY_DROP_MOVE);
        }
    }
}
