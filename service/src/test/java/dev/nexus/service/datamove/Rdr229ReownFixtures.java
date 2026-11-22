// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.BeforeEach;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.TreeMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * The shared fixtures of the re-own move's integration tests (RDR-229, bead nexus-p2m1x.14): the move's label and tenants, the
 * registration by the shipped changeset, the worker run to completion, and the catalog row and owner builders. An abstract
 * base so the move test and the dry-count parity test share them and neither duplicates them; it adds no test of its own. The
 * database is the pg_background image {@link DataMoveIntegrationBase} hands out (a clone, not a walk of its own).
 */
abstract class Rdr229ReownFixtures extends DataMoveIntegrationBase {

    static final String LABEL = "rdr229-reown-repo-stems-from-curator";

    static final String REGISTER = "db/changelog/vectors-067-reown-repo-stems-register.xml";

    static final String T = "rdr229-reown-a";

    static final String U = "rdr229-reown-b";

    static final String ARC_CODE = "code__arcaneum-2ad2825c__voyage-code-3__v1";

    static final String ARC_DOCS = "docs__arcaneum-2ad2825c__voyage-context-3__v1";

    static final String WOW_DOCS = "docs__wow-addon-dev__voyage-context-3__v1";

    int runs;

    @BeforeEach
    void freshCatalog() throws Exception {
        ctx().deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.like("rdr229-reown-%")).execute();
        ctx().deleteFrom(CATALOG_OWNERS).where(CATALOG_OWNERS.TENANT_ID.like("rdr229-reown-%")).execute();
        ctx().deleteFrom(table("nexus", "gc_audit")).where(DSL.field(DSL.name("tenant_id"), String.class).like("rdr229-reown-%"))
            .execute();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    /** Registers the move by the shipped changeset, as the migrating role, exactly as a boot does. */
    void register() throws Exception {
        // The dml_utils runs (<label>/<tenant>) outlive the data_moves row: a later test with the same label and tenant
        // would resume a run that already completed, execute no chunk, and fail verification on its fresh rows. Archive
        // them, as a tenant removal does, so every test starts with no run.
        for (String label : new String[] {LABEL, "again"}) {
            for (String run : DataMoveRuns.activeRunLabels(ctx(), label)) {
                DataMoveRuns.archive(ctx(), run);
            }
        }
        ctx().deleteFrom(table("nexus", "data_moves")).execute();
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            PgContainerHelper.runSuperuserTestChangelog(c, REGISTER, "databasechangelog_test_reown_register_" + (++runs),
                Map.of());
        }
        assertThat(ctx().fetchCount(table("nexus", "data_moves"),
            DSL.field(DSL.name("label"), String.class).eq(LABEL))).as("registered by the shipped changeset").isEqualTo(1);
    }

    /** One worker cycle, then the move must be complete: verified, with {@code completed_at} set. */
    void runToCompletion() {
        for (int i = 0; i < 3 && completedAt(LABEL) == null; i++) {
            worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher()).runCycle();
        }
        assertThat(completedAt(LABEL)).as("the move ran, verified and completed on the worker").isNotNull();
    }

    void owner(String tenant, String prefix, String name, String type, String repoHash) {
        ctx().insertInto(CATALOG_OWNERS, CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME,
                CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.NEXT_SEQ)
            .values(tenant, prefix, name, type, repoHash, "", 0L).execute();
    }

    void collection(String tenant, String name, String owner) {
        collection(tenant, name, owner, "live", "");
    }

    void collection(String tenant, String name, String owner, String lifecycle, String supersededBy) {
        String contentType = name.substring(0, name.indexOf("__"));
        ctx().insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE, CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .values(tenant, name, contentType, owner,
                contentType.contains("code") ? "voyage-code-3" : "voyage-context-3", "v1", lifecycle, supersededBy)
            .execute();
    }

    /** A live collection whose stored model_version is {@code version} ({@code ""}: not yet written by D2's repair). */
    void collectionWithVersion(String tenant, String name, String owner, String lifecycle, String version) {
        String contentType = name.substring(0, name.indexOf("__"));
        ctx().insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE, CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .values(tenant, name, contentType, owner,
                contentType.contains("code") ? "voyage-code-3" : "voyage-context-3", version, lifecycle, "")
            .execute();
    }

    String ownerOf(String tenant, String name) {
        return ctx().select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.OWNER_ID);
    }

    /** The tenant's owners as {@code prefix=name/type/hash}, for an exact comparison. */
    List<String> ownersOf(String tenant) {
        return ctx().select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE,
                CATALOG_OWNERS.REPO_HASH, CATALOG_OWNERS.REPO_ROOT)
            .from(CATALOG_OWNERS).where(CATALOG_OWNERS.TENANT_ID.eq(tenant)).orderBy(CATALOG_OWNERS.TUMBLER_PREFIX)
            .fetch().stream().map(r -> r.value1() + "=" + r.value2() + "/" + r.value3() + "/"
                + (r.value4() == null ? "" : r.value4()) + "/" + r.value5()).collect(Collectors.toList());
    }

    /** The gate-xr789 shape: a curator 1.40 owns a repo's code and docs and an unrelated doc corpus; no repo owner. */
    void gateXr789(String tenant) {
        owner(tenant, "1.40", "knowledge", "curator", null);
        collection(tenant, ARC_CODE, "1-40");
        collection(tenant, ARC_DOCS, "1-40");
        collection(tenant, WOW_DOCS, "1-40");
    }

    // ── an occupied destination tuple: skipped, reported, the move completes ────────────────────────────────

    /** A native code__1-17 collection of the repo, its restored legacy twin under the curator, and one sibling of each. */
    void restoredLegacyBesideNative(String tenant) {
        owner(tenant, "1.40", "knowledge", "curator", null);
        owner(tenant, "1.17", "arcaneum", "repo", "2ad2825c");
        collection(tenant, "code__1-17__voyage-code-3__v1", "1-17");                  // native: holds (code, 1-17, voyage-code-3, v1)
        collection(tenant, ARC_CODE, "1-40");                                          // legacy: would land on that tuple
        collection(tenant, ARC_DOCS, "1-40");                                          // legacy docs: its destination is free
        collection(tenant, "quarantine-" + ARC_CODE, "1-40", "quarantine", "");
        collection(tenant, "quarantine-" + ARC_DOCS, "1-40", "quarantine", "");
    }

    List<String> skipReports(String tenant) {
        var op = DSL.field(DSL.name("operation"), String.class);
        return ctx().select(DSL.field(DSL.name("collection"), String.class), DSL.field(DSL.name("details"), String.class))
            .from(table("nexus", "gc_audit"))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).and(op.eq("rdr229_reown_skipped"))
            .orderBy(DSL.field(DSL.name("id"))).fetch().stream().map(r -> r.value1() + " " + r.value2()).toList();
    }

    /** Two curator-owned stems of one repo hash and different slugs, neither with an owner yet: one free tuple, two claimants. */
    void twoStemsRegistered(int chunkSize) throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        collection(T, "code__a-c0ffee11__voyage-code-3__v1", "1-40");
        collection(T, "code__b-c0ffee11__voyage-code-3__v1", "1-40");
        collection(T, "code__c-deadbeef__voyage-code-3__v1", "1-40");   // another hash after the pair: pins a gap-free numbering
        collection(T, "quarantine-code__b-c0ffee11__voyage-code-3__v1", "1-40", "quarantine", "");
        register();
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), chunkSize)
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();
    }

    Map<String, String> skipReasons(String tenant) {
        Map<String, String> m = new TreeMap<>();
        ctx().select(DSL.field(DSL.name("collection"), String.class), DSL.jsonbGetAttributeAsText(DSL.field(DSL.name("details"), SQLDataType.JSONB), "reason"))
            .from(table("nexus", "gc_audit"))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant))
            .and(DSL.field(DSL.name("operation"), String.class).eq("rdr229_reown_skipped"))
            .forEach(r -> m.put(r.value1(), r.value2()));
        return m;
    }

    // ── S1: the version in a tuple is the one D2 will write ─────────────────────────────────────────────────

    /** A native v1 collection of the repo, and its legacy twin under the curator whose model_version D2 has not yet written. */
    void blankVersionLegacyBesideNative(String tenant) {
        owner(tenant, "1.40", "knowledge", "curator", null);
        owner(tenant, "1.17", "arcaneum", "repo", "2ad2825c");
        collection(tenant, "code__1-17__voyage-code-3__v1", "1-17");
        collectionWithVersion(tenant, ARC_CODE, "1-40", "live", "");
    }
}
