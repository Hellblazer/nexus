/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-u9zkn -- {@link PoolKeepAlive}: the {@code tcpKeepAlive} pool property, and the boot-order pin that
 * keeps it off {@link PgSession}.
 *
 * <p>{@code Main} applies it while building the pool, before the boot {@code catch (Throwable)} that logs
 * {@code event=pg_session_env_invalid} and exits 1. {@code PgSession}'s static initializers parse env, so
 * if the keep-alive helper touched {@code PgSession} a bad {@code NX_HNSW_EF_SEARCH} (or any other bound)
 * would throw {@code ExceptionInInitializerError} out of {@code main} instead. The proof is from the class
 * file: a class whose constant pool never names {@code PgSession} cannot initialize it.
 */
class PoolKeepAliveTest {

    @Test
    void tcpKeepAliveIsSetUnlessTheUrlNamesIt_andNoSocketTimeoutIsEverSet() {
        var plain = new HikariConfig();
        PoolKeepAlive.apply(plain, "jdbc:postgresql://h:5432/db");
        assertThat(plain.getDataSourceProperties().getProperty("tcpKeepAlive")).isEqualTo("true");
        assertThat(plain.getDataSourceProperties().getProperty("socketTimeout"))
            .as("no pool-wide read bound: several main-pool statements legitimately run for minutes")
            .isNull();

        var named = new HikariConfig();
        PoolKeepAlive.apply(named, "jdbc:postgresql://h:5432/db?tcpKeepAlive=false");
        assertThat(named.getDataSourceProperties().getProperty("tcpKeepAlive")).isNull();
    }

    @Test
    void poolKeepAliveNeverReferencesPgSession_soBuildingAPoolCannotRunItsEnvParsingOutsideTheBootCatch()
            throws IOException {
        String keepAlive = constantPoolText(PoolKeepAlive.class);
        assertThat(keepAlive).as("the read saw PoolKeepAlive's class file").contains("HikariConfig");
        assertThat(keepAlive).as("PoolKeepAlive must not name PgSession (its static init parses env)")
            .doesNotContain("PgSession");

        // Positive control: the same read of PgSession's own class file does find its name, so a read that
        // came back empty or mangled cannot pass the assertion above vacuously.
        assertThat(constantPoolText(PgSession.class)).contains("PgSession");
    }

    private static String constantPoolText(Class<?> c) throws IOException {
        try (var in = c.getResourceAsStream(c.getSimpleName() + ".class")) {
            assertThat(in).as("class file of %s", c).isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.ISO_8859_1);
        }
    }
}
