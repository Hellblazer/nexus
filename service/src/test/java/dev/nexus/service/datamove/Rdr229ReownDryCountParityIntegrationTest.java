// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 (bead nexus-p2m1x.14): the operator's read-only dry count of the re-own move
 * ({@code service/src/test/resources/datamove/rdr229-reown-count.sql}, loaded from the classpath, never copied) predicts exactly
 * what {@code rdr229-reown-repo-stems-from-curator} (vectors-067-1) does. Per tenant, before the move, the query runs read-only
 * and must write nothing; the move then runs to completion and the prediction is compared with the rows whose owner changed, the
 * gc_audit skip reports and the owners the move inserted. Every template change of vectors-067 is mirrored in that file, and
 * these tests are the proof. Fixtures: {@link Rdr229ReownFixtures}; the move's own behaviour:
 * {@link Rdr229ReownRepoStemsMoveIntegrationTest}.
 */
class Rdr229ReownDryCountParityIntegrationTest extends Rdr229ReownFixtures {

    // ── parity with the operator's read-only dry count ──────────────────────────────────────────────────────

    /** The dry-count query bob runs on the cloud: loaded from the classpath, never copied, so the tested file is the run file. */
    static final String COUNT_SQL_RESOURCE = "/datamove/rdr229-reown-count.sql";

    record Pred(String action, String name, String ownerId, String dest, String reason) { }

    static String countSql() throws Exception {
        try (var in = Rdr229ReownDryCountParityIntegrationTest.class.getResourceAsStream(COUNT_SQL_RESOURCE)) {
            assertThat(in).as("the dry-count file is on the test classpath").isNotNull();
            return new String(in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
        }
    }

    /** The query for one tenant, in a READ ONLY transaction with SET LOCAL nexus.tenant: a write would raise 25006. */
    List<Pred> predict(String tenant) throws Exception {
        List<Pred> out = new ArrayList<>();
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            c.setAutoCommit(false);
            c.setReadOnly(true);
            try {
                DSLContext d = DSL.using(c, SQLDialect.POSTGRES);
                // set_config(.., true) is SET LOCAL: scoped to this transaction
                d.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("nexus.tenant"), DSL.val(tenant),
                    DSL.inline(true))).fetch();
                d.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("statement_timeout"), DSL.val("60s"),
                    DSL.inline(true))).fetch();
                // The shipped dry-count file, passed by name (the established shape for a published statement, as
                // PgVectorRepository's MANIFEST_LESS_CENSUS_SQL and ChashRepository.PROBE_SQL are).
                for (Record r : d.resultQuery(countSql()).fetch()) {
                    out.add(new Pred(r.get(0, String.class), r.get(1, String.class), r.get(2, String.class),
                        r.get(3, String.class), r.get(4, String.class)));
                }
            } finally {
                c.rollback();
            }
        }
        return out;
    }

    Map<String, String> ownersOfAll(String tenant) {
        Map<String, String> m = new TreeMap<>();
        ctx().select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).forEach(r -> m.put(r.value1(), r.value2()));
        return m;
    }

    Map<String, String> ownerPrefixesWithHash(String tenant) {
        Map<String, String> m = new TreeMap<>();
        ctx().select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.REPO_HASH).from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)).forEach(r -> m.put(r.value1(), r.value2() == null ? "" : r.value2()));
        return m;
    }

    /**
     * The dry count must predict exactly what the move does. Per tenant, before the move: run the query read-only (and see
     * that the data is unchanged). Then run the move to completion and compare: (a) the reown and reown_quarantine_sibling
     * names are the rows whose owner_id changed, each to the predicted owner; (b) the skip rows are the gc_audit reports,
     * name and reason; (c) the distinct {@code new:} owners are the catalog_owners rows the move inserted, and the rows moved
     * to a new owner hold its tumbler. {@code expected} is the aggregate "reown=N skip=N sibling=N new=N" (non-vacuity).
     */
    void assertParity(String expected, String... tenants) throws Exception {
        Map<String, List<Pred>> predicted = new LinkedHashMap<>();
        Map<String, Map<String, String>> collsBefore = new LinkedHashMap<>();
        Map<String, Map<String, String>> ownersBefore = new LinkedHashMap<>();
        for (String t : tenants) {
            collsBefore.put(t, ownersOfAll(t));
            ownersBefore.put(t, ownerPrefixesWithHash(t));
            predicted.put(t, predict(t));
            assertThat(ownersOfAll(t)).as("(d) the dry count wrote nothing: collections, " + t).isEqualTo(collsBefore.get(t));
            assertThat(ownerPrefixesWithHash(t)).as("(d) the dry count wrote nothing: owners, " + t).isEqualTo(ownersBefore.get(t));
            assertThat(skipReasons(t)).as("(d) the dry count wrote nothing: gc_audit, " + t).isEmpty();
        }

        runToCompletion();

        int reown = 0, skip = 0, sibling = 0, minted = 0;
        for (String t : tenants) {
            List<Pred> preds = predicted.get(t);
            Map<String, String> before = collsBefore.get(t);
            Map<String, String> after = ownersOfAll(t);
            Map<String, String> ownersAfter = ownerPrefixesWithHash(t);

            var changed = new TreeSet<String>();
            before.forEach((name, owner) -> {
                if (!owner.equals(after.get(name))) {
                    changed.add(name);
                }
            });
            var moves = preds.stream().filter(p -> !p.action().equals("skip")).toList();
            TreeSet<String> movedNames = new TreeSet<>();
            moves.forEach(p -> movedNames.add(p.name()));
            assertThat(movedNames)
                .as("(a) reown + reown_quarantine_sibling names are the rows whose owner_id changed, " + t).isEqualTo(changed);

            var newHashes = new TreeSet<String>();
            for (Pred p : moves) {
                if (p.dest().startsWith("new:")) {
                    String hash = p.dest().substring(4);
                    newHashes.add(hash);
                    String tumbler = ownersAfter.entrySet().stream().filter(e -> e.getValue().equals(hash))
                        .map(Map.Entry::getKey).findFirst().orElse(null);
                    assertThat(tumbler).as("(c) the move minted the owner for hash " + hash + ", " + t).isNotNull();
                    assertThat(after.get(p.name())).as("(c) " + p.name() + " moved to the minted owner's tumbler")
                        .isEqualTo(tumbler.replace('.', '-'));
                } else {
                    assertThat(after.get(p.name())).as("(a) " + p.name() + " moved to the predicted owner").isEqualTo(p.dest());
                }
            }
            var inserted = new TreeSet<>(ownersAfter.keySet());
            inserted.removeAll(ownersBefore.get(t).keySet());
            assertThat(inserted).as("(c) the number of distinct new: owners is the number of owners the move inserted, " + t)
                .hasSize(newHashes.size());
            TreeSet<String> insertedHashes = new TreeSet<>();
            inserted.forEach(prefix -> insertedHashes.add(ownersAfter.get(prefix)));
            assertThat(insertedHashes).as("(c) the inserted owners carry the predicted hashes, " + t).isEqualTo(newHashes);

            Map<String, String> predictedSkips = new TreeMap<>();
            preds.stream().filter(p -> p.action().equals("skip")).forEach(p -> predictedSkips.put(p.name(), p.reason()));
            assertThat(skipReasons(t)).as("(b) skip names and reasons are the gc_audit reports, " + t).isEqualTo(predictedSkips);

            reown += (int) moves.stream().filter(p -> p.action().equals("reown")).count();
            sibling += (int) moves.stream().filter(p -> p.action().equals("reown_quarantine_sibling")).count();
            skip += predictedSkips.size();
            minted += newHashes.size();
        }
        assertThat("reown=" + reown + " skip=" + skip + " sibling=" + sibling + " new=" + minted)
            .as("non-vacuity: the shape's prediction").isEqualTo(expected);
    }

    @Test
    void dryCountParity_gateXr789Shape() throws Exception {
        gateXr789(T);
        collection(T, "knowledge__distributed-systems__voyage-context-3__v1", "1-40");
        collection(T, "quarantine-code__arcaneum-2ad2825c__voyage-code-3__v1", "1-40", "quarantine", "");
        collection(T, "code__arcaneum-2ad2825c__voyage-code-3__v0", "1-40", "live", "code__arcaneum-2ad2825c__voyage-code-3__v1");
        register();
        assertParity("reown=2 skip=0 sibling=1 new=1", T);
    }

    @Test
    void dryCountParity_restoredLegacyBesideItsNativeTwin() throws Exception {
        restoredLegacyBesideNative(T);
        register();
        assertParity("reown=1 skip=1 sibling=1 new=0", T);
    }

    @Test
    void dryCountParity_twoStemsOfOneHash_oneChunk() throws Exception {
        twoStemsRegistered(1000);
        assertParity("reown=2 skip=1 sibling=0 new=2", T);
    }

    @Test
    void dryCountParity_twoStemsOfOneHash_chunkSizeOne() throws Exception {
        twoStemsRegistered(1);
        assertParity("reown=2 skip=1 sibling=0 new=2", T);
    }

    @Test
    void dryCountParity_aDocsOnlyStemWhoseHashHasARepoOwner() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        owner(T, "1.7", "arcaneum-checkout", "repo", "2ad2825c");
        owner(T, "1.9", "some-repo", "repo", "11112222");
        collection(T, ARC_DOCS, "1-40");
        collection(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1", "1-9");
        collection(T, "docs__notes-0badc0de__voyage-context-3__v1", "1-40");
        register();
        assertParity("reown=1 skip=0 sibling=0 new=0", T);
    }

    @Test
    void dryCountParity_aStemWhoseRowsFallInDifferentChunks() throws Exception {
        gateXr789(T);
        collection(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1", "1-40");
        register();
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), 1)
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();
        assertParity("reown=3 skip=0 sibling=0 new=1", T);
    }

    @Test
    void dryCountParity_aFreshInstallWithNoRows() throws Exception {
        register();
        assertParity("reown=0 skip=0 sibling=0 new=0", T);
    }

    @Test
    void dryCountParity_aTenantPair() throws Exception {
        gateXr789(T);
        owner(U, "1.3", "knowledge", "curator", null);
        owner(U, "1.4", "older-repo", "repo", "cafe0001");
        collection(U, ARC_CODE, "1-3");
        collection(U, ARC_DOCS, "1-3");
        register();
        assertParity("reown=4 skip=0 sibling=0 new=2", T, U);
    }

    @Test
    void dryCountParity_aBlankVersionRowBesideItsV1Twin() throws Exception {
        blankVersionLegacyBesideNative(T);
        register();
        assertParity("reown=0 skip=1 sibling=0 new=0", T);
    }

    @Test
    void dryCountParity_aDormantHolder() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        owner(T, "1.17", "arcaneum", "repo", "2ad2825c");
        collection(T, "code__1-17__voyage-code-3__v1", "1-17", "dormant", "");
        collection(T, ARC_CODE, "1-40");
        register();
        assertParity("reown=0 skip=1 sibling=0 new=0", T);
    }
}
