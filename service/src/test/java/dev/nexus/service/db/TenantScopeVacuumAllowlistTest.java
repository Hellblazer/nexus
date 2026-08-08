package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.io.PrintWriter;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Logger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * nexus-0ys55 — the schema-qualified table allowlist in {@link
 * TenantScope#vacuumAnalyze(List)} must reject any table name not in the fixed
 * purge-vacuum set, mirroring the {@code PERMITTED_GUCS} discipline {@link
 * TenantScopeGucAllowlistTest} pins for {@code withTenant}'s GUC-name argument.
 * VACUUM cannot bind-parameterize a table identifier, so this allowlist is the only
 * durable guard against a future caller passing a request-derived string.
 *
 * <p>Pure unit test, no live DB: the {@link DataSource} stub increments a counter on
 * {@code getConnection()} and otherwise throws, so a rejected table name must throw
 * {@link IllegalArgumentException} BEFORE any connection is borrowed (the counter
 * staying at zero is the non-vacuous proof, same pattern as the GUC allowlist test).
 */
class TenantScopeVacuumAllowlistTest {

    private static final class CountingDataSource implements DataSource {
        final AtomicInteger borrows = new AtomicInteger();

        @Override
        public Connection getConnection() throws SQLException {
            borrows.incrementAndGet();
            throw new SQLException("getConnection must not be reached for a rejected table name");
        }

        @Override public Connection getConnection(String username, String password) throws SQLException {
            return getConnection();
        }
        @Override public PrintWriter getLogWriter() { return null; }
        @Override public void setLogWriter(PrintWriter out) { }
        @Override public void setLoginTimeout(int seconds) { }
        @Override public int getLoginTimeout() { return 0; }
        @Override public Logger getParentLogger() { return Logger.getAnonymousLogger(); }
        @Override public <T> T unwrap(Class<T> iface) { return null; }
        @Override public boolean isWrapperFor(Class<?> iface) { return false; }
    }

    private CountingDataSource ds;
    private TenantScope scope;

    @org.junit.jupiter.api.BeforeEach
    void setUp() {
        ds = new CountingDataSource();
        scope = new TenantScope(ds);
    }

    @Test
    void injectionShapedTableNameIsRejectedBeforeBorrow() {
        String evil = "nexus.chunks_1024; DROP TABLE nexus.catalog_documents; --";
        assertThrows(IllegalArgumentException.class,
                () -> scope.vacuumAnalyze(List.of(evil)));
        assertEquals(0, ds.borrows.get(),
                "rejection must happen before a connection is borrowed");
    }

    @Test
    void unknownButHarmlessTableNameIsRejected() {
        assertThrows(IllegalArgumentException.class,
                () -> scope.vacuumAnalyze(List.of("nexus.not_a_real_table")));
        assertEquals(0, ds.borrows.get());
    }

    @Test
    void oneRejectedNameInAMixedListRejectsTheWholeBatch() {
        assertThrows(IllegalArgumentException.class,
                () -> scope.vacuumAnalyze(List.of("nexus.chunks_1024", "nexus.not_allowed")));
        assertEquals(0, ds.borrows.get(),
                "the allowlist check must run over the WHOLE list before any borrow, "
                + "so a batch containing one bad name never partially executes");
    }

    @Test
    void allFivePurgeVacuumTablesPassValidationAndProceedToBorrow() {
        List<String> allowed = List.of(
            "nexus.chunks_384", "nexus.chunks_768", "nexus.chunks_1024",
            "nexus.catalog_document_chunks", "nexus.catalog_documents");
        // Every name passes the allowlist guard and proceeds to borrow a connection —
        // our stub then throws, surfacing as a wrapped RuntimeException. The borrow
        // counter proving we got PAST the allowlist is the non-vacuous assertion.
        assertThrows(RuntimeException.class, () -> scope.vacuumAnalyze(allowed));
        assertEquals(1, ds.borrows.get(),
                "an all-allowlisted table list must pass validation and borrow a connection");
    }
}
