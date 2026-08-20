// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-asaod — {@code POST /v1/taxonomy/import/topic} maps a row-level-security
 * REFUSAL to a typed 409 instead of the opaque 500 it produced before.
 *
 * <p>WHY THIS BUG EXISTED. The {@code /import/*} verbs preserve CLIENT-SUPPLIED ids
 * verbatim so a fidelity ETL round-trips, and {@code nexus.topics} has a GLOBAL
 * {@code BIGSERIAL} primary key — global because the self-referential parent FK
 * ({@code fk_topics_parent_tenant} since RDR-194 P5a/taxonomy-014-2, formerly
 * {@code topics_parent_fk}) means a composite {@code (tenant_id, id)} PRIMARY KEY
 * would force every {@code parent_id} to carry a tenant as well (RDR-194 D4 instead
 * added a separate {@code UNIQUE (tenant_id, id)} and repointed the FK onto it,
 * leaving this PK unchanged). When a second tenant supplies an id the first already
 * holds, the INSERT is refused by the RLS policy, not by the PK, because RLS is
 * evaluated first: the row exists but is invisible to this tenant.
 *
 * <p>That refusal is SQLSTATE 42501 (insufficient_privilege), NOT class 23 — so
 * {@link HttpUtil#sqlState23} correctly declined it and it fell through to
 * {@code catch (Exception)} → bare 500. Sibling of
 * {@link TaxonomyHandlerAssignFkTest}, which pins the class-23 arm of the same ladder.
 *
 * <p>The 500 was also what made this expensive to diagnose: it looked like a broken
 * route until the tenant identity turned out to be the variable. A 409 makes the class
 * self-describing.
 *
 * <p>Hermetic: Testcontainers PG with the real Liquibase schema (so the real RLS
 * policies are in force) and a NON-BYPASSRLS service role — RLS must actually be able
 * to refuse, or this suite would pass vacuously.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyHandlerImportRlsTest {

    private static final String SVC_ROLE = "svc_tax_import_rls_test";
    private static final String SVC_PASS = "svc_tax_import_rls_test_pass";
    private static final String TENANT_A = "tax-import-rls-tenant-a";
    private static final String TENANT_B = "tax-import-rls-tenant-b";

    /** Client-supplied id both tenants will claim. */
    private static final long CONTESTED_ID = 424_242L;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TaxonomyRepository repo;
    TaxonomyHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + table + " TO " + SVC_ROLE);
            }
            // USAGE alone is NOT enough here, unlike the sibling AssignFkTest:
            // importTopic preserves a client-supplied id by calling
            // setval(pg_get_serial_sequence(...), GREATEST(last_value, ?)) to keep the
            // sequence ahead of imported ids, and setval requires UPDATE on the
            // sequence. Production grants exactly this in
            // taxonomy-005-topics-seq-update-grant.xml (GRANT UPDATE ON SEQUENCE ...
            // TO nexus_svc); this harness runs as its own role, so it must mirror it or
            // every import 500s on "permission denied for sequence topics_id_seq" and
            // the RLS behaviour under test is never reached.
            su.createStatement().execute("GRANT USAGE, SELECT, UPDATE ON SEQUENCE nexus.topics_id_seq TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);
        handler = new TaxonomyHandler(repo, null);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void importTopic_firstTenantClaimingAnId_succeeds() throws Exception {
        CapturingExchange ex = importTopic(CONTESTED_ID, "topic-a");
        handleAs(TENANT_A, ex);
        assertThat(ex.status)
            .as("the first tenant to supply a preserved id: plain success")
            .isEqualTo(200);
    }

    @Test
    void importTopic_secondTenantClaimingSameId_returns409_not500() throws Exception {
        // Ordering: JUnit runs methods in a deterministic (not declaration) order, so
        // claim the id here rather than depending on the test above having run.
        CapturingExchange seed = importTopic(CONTESTED_ID, "topic-a");
        handleAs(TENANT_A, seed);
        assertThat(seed.status).as("precondition: tenant A holds the id").isIn(200, 409);

        CapturingExchange ex = importTopic(CONTESTED_ID, "topic-b");
        handleAs(TENANT_B, ex);

        assertThat(ex.status)
            .as("RLS refusal on a client-supplied id is a caller conflict → 409, not 500")
            .isEqualTo(409);
        assertThat(ex.bodyString()).contains("not available");
    }

    @Test
    void importBatch_secondTenantClaimingSameId_returns409_not500() throws Exception {
        // nexus-asaod review H1: the fix guarded op.startsWith("/import/"), which
        // EXCLUDES "/import_batch" — no slash after "import". The bulk route reaches
        // importTopicsBatch, which does the identical
        // insertInto(TOPICS, TOPICS.ID, ...).onConflict(TOPICS.ID) against the same
        // global BIGSERIAL PK, so it raised the same 42501 and returned the same
        // opaque 500 the fix was cut to remove. It is also the HEAVIER real path (the
        // 190k-row dogfood leg), so the fix was half-applied to the less-used route.
        long id = CONTESTED_ID + 1;
        CapturingExchange seed = importTopicBatch(id, "batch-a");
        handleAs(TENANT_A, seed);
        assertThat(seed.status).as("precondition: tenant A holds the id").isIn(200, 409);

        CapturingExchange ex = importTopicBatch(id, "batch-b");
        handleAs(TENANT_B, ex);

        assertThat(ex.status)
            .as("bulk import must classify an RLS row refusal the same as the single-row route")
            .isEqualTo(409);
        assertThat(ex.bodyString()).contains("not available");
        assertThat(ex.bodyString())
            .as("must not leak that the id belongs to another tenant")
            .doesNotContain(TENANT_A);
    }

    @Test
    void importTopic_conflictBodyDoesNotLeakTheOtherTenant() throws Exception {
        CapturingExchange seed = importTopic(CONTESTED_ID + 1, "leak-probe-a");
        handleAs(TENANT_A, seed);

        CapturingExchange ex = importTopic(CONTESTED_ID + 1, "leak-probe-b");
        handleAs(TENANT_B, ex);
        assertThat(ex.status).isEqualTo(409);

        // Cross-tenant information disclosure guard: the caller learns the id is
        // unusable, never that it belongs to someone or who that someone is. Also no
        // SQL/schema shape, matching the sibling suite's fixed-body discipline.
        String body = ex.bodyString();
        assertThat(body)
            .doesNotContain(TENANT_A)
            .doesNotContain("row-level security")
            .doesNotContain("topics")
            .doesNotContain("insert into");
    }

    @Test
    void importTopic_differentIdInSecondTenant_stillSucceeds() throws Exception {
        // Non-regression: the 409 guard must fire ONLY on the contested id. If it
        // over-fired, cross-tenant imports would break wholesale and this suite would
        // be the only thing saying so.
        CapturingExchange ex = importTopic(CONTESTED_ID + 500, "uncontested");
        handleAs(TENANT_B, ex);
        assertThat(ex.status)
            .as("an id no other tenant holds imports normally")
            .isEqualTo(200);
    }

    @Test
    void genuineGrantFault_stillReturns500_notA409() throws Exception {
        // nexus-5ctw8: the whole fix rests on a DISCRIMINATION — 42501 + the RLS
        // message is a caller conflict (409), while a 42501 from a broken GRANT is
        // a DEPLOYMENT fault that must keep surfacing as 500. Until now that was
        // proven only at the unit level, by handing a synthetic SQLException to
        // HttpUtil.isRlsRowRejection. This drives it end-to-end through the real
        // handler against the real PG.
        //
        // Why it matters: a 42501 mislabelled as 409 tells an operator their
        // deployment is healthy when their role grants are broken — the same
        // false-clean class, wearing an HTTP status code.
        //
        // The revoke is on the SEQUENCE, which /import/topic touches via the
        // BIGSERIAL default. Restored in a finally so ordering cannot leak a
        // broken grant into a sibling test.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "REVOKE INSERT ON nexus.topics FROM " + SVC_ROLE);
        }
        try {
            CapturingExchange ex = importTopic(CONTESTED_ID + 500, "grant-fault");
            handleAs(TENANT_A, ex);

            assertThat(ex.status)
                .as("a genuine grant fault is a DEPLOYMENT error, not a caller conflict")
                .isEqualTo(500);
            assertThat(ex.bodyString())
                .as("must not be dressed up as the caller's id conflict")
                .doesNotContain("not available");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "GRANT INSERT ON nexus.topics TO " + SVC_ROLE);
            }
        }
    }

    @Test
    void rlsDetector_requiresBothTheSqlstateAndTheRlsMessage() {
        // DISCRIMINATION — the reason isRlsRowRejection is not a bare SQLSTATE check.
        // 42501 also means "this role genuinely lacks a privilege", a DEPLOYMENT fault
        // that must keep surfacing as 500 rather than being mislabelled as the caller's
        // conflict. Without this test the fix could be quietly widened to any 42501 and
        // nothing would notice.
        SQLException rlsRefusal = new SQLException(
            "ERROR: new row violates row-level security policy (USING expression) for table \"topics\"",
            "42501");
        SQLException plainPrivilege = new SQLException(
            "ERROR: permission denied for table topics", "42501");
        SQLException integrity = new SQLException("duplicate key value", "23505");

        assertThat(HttpUtil.isRlsRowRejection(new RuntimeException(rlsRefusal)))
            .as("RLS refusal: detected through the wrapper chain")
            .isTrue();
        assertThat(HttpUtil.isRlsRowRejection(new RuntimeException(plainPrivilege)))
            .as("a real privilege fault is NOT a caller conflict — must stay a 500")
            .isFalse();
        assertThat(HttpUtil.isRlsRowRejection(new RuntimeException(integrity)))
            .as("class-23 belongs to the sqlState23 arm, not this one")
            .isFalse();
        assertThat(HttpUtil.isRlsRowRejection(null)).isFalse();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void handleAs(String tenant, CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(
            tenant, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    /**
     * The BULK route (nexus-asaod review, H1). Same client-supplied id, same global
     * BIGSERIAL PK, same {@code onConflict(TOPICS.ID)} in importTopicsBatch — but a
     * different op string, which the original {@code startsWith("/import/")} guard
     * did not match.
     */
    private static CapturingExchange importTopicBatch(long id, String label) {
        String row = "{\"id\":" + id + ",\"label\":\"" + label + "\","
            + "\"parent_id\":null,\"collection\":\"knowledge__rls\","
            + "\"centroid_hash\":null,\"doc_count\":1,"
            + "\"created_at\":\"2026-07-25T00:00:00Z\","
            + "\"review_status\":\"pending\",\"terms\":\"[]\"}";
        String body = "{\"kind\":\"topic\",\"rows\":[" + row + "]}";
        return new CapturingExchange("POST", URI.create("/v1/taxonomy/import_batch"), body);
    }

    private static CapturingExchange importTopic(long id, String label) {
        String body = "{\"id\":" + id + ",\"label\":\"" + label + "\","
            + "\"parent_id\":null,\"collection\":\"knowledge__rls\","
            + "\"centroid_hash\":null,\"doc_count\":1,"
            + "\"created_at\":\"2026-07-25T00:00:00Z\","
            + "\"review_status\":\"pending\",\"terms\":\"[]\"}";
        return new CapturingExchange("POST", URI.create("/v1/taxonomy/import/topic"), body);
    }

    /** Minimal {@link HttpExchange} that captures the response status + body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
