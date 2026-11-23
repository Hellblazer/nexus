// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.ImportTestEnv.IMPORT;
import static dev.nexus.service.ImportTestEnv.json;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.TenantImportSessions;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import dev.nexus.service.db.TenantExport;

/**
 * RDR-229 row 15 addendum (nexus-p2m1x.22): a tenant import whose COMMIT reaches the engine as an exception ("the commit was not
 * confirmed") may still have been applied by PostgreSQL, so the tenant's cached registry state is emptied for it as for a confirmed
 * commit (the session's {@code onCommitted} hook, {@code CollectionRegistry#evictTenant} in production). The import is still
 * reported failed.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantImportUnconfirmedCommitTest {

    private static final String SRC = "t-imp-uc-source";

    private ImportTestEnv env;
    private ImportTestEnv.BackupFile file;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
        String srcToken = env.newTenant(SRC);
        List<String> failures = new ArrayList<>();
        env.su(ctx -> {
            TenantExportBatteryTest.Seed.rows(ctx, SRC, 1, failures);
            TenantExportBatteryTest.Seed.fillNulls(ctx, SRC, failures);
            return null;
        });
        assertThat(failures).as("the seed").isEmpty();
        file = env.export(srcToken);
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    @Test
    void aLoadWhoseCommitIsUnconfirmedStillEmptiesTheTenantsCachedState_andIsReportedFailed() throws Exception {
        String tenant = "t-imp-uc-target";
        String token = env.newTenant(tenant);
        env.freshSessions();
        CommitThenThrowDataSource lossy = new CommitThenThrowDataSource(env.ds);
        List<String> evicted = new CopyOnWriteArrayList<>();
        var sessions = new TenantImportSessions(env.service::exportSessions, lossy.dataSource(),
            TenantImportSessions.Config.defaults(), evicted::add, env.service.importWiring()::singletonLockHeld, () -> null,
            TenantExport.RETIRED);
        env.service.importWiring().sessions(sessions);
        sessions.loadHookForTests(text -> {
            if (text.equals("loading memory")) {   // the load is between its inserts and its COMMIT
                lossy.armAnyCommit();
            }
        });
        HttpResponse<String> opened = env.open(token, file);
        assertThat(opened.statusCode()).as(opened.body()).isEqualTo(200);
        JsonNode open = json(opened);
        String id = open.get("session_id").asText();
        env.upload(token, id, file, open, 100);

        assertThat(env.call("POST", IMPORT + "/" + id + "/commit", token).statusCode()).isEqualTo(202);
        JsonNode end = env.awaitEnd(token, id, Duration.ofMinutes(2));
        lossy.disarm();

        assertThat(lossy.lostAcknowledgements()).as("setup: the COMMIT was applied and then reported lost").isGreaterThan(0);
        assertThat(end.get("status").asText()).as("reported failed, as before").startsWith("failed: the commit was not confirmed");
        assertThat(evicted).as("the tenant's cached state was emptied although the COMMIT was not confirmed").contains(tenant);
    }
}
