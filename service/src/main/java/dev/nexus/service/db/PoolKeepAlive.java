/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;

/**
 * {@code tcpKeepAlive} on a connection pool (nexus-u9zkn). Its own class on purpose: {@code Main} calls
 * this while building the pool, BEFORE the boot {@code catch (Throwable)} that logs
 * {@code event=pg_session_env_invalid} and exits 1, and {@link PgSession}'s static initializers parse
 * env ({@code NX_HNSW_EF_SEARCH}, {@code NX_SEARCH_STATEMENT_TIMEOUT_MS},
 * {@code NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS}, ...). A bad value there must surface in that catch, not as
 * an uncaught {@code ExceptionInInitializerError} out of {@code main}, so nothing here may touch
 * {@code PgSession}; {@code PoolKeepAliveTest} pins that from the class file.
 */
public final class PoolKeepAlive {

    private PoolKeepAlive() {
    }

    /**
     * {@code tcpKeepAlive=true} on a pool unless the JDBC URL names it. Not a read bound: the OS default
     * probes after about two hours and only an idle connection. Cheap, and it ends a connection to a host
     * that vanished while idle, so the pool does not hand it out.
     */
    public static void apply(HikariConfig cfg, String jdbcUrl) {
        if (!jdbcUrl.contains("tcpKeepAlive=")) {
            cfg.addDataSourceProperty("tcpKeepAlive", "true");
        }
    }
}
