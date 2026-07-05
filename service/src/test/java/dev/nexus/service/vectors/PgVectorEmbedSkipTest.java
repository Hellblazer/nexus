// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import dev.nexus.service.db.TenantScope;

import java.io.PrintWriter;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLFeatureNotSupportedException;
import java.util.List;
import java.util.Set;
import java.util.logging.Logger;
import javax.sql.DataSource;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Hermetic (no Testcontainers) coverage for the RDR-181 existence-partition primitive
 * (bead nexus-f0r8p.1): {@link PgVectorRepository#partitionByExistence} (pure, no DB) and
 * {@link PgVectorRepository#selectExistingChashesOrEmpty}'s SELECT-error fail-safe (a
 * stub {@link DataSource} whose {@code getConnection()} always throws). The real
 * PK-indexed SELECT against a live pgvector table is covered separately by
 * {@code PgVectorRepositoryContractTest} (Testcontainers).
 */
class PgVectorEmbedSkipTest {

    // ---------------------------------------------------------------------------
    // partitionByExistence — pure, no DB dependency.
    // ---------------------------------------------------------------------------

    @Test
    void partitionByExistence_mixedBatch_splitsNeedEmbedVsHaveVector() {
        List<String> chashes = List.of("a", "b", "c", "d");
        Set<String> present = Set.of("b", "d");

        var partition = PgVectorRepository.partitionByExistence(chashes, present);

        assertThat(partition.needEmbedIdx()).as("indices of absent chashes (a=0, c=2)")
            .containsExactly(0, 2);
        assertThat(partition.haveVectorIdx()).as("indices of present chashes (b=1, d=3)")
            .containsExactly(1, 3);
    }

    @Test
    void partitionByExistence_allAbsent_behavesAsToday_allNeedEmbed() {
        List<String> chashes = List.of("a", "b", "c");

        var partition = PgVectorRepository.partitionByExistence(chashes, Set.of());

        assertThat(partition.needEmbedIdx()).as("all-absent batch: every index needs embed")
            .containsExactly(0, 1, 2);
        assertThat(partition.haveVectorIdx()).as("all-absent batch: nothing has a stored vector")
            .isEmpty();
    }

    // ---------------------------------------------------------------------------
    // selectExistingChashesOrEmpty — SELECT-error fail-safe (embeds all, never throws).
    // ---------------------------------------------------------------------------

    @Test
    void selectExistingChashesOrEmpty_selectErrors_failSafeReturnsEmptySet() {
        TenantScope tenantScope = new TenantScope(new AlwaysFailingDataSource());
        PgVectorRepository repo = new PgVectorRepository(tenantScope, new NeverCalledEmbedder(),
                                                          new NeverCalledEmbedder());

        Set<String> present = repo.selectExistingChashesOrEmpty(
            "tenant-a", "code__test__voyage-code-3__v1", List.of("somechash"));

        assertThat(present).as("a SELECT error must fail-safe to embed-all, never throw")
            .isEmpty();
    }

    /** Embedder stub: this test's fail-safe path never reaches the embedder. */
    private static final class NeverCalledEmbedder implements Embedder {
        @Override
        public List<float[]> embed(List<String> texts) {
            throw new UnsupportedOperationException(
                "the SELECT-error fail-safe path must not reach the embedder");
        }
    }

    /** DataSource stub whose every connection attempt fails — forces the SELECT to error. */
    private static final class AlwaysFailingDataSource implements DataSource {
        @Override
        public Connection getConnection() throws SQLException {
            throw new SQLException("stub: connection always fails");
        }

        @Override
        public Connection getConnection(String username, String password) throws SQLException {
            throw new SQLException("stub: connection always fails");
        }

        @Override
        public PrintWriter getLogWriter() {
            throw new UnsupportedOperationException();
        }

        @Override
        public void setLogWriter(PrintWriter out) {
            throw new UnsupportedOperationException();
        }

        @Override
        public void setLoginTimeout(int seconds) {
            throw new UnsupportedOperationException();
        }

        @Override
        public int getLoginTimeout() {
            throw new UnsupportedOperationException();
        }

        @Override
        public Logger getParentLogger() throws SQLFeatureNotSupportedException {
            throw new SQLFeatureNotSupportedException();
        }

        @Override
        public <T> T unwrap(Class<T> iface) throws SQLException {
            throw new SQLException("stub: unwrap not supported");
        }

        @Override
        public boolean isWrapperFor(Class<?> iface) throws SQLException {
            return false;
        }
    }
}
