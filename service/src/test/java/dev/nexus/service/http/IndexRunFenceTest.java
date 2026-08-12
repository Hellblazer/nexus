// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
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
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-5xn3k.2 (RUNFENCE E2, engine half step 2 of 2) — TDD suite for
 * {@code CatalogRepository.beginIndexRun/completeIndexRun/failIndexRun}, the
 * five new {@code CatalogHandler} routes, {@code write_manifest_many}'s
 * optional {@code complete} map, and {@code /v1/vectors/update-metadata}'s
 * {@code missing} ids report.
 *
 * <p>Design of record: T2 nexus memory {@code 5xn3k-design-2026-08-02} §3.3 +
 * §3.5. Schema + {@code nexus.manifest_verify}/{@code manifest_verify_all}
 * landed in step 1 (nexus-5xn3k.1, catalog-020); the CHECK constraint
 * (catalog-021) is new in this bead, per the substantive-critic HARD spec
 * amendment on .1 (T2 21350): {@code /complete} must refuse on EITHER
 * {@code missing > 0} OR {@code referenced != chunkCount} — {@code missing}
 * alone is fail-OPEN for the all-manifest-writes-failed case.
 *
 * <p>Hermetic: Testcontainers PG, full master changelog, the real
 * {@code nexus_svc} role (mirrors {@link ManifestVerifyTest} — the grants
 * this bead depends on, including catalog-020-5's EXECUTE on
 * {@code manifest_verify}/{@code manifest_verify_all}, are already wired to
 * that role by the master changelog rather than a hand-rolled test role).
 * Drives {@link CatalogHandler#handle} and {@link VectorHandler#handle}
 * directly via a capturing {@link HttpExchange} (same pattern as
 * {@code CatalogHandlerUpdateManyDeleteManyTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IndexRunFenceTest {

    private static final String TENANT = "irf-tenant-a";
    private static final String COLLECTION = "knowledge__irf-owner__voyage-context-3__v1";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler catalogHandler;
    PgVectorRepository vecRepo;
    VectorHandler vectorHandler;
    com.zaxxer.hikari.HikariDataSource svcDs;

    // ══════════════════════════════════════════════════════════════════════════
    // LIFECYCLE
    // ══════════════════════════════════════════════════════════════════════════

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Full master changelog: role-001 creates nexus_svc; catalog-020 (nexus-5xn3k.1)
        // adds the fence columns/index + manifest_verify/manifest_verify_all;
        // catalog-021 (THIS bead) adds the index_state CHECK constraint;
        // grants-nexus-svc (runAlways, last) grants nexus_svc DML on every table
        // it owns, including catalog-020-5's EXECUTE on the two SQL functions.
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        catalogHandler = new CatalogHandler(repo);
        vecRepo = new PgVectorRepository(tenantScope, (dev.nexus.service.vectors.Embedder) null,
                                          (dev.nexus.service.vectors.Embedder) null);
        vectorHandler = new VectorHandler(null, vecRepo);

        repo.upsertCollection(TENANT, Map.of("name", COLLECTION, "content_type", "knowledge"));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // migration conformance — catalog-021 actually landed
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void catalog021Changeset_recordedInDatabaseChangeLog() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM databasechangelog WHERE id LIKE 'catalog-021-%'");
            rs.next();
            assertThat(rs.getLong(1))
                .as("the catalog-021 CHECK-constraint changeset must be recorded as applied")
                .isEqualTo(1L);
        }
    }

    @Test
    void checkConstraint_rejectsTypoIndexState() throws Exception {
        String docId = "irf-check-doc-1";
        registerDoc(docId);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThatThrownBy(() -> su.createStatement().execute(
                    "UPDATE nexus.catalog_documents SET index_state = 'bogus-typo' " +
                    "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + docId + "'"))
                .as("a typo state must be rejected by the CHECK constraint, not silently stored")
                .isInstanceOf(SQLException.class);
        }
    }

    @Test
    void checkConstraint_allowsNullAndAllThreeValidStates() throws Exception {
        String docId = "irf-check-doc-2";
        registerDoc(docId);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String state : new String[]{"indexing", "complete", "failed"}) {
                su.createStatement().execute(
                    "UPDATE nexus.catalog_documents SET index_state = '" + state + "' " +
                    "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + docId + "'");
            }
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET index_state = NULL " +
                "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + docId + "'");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // /index-run/begin — idempotent, NOT a lock
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void begin_idempotent_secondCallSucceedsAndStateStaysIndexing() throws Exception {
        String docId = "irf-begin-doc-1";
        registerDoc(docId);

        var ex1 = postCatalog("/v1/catalog/index-run/begin",
            "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"h1\",\"run_id\":\"run-1\",\"collection\":\""
                + COLLECTION + "\"}");
        handleCatalog(ex1);
        assertThat(ex1.status).isEqualTo(200);

        // Fence is NOT a lock (nexus-lcmbp non-goal): a second /begin against an
        // already-'indexing' doc must succeed, not refuse/skip.
        var ex2 = postCatalog("/v1/catalog/index-run/begin",
            "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"h2\",\"run_id\":\"run-2\",\"collection\":\""
                + COLLECTION + "\"}");
        handleCatalog(ex2);
        assertThat(ex2.status).isEqualTo(200);

        var doc = repo.getDocument(TENANT, docId);
        assertThat(doc.get("index_state")).isEqualTo("indexing");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // /index-run/complete — the fail-closed gate
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void complete_success_stampsStateHashCountAtomically() throws Exception {
        String docId = "irf-complete-ok-doc-1";
        registerDoc(docId);
        String chash = writeOneRowManifestWithMatchingChunk(docId, "irf-complete-ok-chash");

        beginViaHttp(docId, "content-hash-x", "run-ok-1");

        var ex = postCatalog("/v1/catalog/index-run/complete",
            "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"content-hash-x\",\"chunk_count\":1}");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"missing\":0");

        var doc = repo.getDocument(TENANT, docId);
        assertThat(doc.get("index_state")).isEqualTo("complete");
        assertThat(doc.get("index_content_hash")).isEqualTo("content-hash-x");
        assertThat(((Number) doc.get("chunk_count")).intValue()).isEqualTo(1);
    }

    @Test
    void complete_refuses409_whenMissingGreaterThanZero_leavesIndexingUntouched() throws Exception {
        String docId = "irf-complete-missing-doc-1";
        registerDoc(docId);
        // Manifest row with NO matching chunks_1024 row -> missing = 1.
        repo.writeManifest(TENANT, docId, COLLECTION, List.of(
            Map.<String, Object>of("position", 0, "chash", chash("irf-missing-chunk"), "chunk_index", 0)));

        beginViaHttp(docId, "h", "run-missing-1");

        var ex = postCatalog("/v1/catalog/index-run/complete",
            "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"h\",\"chunk_count\":1}");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"missing\":1");

        var doc = repo.getDocument(TENANT, docId);
        assertThat(doc.get("index_state"))
            .as("a refused /complete must leave index_state EXACTLY as /begin left it")
            .isEqualTo("indexing");
    }

    /**
     * nexus-c8hl7: {@code completeIndexRun}'s refusal path logged nothing
     * engine-side prior to this bead — {@code stampCompleteIfVerified}
     * (write_manifest_many's ride) logged its refusal, this fence's own
     * primary route did not, and CliRunner swallows client-side structlog
     * for the identical event, so an intermittent completion refusal left
     * zero evidence anywhere. Pin the new WARN: event name, tenant, doc_id,
     * the collection manifest_verify actually checked (the STAMPED
     * physical_collection), the counts, and a sample containing the
     * specific missing chash.
     */
    @Test
    void complete_refuses409_logsWarnWithCollectionAndMissingChashSample() throws Exception {
        String docId = "irf-complete-missing-logged-doc-1";
        registerDoc(docId);
        String missingChash = chash("irf-missing-logged-chunk");
        // Manifest row with NO matching chunks_1024 row -> missing = 1.
        repo.writeManifest(TENANT, docId, COLLECTION, List.of(
            Map.<String, Object>of("position", 0, "chash", missingChash, "chunk_index", 0)));

        beginViaHttp(docId, "h", "run-missing-logged-1");

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            var ex = postCatalog("/v1/catalog/index-run/complete",
                "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"h\",\"chunk_count\":1}");
            handleCatalog(ex);
            assertThat(ex.status).as(ex.bodyString()).isEqualTo(409);

            var messages = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .toList();
            assertThat(messages)
                .as("a refused /complete must WARN engine-side, naming the collection checked, "
                    + "the counts, and a sample of the missing chash(es)")
                .anyMatch(m -> m.startsWith("event=complete_index_run_refused")
                    && m.contains("doc_id=" + docId)
                    && m.contains("collection=" + COLLECTION)
                    && m.contains("referenced=1")
                    && m.contains("present=0")
                    && m.contains("missing=1")
                    && m.contains("claimed_chunk_count=1")
                    && m.contains(missingChash));
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    @Test
    void complete_refuses409_whenReferencedLowerThanClaimedChunkCount_zeroContentCase() throws Exception {
        // The critic's spec amendment case (T2 21350): a run whose manifest writes
        // ALL failed while chunks landed in T3 yields referenced=0/missing=0 — an
        // empty manifest is NOT damage by manifest_verify's own contract, so
        // missing>0 alone would fail to catch this. referenced(0) != claimed
        // chunk_count(5) must ALSO refuse.
        String docId = "irf-complete-zerocontent-doc-1";
        registerDoc(docId);
        // No manifest rows written at all -> referenced = 0.

        beginViaHttp(docId, "h", "run-zero-1");

        var ex = postCatalog("/v1/catalog/index-run/complete",
            "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"h\",\"chunk_count\":5}");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"referenced\":0").contains("\"missing\":0");

        var doc = repo.getDocument(TENANT, docId);
        assertThat(doc.get("index_state")).isEqualTo("indexing");
    }

    /**
     * nexus-c8hl7 companion case: the zero-content refusal (missing==0,
     * referenced != claimed chunk_count) must ALSO WARN — the sample-query
     * guard ({@code counts.missing() > 0}) must not accidentally suppress
     * the whole log line for this refusal shape, only the (here, correctly
     * empty) chash sample.
     */
    @Test
    void complete_refuses409_zeroContentCase_logsWarnWithEmptyMissingSample() throws Exception {
        String docId = "irf-complete-zerocontent-logged-doc-1";
        registerDoc(docId);
        // No manifest rows written at all -> referenced = 0, missing = 0.

        beginViaHttp(docId, "h", "run-zero-logged-1");

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            var ex = postCatalog("/v1/catalog/index-run/complete",
                "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"h\",\"chunk_count\":5}");
            handleCatalog(ex);
            assertThat(ex.status).as(ex.bodyString()).isEqualTo(409);

            var messages = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .toList();
            assertThat(messages)
                .as("the referenced!=chunk_count refusal shape must WARN too, with an empty "
                    + "(not omitted) missing-chash sample since missing==0")
                .anyMatch(m -> m.startsWith("event=complete_index_run_refused")
                    && m.contains("doc_id=" + docId)
                    && m.contains("referenced=0")
                    && m.contains("present=0")
                    && m.contains("missing=0")
                    && m.contains("claimed_chunk_count=5")
                    && m.contains("missing_chash_sample=[]"));
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    @Test
    void complete_withoutPriorBegin_acceptedButFlagged() throws Exception {
        String docId = "irf-complete-noflag-doc-1";
        registerDoc(docId);
        writeOneRowManifestWithMatchingChunk(docId, "irf-noflag-chash");
        // Deliberately NO /begin call — index_state is still NULL (unknown).

        var ex = postCatalog("/v1/catalog/index-run/complete",
            "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"h\",\"chunk_count\":1}");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        assertThat(ex.bodyString())
            .as("a complete with no prior begin must be ACCEPTED but FLAGGED, not refused")
            .contains("\"flagged\":true");

        var doc = repo.getDocument(TENANT, docId);
        assertThat(doc.get("index_state")).isEqualTo("complete");
    }

    @Test
    void complete_afterBegin_isNotFlagged() throws Exception {
        String docId = "irf-complete-flag-false-doc-1";
        registerDoc(docId);
        writeOneRowManifestWithMatchingChunk(docId, "irf-flagfalse-chash");
        beginViaHttp(docId, "h", "run-flagfalse-1");

        var ex = postCatalog("/v1/catalog/index-run/complete",
            "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"h\",\"chunk_count\":1}");
        handleCatalog(ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"flagged\":false");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // /index-run/fail
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void fail_stampsFailedState() throws Exception {
        String docId = "irf-fail-doc-1";
        registerDoc(docId);
        beginViaHttp(docId, "h", "run-fail-1");

        var ex = postCatalog("/v1/catalog/index-run/fail",
            "{\"doc_id\":\"" + docId + "\",\"error\":\"MinerU OOM at page 49\"}");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);

        var doc = repo.getDocument(TENANT, docId);
        assertThat(doc.get("index_state")).isEqualTo("failed");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // stacked-review item 3: identity-mismatch (unknown doc_id) leaves a signal
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * The long-standing contract for an unknown tumbler stays a silent NO-OP
     * (never a thrown exception, per {@code writeManifestRows}'s own
     * precedent) — but the critic's point is that a 0-row update on a doc_id
     * that was never registered (as opposed to one that IS registered and
     * merely tombstoned) is an identity-mismatch class bug signature that
     * must not be INVISIBLE. Assert both halves: the call still succeeds
     * (no exception, no 500) AND a WARN fires naming the event.
     */
    @Test
    void beginCompleteFail_unknownDocId_staysNoOpButLogsWarn() throws Exception {
        String unknownDocId = "irf-unknown-doc-never-registered";

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            var beginEx = postCatalog("/v1/catalog/index-run/begin",
                "{\"doc_id\":\"" + unknownDocId + "\",\"content_hash\":\"h\",\"run_id\":\"r\",\"collection\":\""
                    + COLLECTION + "\"}");
            handleCatalog(beginEx);
            assertThat(beginEx.status).as(beginEx.bodyString()).isEqualTo(200);

            var failEx = postCatalog("/v1/catalog/index-run/fail",
                "{\"doc_id\":\"" + unknownDocId + "\",\"error\":\"n/a\"}");
            handleCatalog(failEx);
            assertThat(failEx.status).as(failEx.bodyString()).isEqualTo(200);

            var messages = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .toList();
            assertThat(messages)
                .as("begin on an unregistered doc_id must WARN the identity-mismatch, not stay silent")
                .anyMatch(m -> m.startsWith("event=index_run_begin_unknown_doc") && m.contains(unknownDocId));
            assertThat(messages)
                .as("fail on an unregistered doc_id must WARN the identity-mismatch, not stay silent")
                .anyMatch(m -> m.startsWith("event=index_run_fail_unknown_doc") && m.contains(unknownDocId));
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }

        assertThat(repo.getDocument(TENANT, unknownDocId))
            .as("an unknown doc_id must never be silently registered as a side effect")
            .isNull();
    }

    @Test
    void complete_unknownDocId_staysNoOpButLogsWarn_andDoesNotThrow() throws Exception {
        // completeIndexRun's own verify-then-stamp runs manifest_verify() first,
        // which returns (0,0,0) for a doc_id with no manifest rows regardless of
        // whether the doc is registered — so a chunk_count of 0 passes the
        // fail-closed gate (referenced=0 == chunk_count=0) and reaches the
        // UPDATE, which is the 0-row/unknown-doc branch under test here.
        String unknownDocId = "irf-unknown-doc-complete-1";

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            var ex = postCatalog("/v1/catalog/index-run/complete",
                "{\"doc_id\":\"" + unknownDocId + "\",\"content_hash\":\"h\",\"chunk_count\":0}");
            handleCatalog(ex);
            assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);

            var messages = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .toList();
            assertThat(messages)
                .as("complete on an unregistered doc_id must WARN the identity-mismatch")
                .anyMatch(m -> m.startsWith("event=index_run_complete_unknown_doc") && m.contains(unknownDocId));
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // manifest/verify + manifest/verify_all route wiring (logic already covered
    // by ManifestVerifyTest — this is the HTTP-layer smoke test)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void manifestVerify_httpRoute_returnsCounts() throws Exception {
        String docId = "irf-verify-route-doc-1";
        registerDoc(docId);
        writeOneRowManifestWithMatchingChunk(docId, "irf-verify-route-chash");

        var ex = getCatalog("/v1/catalog/manifest/verify?doc_id=" + docId);
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"referenced\":1").contains("\"missing\":0");
    }

    @Test
    void manifestVerifyAll_httpRoute_returnsPerCollectionAggregates() throws Exception {
        String docId = "irf-verifyall-route-doc-1";
        registerDoc(docId);
        writeOneRowManifestWithMatchingChunk(docId, "irf-verifyall-route-chash");

        var ex = getCatalog("/v1/catalog/manifest/verify_all");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"collection\"");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // write_manifest_many's optional "complete" map
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void writeManifestMany_completeMap_stampsCompletionInSameTransaction() throws Exception {
        String docId = "irf-wmm-complete-doc-1";
        registerDoc(docId);
        String chash = chash("irf-wmm-complete-chash");
        insertChunk1024(chash);

        Map<String, Object> doc = Map.of(
            "doc_id", docId,
            "rows", List.of(Map.of("position", 0, "chash", chash, "chunk_index", 0)));
        var result = repo.writeManifestMany(TENANT, List.of(doc), COLLECTION, Map.of(docId, "wmm-content-hash"));
        assertThat(result.get("docs")).isEqualTo(1);
        assertThat((List<?>) result.get("complete_refused")).isEmpty();
        assertThat(result.get("complete_refused_count")).isEqualTo(0);

        var got = repo.getDocument(TENANT, docId);
        assertThat(got.get("index_state")).isEqualTo("complete");
        assertThat(got.get("index_content_hash")).isEqualTo("wmm-content-hash");
    }

    @Test
    void writeManifestMany_completeMap_refusedWhenMissing_reportsCompleteRefused() throws Exception {
        String docId = "irf-wmm-refused-doc-1";
        registerDoc(docId);
        String chash = chash("irf-wmm-refused-chash");
        // NO matching chunks_1024 row inserted -> missing = 1.

        Map<String, Object> doc = Map.of(
            "doc_id", docId,
            "rows", List.of(Map.of("position", 0, "chash", chash, "chunk_index", 0)));
        var result = repo.writeManifestMany(TENANT, List.of(doc), COLLECTION, Map.of(docId, "wmm-refused-hash"));
        assertThat(result.get("docs"))
            .as("the manifest write itself must still succeed — over-work, never under-work")
            .isEqualTo(1);
        List<?> refused = (List<?>) result.get("complete_refused");
        assertThat(refused).hasSize(1);
        assertThat(result.get("complete_refused_count"))
            .as("scalar sibling to the list (stacked-review item 2) — must not be silently ignorable")
            .isEqualTo(1);

        var got = repo.getDocument(TENANT, docId);
        assertThat(got.get("index_state"))
            .as("a refused completion stamp must not mark the doc complete")
            .isNotEqualTo("complete");
    }

    @Test
    void writeManifestMany_httpRoute_acceptsCompleteField() throws Exception {
        String docId = "irf-wmm-http-doc-1";
        registerDoc(docId);
        String chash = chash("irf-wmm-http-chash");
        insertChunk1024(chash);

        var ex = postCatalog("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + COLLECTION + "\",\"docs\":[{\"doc_id\":\"" + docId + "\",\"rows\":[{\"position\":0,\"chash\":\""
                + chash + "\",\"chunk_index\":0}]}],\"complete\":{\"" + docId + "\":\"http-wmm-hash\"}}");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);

        var got = repo.getDocument(TENANT, docId);
        assertThat(got.get("index_state")).isEqualTo("complete");
        assertThat(got.get("index_content_hash")).isEqualTo("http-wmm-hash");
    }

    /**
     * Stacked-review item 5: the repo-level refusal test
     * ({@code writeManifestMany_completeMap_refusedWhenMissing_reportsCompleteRefused})
     * calls {@link CatalogRepository#writeManifestMany} directly — this is the
     * SAME scenario driven through the actual wire route, so a regression in
     * {@code handleManifestWriteMany}'s body/JSON-response plumbing (as opposed
     * to the repo method itself) would be caught here.
     */
    @Test
    void writeManifestMany_httpRoute_refusedCompletion_leavesManifestWrittenAndStateUnchanged() throws Exception {
        String docId = "irf-wmm-http-refused-doc-1";
        registerDoc(docId);
        String chash = chash("irf-wmm-http-refused-chash");
        // Deliberately NO matching chunks_1024 row -> missing = 1 -> refused.

        var ex = postCatalog("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + COLLECTION + "\",\"docs\":[{\"doc_id\":\"" + docId + "\",\"rows\":[{\"position\":0,\"chash\":\""
                + chash + "\",\"chunk_index\":0}]}],\"complete\":{\"" + docId + "\":\"http-refused-hash\"}}");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        assertThat(ex.bodyString())
            .as("the manifest write succeeds (200) even though the completion stamp is refused")
            .contains("\"docs\":1")
            .contains("\"complete_refused_count\":1");

        var got = repo.getDocument(TENANT, docId);
        assertThat(got.get("index_state"))
            .as("a refused completion via the HTTP route must not stamp 'complete'")
            .isNotEqualTo("complete");
        // The manifest row itself WAS written (over-work-never-under-work).
        assertThat(repo.getManifest(TENANT, docId)).hasSize(1);
    }

    /**
     * Stacked-review item 4: a JSON {@code null} value for a doc_id in the
     * {@code complete} map must be treated as absent (that doc simply isn't
     * completed by this call), never coerced into the literal string "null"
     * via {@code String.valueOf}.
     */
    @Test
    void writeManifestMany_httpRoute_completeMapNullValue_treatedAsAbsent() throws Exception {
        String docId = "irf-wmm-http-nullcomplete-doc-1";
        registerDoc(docId);
        String chash = chash("irf-wmm-http-nullcomplete-chash");
        insertChunk1024(chash);

        var ex = postCatalog("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + COLLECTION + "\",\"docs\":[{\"doc_id\":\"" + docId + "\",\"rows\":[{\"position\":0,\"chash\":\""
                + chash + "\",\"chunk_index\":0}]}],\"complete\":{\"" + docId + "\":null}}");
        handleCatalog(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"complete_refused_count\":0");

        var got = repo.getDocument(TENANT, docId);
        assertThat(got.get("index_state"))
            .as("a null completion value must be treated as absent, not stamp index_content_hash='null'")
            .isNotEqualTo("complete");
        assertThat(got.get("index_content_hash"))
            .as("must never become the literal string 'null'")
            .isNotEqualTo("null");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // stacked-review item 1: per-(tenant,doc_id) advisory xact lock
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Direct, deterministic proof that {@code completeIndexRun} actually
     * acquires the {@code acquireIndexRunLock} advisory lock (rather than
     * relying on a business-level race that would be flaky to assert on): a
     * separate raw connection holds the SAME lock key manually; a concurrent
     * {@code completeIndexRun} call for that (tenant, doc_id) must BLOCK
     * (observable via {@code pg_locks} — a session waiting on an
     * ungranted advisory lock) until the holder releases it, then proceed
     * and succeed. Bounded polling (up to 1s), not a timing race: the
     * assertion is "a waiter eventually appears", never "appears within Xms".
     */
    @Test
    void completeIndexRun_blocksOnAdvisoryLock_heldByConcurrentSession() throws Exception {
        String docId = "irf-lock-doc-1";
        registerDoc(docId);
        writeOneRowManifestWithMatchingChunk(docId, "irf-lock-chash");
        beginViaHttp(docId, "h", "run-lock-1");

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            try (var ps = holder.prepareStatement(
                    "SELECT pg_advisory_xact_lock(hashtext('indexrun:' || ? || ':' || ?))")) {
                ps.setString(1, TENANT);
                ps.setString(2, docId);
                ps.execute();
            }

            var completed = new java.util.concurrent.CountDownLatch(1);
            var failure = new java.util.concurrent.atomic.AtomicReference<Throwable>();
            Thread t = new Thread(() -> {
                try {
                    repo.completeIndexRun(TENANT, docId, "h", 1);
                } catch (Throwable e) {
                    failure.set(e);
                } finally {
                    completed.countDown();
                }
            });
            t.start();

            boolean sawWaiter = false;
            for (int i = 0; i < 50 && !sawWaiter; i++) {
                Thread.sleep(20);
                try (Connection su = pg.createConnection("")) {
                    var rs = su.createStatement().executeQuery(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted");
                    rs.next();
                    sawWaiter = rs.getLong(1) > 0;
                }
            }
            assertThat(sawWaiter)
                .as("a concurrent completeIndexRun must BLOCK on the SAME advisory lock the holder "
                    + "took, not race past it — this is the direct proof acquireIndexRunLock runs "
                    + "before the verify read")
                .isTrue();

            holder.rollback(); // release the lock
            assertThat(completed.await(5, java.util.concurrent.TimeUnit.SECONDS))
                .as("completeIndexRun must proceed once the lock is released").isTrue();
            t.join(5000);
            assertThat(failure.get()).as("completeIndexRun must succeed once unblocked").isNull();
        }

        var doc = repo.getDocument(TENANT, docId);
        assertThat(doc.get("index_state")).isEqualTo("complete");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // /v1/vectors/update-metadata ("update-chunks") missing-ids report (AC6)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void updateMetadataWithMissing_repoLevel_reportsIdsAbsentFromStore() throws Exception {
        String existing = chash("irf-updatemeta-existing");
        String missing  = chash("irf-updatemeta-missing");
        insertChunk1024(existing);

        var outcome = vecRepo.updateMetadataWithMissing(TENANT, COLLECTION,
            List.of(existing, missing), List.of(Map.of("v", "1"), Map.of("v", "1")));

        assertThat(outcome.updated()).isEqualTo(1);
        assertThat(outcome.missing()).containsExactly(missing);
    }

    @Test
    void updateMetadata_httpRoute_responseIncludesMissingIds() throws Exception {
        String existing = chash("irf-updatemeta-http-existing");
        String missing  = chash("irf-updatemeta-http-missing");
        insertChunk1024(existing);

        var ex = postVectors("/v1/vectors/update-metadata",
            "{\"collection\":\"" + COLLECTION + "\",\"ids\":[\"" + existing + "\",\"" + missing + "\"],"
                + "\"metadatas\":[{\"v\":\"1\"},{\"v\":\"1\"}]}");
        handleVectors(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"updated\":1").contains(missing);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    private static String chash(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private void registerDoc(String docId) {
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", docId,
            "title", "Test Doc " + docId,
            "content_type", "knowledge",
            "corpus", "knowledge",
            "physical_collection", COLLECTION));
    }

    /** Writes a single manifest row for docId AND a matching chunks_1024 row (present, not missing). */
    private String writeOneRowManifestWithMatchingChunk(String docId, String chashSeed) {
        String chash = chash(chashSeed);
        repo.writeManifest(TENANT, docId, COLLECTION, List.of(
            Map.<String, Object>of("position", 0, "chash", chash, "chunk_index", 0)));
        insertChunk1024(chash);
        return chash;
    }

    private void insertChunk1024(String chashHex) throws RuntimeException {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) " +
                "VALUES ('" + TENANT + "', '" + COLLECTION + "', decode('" + chashHex + "', 'hex'), " +
                "'chunk text', ('[1" + ",0".repeat(1023) + "]')::vector) " +
                "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private void beginViaHttp(String docId, String contentHash, String runId) throws Exception {
        var ex = postCatalog("/v1/catalog/index-run/begin",
            "{\"doc_id\":\"" + docId + "\",\"content_hash\":\"" + contentHash + "\",\"run_id\":\"" + runId
                + "\",\"collection\":\"" + COLLECTION + "\"}");
        handleCatalog(ex);
        assertThat(ex.status).as("begin must succeed as test setup: " + ex.bodyString()).isEqualTo(200);
    }

    private void handleCatalog(CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            catalogHandler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private void handleVectors(CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            vectorHandler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private static CapturingExchange postCatalog(String path, String jsonBody) {
        return new CapturingExchange("POST", URI.create(path), jsonBody);
    }

    private static CapturingExchange getCatalog(String pathWithQuery) {
        return new CapturingExchange("GET", URI.create(pathWithQuery), "");
    }

    private static CapturingExchange postVectors(String path, String jsonBody) {
        return new CapturingExchange("POST", URI.create(path), jsonBody);
    }

    /** Minimal {@link HttpExchange} that captures the response status + body (same pattern as
     *  {@code CatalogHandlerUpdateManyDeleteManyTest}). */
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
