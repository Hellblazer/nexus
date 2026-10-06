/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-u9zkn gate: a statement bound and its network (read) bound are set in ONE place,
 * {@code PgSession#setLocal}, so they cannot drift. This test fails the build when a main source sets
 * {@code statement_timeout} any other way (a raw {@code set_config("statement_timeout", ...)}, which
 * would bound the statement and leave the read waiting on TCP), or sets a network timeout outside
 * {@code PgSession}, which would be a second place to keep the two in step.
 *
 * <p>Non-vacuity: the scan must see the main tree, must find {@code PgSession}'s own binding, and the
 * patterns are shown to match the shapes they exist to forbid.
 */
class NetworkBoundStatementTimeoutGateTest {

    /** The raw shapes a statement bound can be set in without going through {@code setLocal}. */
    static final Pattern RAW_STATEMENT_TIMEOUT =
        Pattern.compile("DSL\\.(?:val|inline)\\(\\s*\"statement_timeout\"\\s*\\)");

    static final Pattern NETWORK_TIMEOUT_CALL = Pattern.compile("\\.setNetworkTimeout\\(");

    @Test
    void patternsMatchTheShapesTheyForbid() {
        assertThat(RAW_STATEMENT_TIMEOUT.matcher(
            "DSL.function(\"set_config\", String.class, DSL.val(\"statement_timeout\"), DSL.val(\"5000\"))").find())
            .isTrue();
        assertThat(RAW_STATEMENT_TIMEOUT.matcher("DSL.inline( \"statement_timeout\" )").find()).isTrue();
        assertThat(RAW_STATEMENT_TIMEOUT.matcher("PgSession.setLocal(ctx, \"statement_timeout\", v)").find())
            .as("the sanctioned route is not flagged").isFalse();
        assertThat(NETWORK_TIMEOUT_CALL.matcher("conn.setNetworkTimeout(Runnable::run, 0)").find()).isTrue();
    }

    @Test
    void noMainSourceSetsAStatementBoundOrANetworkTimeoutOutsidePgSession() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).as("run from the service module").isDirectory();
        List<String> offenders = new ArrayList<>();
        int scanned = 0;
        boolean pgSessionBinds = false;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path f : (Iterable<Path>) files.filter(p -> p.toString().endsWith(".java"))::iterator) {
                String src = Files.readString(f);
                scanned++;
                boolean isPgSession = f.getFileName().toString().equals("PgSession.java");
                if (isPgSession) {
                    pgSessionBinds = src.contains("\"statement_timeout\".equals(guc)")
                        && src.contains("bindNetworkTimeout(ctx, Long.parseLong(value))");
                    continue;
                }
                if (RAW_STATEMENT_TIMEOUT.matcher(src).find()) {
                    offenders.add(f + ": sets statement_timeout with set_config directly; use PgSession.setLocal"
                        + " so the matching network timeout is bound with it");
                }
                if (NETWORK_TIMEOUT_CALL.matcher(src).find()) {
                    offenders.add(f + ": calls setNetworkTimeout outside PgSession; the per-path read bound is"
                        + " set in PgSession#setLocal only");
                }
            }
        }
        assertThat(scanned).as("the scan saw the main tree").isGreaterThan(100);
        assertThat(pgSessionBinds).as("PgSession#setLocal binds the network timeout for statement_timeout").isTrue();
        assertThat(offenders).isEmpty();
    }
}
