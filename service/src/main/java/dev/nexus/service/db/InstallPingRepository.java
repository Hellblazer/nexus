// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import javax.sql.DataSource;
import java.time.Clock;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.INSTALL_PINGS;

/**
 * nexus-h5olw — writes and counts anonymous install pings.
 *
 * <p>Unlike the tenant repositories this does NOT go through {@link TenantScope}:
 * {@code nexus.install_pings} is a global table with no RLS (see
 * telemetry-014-install-pings.xml), written by an unauthenticated route that
 * has no tenant to stamp. Same posture as {@link TokenStore}.
 */
public final class InstallPingRepository implements InstallPingSink {

    /** One ping as the handler validated it. */
    public record Ping(UUID installId, String clientVersion, String mode,
                       String os, String arch, String python) {}

    private final DataSource dataSource;
    private final Clock clock;

    public InstallPingRepository(DataSource dataSource, Clock clock) {
        this.dataSource = dataSource;
        this.clock = clock;
    }

    private DSLContext dsl() {
        return DSL.using(dataSource, SQLDialect.POSTGRES);
    }

    @Override
    public void record(Ping ping) {
        dsl().insertInto(INSTALL_PINGS)
             .set(INSTALL_PINGS.INSTALL_ID, ping.installId())
             .set(INSTALL_PINGS.CLIENT_VERSION, ping.clientVersion())
             .set(INSTALL_PINGS.MODE, ping.mode())
             .set(INSTALL_PINGS.OS, ping.os())
             .set(INSTALL_PINGS.ARCH, ping.arch())
             .set(INSTALL_PINGS.PYTHON, ping.python())
             .set(INSTALL_PINGS.RECEIVED_AT, OffsetDateTime.now(clock).withOffsetSameInstant(ZoneOffset.UTC))
             .execute();
    }

    /** Distinct install ids seen within the trailing {@code window}. The active-user number. */
    public long activeInstalls(Duration window) {
        OffsetDateTime since = OffsetDateTime.now(clock).minus(window).withOffsetSameInstant(ZoneOffset.UTC);
        Integer n = dsl().select(DSL.countDistinct(INSTALL_PINGS.INSTALL_ID))
                         .from(INSTALL_PINGS)
                         .where(INSTALL_PINGS.RECEIVED_AT.ge(since))
                         .fetchOne(0, Integer.class);
        return n == null ? 0 : n;
    }
}
