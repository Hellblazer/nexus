package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * nexus-bhzuv — pooler-mode fail-closed assertion + the shipped pgbouncer.ini artifact.
 *
 * <p>Pure-logic tests (no live pooler): the {@code transaction}-mode requirement and the
 * fail-closed handling of session/missing modes. Plus a structural pin on the checked-in
 * {@code service/deploy/pgbouncer.ini} so the version-controlled config can't drift to a
 * session-mode setting unnoticed.
 */
class PoolerModeCheckTest {

    @Test
    void transactionModePasses() {
        assertDoesNotThrow(() -> PoolerModeCheck.assertTransactionMode("transaction"));
    }

    @Test
    void sessionModeFailsClosed() {
        var ex = assertThrows(PoolerModeCheck.PoolerModeException.class,
                () -> PoolerModeCheck.assertTransactionMode("session"));
        assertTrue(ex.getMessage().contains("session"));
    }

    @Test
    void statementAndUnknownModesFailClosed() {
        assertThrows(PoolerModeCheck.PoolerModeException.class,
                () -> PoolerModeCheck.assertTransactionMode("statement"));
        assertThrows(PoolerModeCheck.PoolerModeException.class,
                () -> PoolerModeCheck.assertTransactionMode(null));
        assertThrows(PoolerModeCheck.PoolerModeException.class,
                () -> PoolerModeCheck.assertTransactionMode("  "));
    }

    @Test
    void extractPoolModeReadsTheRow() {
        Map<String, String> cfg = new LinkedHashMap<>();
        cfg.put("max_client_conn", "200");
        cfg.put("pool_mode", "transaction");
        assertEquals("transaction", PoolerModeCheck.extractPoolMode(cfg));
        assertEquals(null, PoolerModeCheck.extractPoolMode(new LinkedHashMap<>()));
        assertEquals(null, PoolerModeCheck.extractPoolMode(null));
    }

    @Test
    void verifyAtStartupNoOpsWhenNoAdminUrl() {
        // The direct-PG (no-pooler) v1 path must not require a pooler probe.
        assertDoesNotThrow(() -> PoolerModeCheck.verifyAtStartup(null, null, null));
        assertDoesNotThrow(() -> PoolerModeCheck.verifyAtStartup("  ", null, null));
    }

    @Test
    void shippedPgbouncerIniPinsTransactionMode() throws Exception {
        // service/ is the test working dir; the artifact lives at service/deploy/pgbouncer.ini.
        Path ini = Path.of("deploy", "pgbouncer.ini");
        assertTrue(Files.isRegularFile(ini),
                "service/deploy/pgbouncer.ini must ship as a version-controlled artifact (nexus-bhzuv)");
        String text = Files.readString(ini);
        assertTrue(text.matches("(?s).*(?m)^\\s*pool_mode\\s*=\\s*transaction\\s*$.*"),
                "pgbouncer.ini must set pool_mode = transaction");
        assertTrue(!text.matches("(?s).*(?m)^\\s*pool_mode\\s*=\\s*session\\s*$.*"),
                "pgbouncer.ini must NOT set pool_mode = session");
    }
}
