// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.function.Function;

/**
 * The DDL-capable connection values the engine read at boot: {@code NX_DB_ADMIN_URL}/{@code _USER}/{@code _PASS},
 * or the application's own {@code NX_DB_*} values when none of the three is set. Schema migration and the
 * per-collection index builder (RDR-227) connect with the same values.
 *
 * <p>Read once, at boot. A rotated admin password reaches neither until the engine restarts.
 *
 * <p>{@link #toString()} omits the password.
 */
public record AdminConnection(String url, String user, String password) {

    /**
     * Resolve the admin values from {@code env} (a variable name to its value, or null).
     *
     * <p><strong>Partial-config guard</strong>: if any one of the three {@code NX_DB_ADMIN_*} variables is set,
     * all three must be. A partial configuration would silently mix admin and application credentials and fail
     * with a cryptic authentication error at connect time instead of a clear startup failure.
     *
     * @throws IllegalStateException when one or two of the three variables are set
     */
    public static AdminConnection resolve(Function<String, String> env, String defaultUrl, String defaultUser,
                                          String defaultPass) {
        String adminUrl = env.apply("NX_DB_ADMIN_URL");
        String adminUser = env.apply("NX_DB_ADMIN_USER");
        String adminPass = env.apply("NX_DB_ADMIN_PASS");

        long adminSet = (adminUrl != null ? 1 : 0)
                      + (adminUser != null ? 1 : 0)
                      + (adminPass != null ? 1 : 0);
        if (adminSet > 0 && adminSet < 3) {
            throw new IllegalStateException(
                "Partial NX_DB_ADMIN_* configuration detected (" + adminSet + "/3 vars set). " +
                "Set all of NX_DB_ADMIN_URL, NX_DB_ADMIN_USER, NX_DB_ADMIN_PASS, " +
                "or none (to fall back to NX_DB_* credentials).");
        }
        return adminSet == 3
            ? new AdminConnection(adminUrl, adminUser, adminPass)
            : new AdminConnection(defaultUrl, defaultUser, defaultPass);
    }

    @Override
    public String toString() {
        return "AdminConnection[url=" + url + ", user=" + user + ", password=<redacted>]";
    }
}
