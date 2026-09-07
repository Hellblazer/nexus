// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * The write half of {@link InstallPingRepository}, split out so the HTTP
 * handler's tests run against a recording fake with no Postgres.
 */
@FunctionalInterface
public interface InstallPingSink {
    void record(InstallPingRepository.Ping ping);
}
