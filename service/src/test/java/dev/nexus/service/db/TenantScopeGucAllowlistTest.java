package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.io.PrintWriter;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Logger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * nexus-utnjt — the GUC-name allowlist in {@link TenantScope#withTenant(String, String,
 * java.util.function.Function)} must reject any non-allowlisted GUC name (a future
 * request-derived name is a SQL-injection vector into the session-GUC namespace, since
 * the name is interpolated into {@code set_config(...)} not bound as a parameter).
 *
 * <p>Pure unit test, no live DB: the {@link DataSource} is a stub that increments a
 * counter on {@code getConnection()} and otherwise throws. A rejected GUC name must
 * throw {@link IllegalArgumentException} BEFORE any connection is borrowed, so the
 * counter must stay at zero for every rejection case.
 */
class TenantScopeGucAllowlistTest {

    /** Counts connection borrows; getConnection always throws so a borrow is also a failure. */
    private static final class CountingDataSource implements DataSource {
        final AtomicInteger borrows = new AtomicInteger();

        @Override
        public Connection getConnection() throws SQLException {
            borrows.incrementAndGet();
            throw new SQLException("getConnection must not be reached for a rejected GUC name");
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
    void injectionShapedGucNameIsRejectedBeforeBorrow() {
        String evil = "x', 'y', true); DROP TABLE memory; SELECT set_config('z";
        assertThrows(IllegalArgumentException.class,
                () -> scope.withTenant("tenant-a", evil, dsl -> null));
        assertEquals(0, ds.borrows.get(),
                "rejection must happen before a connection is borrowed");
    }

    @Test
    void unknownButHarmlessGucNameIsRejected() {
        assertThrows(IllegalArgumentException.class,
                () -> scope.withTenant("tenant-a", "nexus.not_a_real_guc", dsl -> null));
        assertEquals(0, ds.borrows.get());
    }

    @Test
    void nullAndBlankGucNamesStillRejected() {
        assertThrows(IllegalArgumentException.class,
                () -> scope.withTenant("tenant-a", null, dsl -> null));
        assertThrows(IllegalArgumentException.class,
                () -> scope.withTenant("tenant-a", "  ", dsl -> null));
        assertEquals(0, ds.borrows.get());
    }

    @Test
    void permittedGucNamesPassValidationAndProceedToBorrow() {
        // An allowlisted name passes the guard and proceeds to borrow a connection —
        // our stub then throws, surfacing as a Runtime.. (wrapped SQLException). The
        // borrow counter proving we got PAST the allowlist is the non-vacuous assertion.
        for (String guc : new String[]{TenantScope.DEFAULT_TENANT_GUC, TenantScope.T1_TENANT_GUC}) {
            ds.borrows.set(0);
            assertThrows(RuntimeException.class,
                    () -> scope.withTenant("tenant-a", guc, dsl -> null));
            assertEquals(1, ds.borrows.get(),
                    "allowlisted GUC '" + guc + "' must pass validation and borrow a connection");
        }
    }

    @Test
    void scratchRepositoryGucIsAMemberOfTheAllowlist() {
        // Single-source-of-truth pin: ScratchRepository's public constant must alias
        // TenantScope's, so it cannot drift out of the allowlist.
        assertSame(TenantScope.T1_TENANT_GUC, ScratchRepository.T1_TENANT_GUC);
    }
}
