// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * Fixture support for tests that drive the garbage-collection paths selected by reapable(c)
 * (RDR-192 Step 8, bead nexus-wbfpw.16): {@code gc_quarantine_orphans} and its bounded twin
 * honour the 30 day grace window on {@code nexus.chunks.last_written_at}, so a fixture that
 * stands for a chunk orphaned long ago has to be aged past it.
 *
 * <p>Only {@code last_written_at} moves. {@code created_at} is write-once and several tests
 * pin its value through the quarantine round trip, so it is left exactly as the fixture wrote it.
 */
public final class ReapableFixtures {

    /** Comfortably past the 30 day default grace window. */
    public static final Duration PAST_GRACE = Duration.ofDays(40);

    private ReapableFixtures() { }

    /** Ages every chunk of {@code (tenant, collection)} past the default grace window. */
    public static void agePastGrace(PostgreSQLContainer<?> pg, String tenant, String collection) {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES)
               .update(CHUNKS)
               .set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minus(PAST_GRACE))
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)))
               .execute();
        } catch (SQLException e) {
            throw new IllegalStateException("could not age chunks of " + tenant + "/" + collection, e);
        }
    }
}
