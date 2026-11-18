// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.ExecuteContext;
import org.jooq.ExecuteListener;
import org.jooq.impl.DefaultExecuteListenerProvider;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-209 Phase 0b (nexus-1gdv0.1.1): pins that {@link MemorySearchPage} reads the page AND the total in ONE
 * SELECT, then stamps in a separate UPDATE. That is the whole basis of the "page and total agree under a
 * concurrent write" claim (one statement reads one snapshot, even at READ COMMITTED), and no behaviour test
 * can see it: a second {@code count(*)} statement returns the same numbers on a quiet database.
 *
 * <p>The SQL is recorded with an {@code ExecuteListener} on the transaction's own context. It lives in the
 * {@code db} package because {@link MemorySearchPage#run} is package-private; {@code MemoryRepository.searchPage}
 * is a one-line delegate to it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemorySearchPageStatementsTest {

    private static final String SVC_ROLE = "svc_search_page_stmts";
    private static final String SVC_PASS = "svc_search_page_stmts_pass";
    private static final String TENANT = "search-page-statements";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    MemoryRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        repo = new MemoryRepository(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** Records the SQL of every statement the context runs. */
    private static final class Statements implements ExecuteListener {
        final List<String> sql = new ArrayList<>();

        @Override
        public void executeStart(ExecuteContext ctx) {
            sql.add(ctx.sql().toLowerCase());
        }

        long countingStatements() {
            return sql.stream().filter(s -> s.contains("count(")).count();
        }
    }

    private MemorySearchPage.Page runRecorded(Statements statements, String query, String project,
                                              boolean trackAccess, int limit, int offset) {
        return scope.withTenant(TENANT, ctx -> MemorySearchPage.run(
            ctx.configuration().derive(new DefaultExecuteListenerProvider(statements)).dsl(),
            query, project, trackAccess, limit, offset));
    }

    private String seed(String project, String term, int n) {
        for (int i = 0; i < n; i++) {
            repo.upsert(TENANT, project, "row-" + i, term + " shared body", "t", null, null, 30);
        }
        return project;
    }

    @Test
    void aPage_isOneSelectCarryingRowsAndTotal_thenOneStampUpdate() {
        String proj = seed("stmts-page-" + System.nanoTime(), "narwhalstmts", 8);
        var statements = new Statements();

        var page = runRecorded(statements, "narwhalstmts", proj, true, 3, 0);

        assertThat(page.rows()).hasSize(3);
        assertThat(page.matchingTotal()).isEqualTo(8);
        assertThat(statements.sql).as("one select for rows and total, then the stamp, nothing else")
            .hasSize(2);
        assertThat(statements.sql.get(0)).startsWith("with ").contains("count(").contains("fetch next");
        assertThat(statements.countingStatements()).as("the total is not a separate count statement").isEqualTo(1);
        assertThat(statements.sql.get(1)).startsWith("update ");
    }

    @Test
    void aPageWithoutTracking_isTheOneSelectAlone() {
        String proj = seed("stmts-silent-" + System.nanoTime(), "walrusstmts", 4);
        var statements = new Statements();

        var page = runRecorded(statements, "walrusstmts", proj, false, 2, 0);

        assertThat(page.matchingTotal()).isEqualTo(4);
        assertThat(statements.sql).hasSize(1);
        assertThat(statements.countingStatements()).isEqualTo(1);
    }

    @Test
    void aPagePastTheEnd_isTheOneSelectAlone_noStamp_noGuard() {
        String proj = seed("stmts-past-" + System.nanoTime(), "manatestmts", 4);
        var statements = new Statements();

        var page = runRecorded(statements, "manatestmts", proj, true, 2, 100);

        assertThat(page.rows()).isEmpty();
        assertThat(page.matchingTotal()).isEqualTo(4);
        assertThat(statements.sql).as("the total rides the single select; no stamp, no guard probe").hasSize(1);
    }
}
