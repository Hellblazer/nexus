// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.AnnHit;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import javax.sql.DataSource;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CopyOnWriteArrayList;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24/.25): once gate 3 holds, {@code TaxonomyCentroidRepository.annQuery} hands the registry row's id
 * to {@code nexus.taxonomy_ann_query_<dim>}'s by-id overload (vectors-084), so the same-collection predicate is
 * {@code collection_id = $n} and the planner estimates it from the value. The plan regression this guards (the text overload's
 * InitPlan subselect makes a seq scan win at 768 and 1024, TaxonomyCentroidAnnPlanShapeTest) cannot come back unseen: the test
 * reads the statement JDBC was handed, the SQL text and the value bound to the last parameter, not the rows it returned (a name
 * call returns the same rows).
 *
 * <p>vectors-084 is CONTINUE-gated, so the fresh walk skips it; the class applies the form A state itself unless the run already did
 * ({@code -Dnexus.test.formA.applied=true}), and the schema helper fails when the three ids are not recorded.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyCentroidAnnQueryByIdTest {

    private static final String SVC_ROLE = "svc_ann_by_id_test";
    private static final String SVC_PASS = "svc_ann_by_id_pass";
    private static final String TENANT = "ann-by-id-tenant";
    private static final int[] DIMS = {384, 768, 1024};

    /** One statement as JDBC received it: the SQL text and the values bound by position. */
    private record Seen(String sql, Map<Integer, Object> params) {}

    /** The service pool's connection, recording every prepared statement. */
    private final List<Seen> seen = new CopyOnWriteArrayList<>();

    private PostgreSQLContainer<?> pg;
    private HikariDataSource pool;
    private TenantScope tenantScope;

    private static String collection(int dim) {
        return switch (dim) {
            case 384 -> "knowledge__annbyid__minilm-l6-v2-384__v1";
            case 768 -> "docs__annbyid__bge-base-en-v15-768__v1";
            default -> "knowledge__annbyid__voyage-context-3__v1";
        };
    }

    private static String foreign(int dim) {
        return collection(dim).replace("__annbyid__", "__annbyidother__");
    }

    private static String model(int dim) {
        return switch (dim) {
            case 384 -> "minilm-l6-v2-384";
            case 768 -> "bge-base-en-v15-768";
            default -> "voyage-context-3";
        };
    }

    private static float[] unit(int dim) {
        float[] v = new float[dim];
        v[0] = 1.0f;
        return v;
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
            if (!Rdr229CompleteStamps.formAApplied()) {
                Rdr229CompleteStamps.completeAndApplyFormA(su);
            }
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
        pool = new HikariDataSource(cfg);
        tenantScope = new TenantScope(recording(pool));
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT);
            for (int dim : DIMS) {
                PgContainerHelper.insertCollection(ctx, TENANT, collection(dim));
                PgContainerHelper.insertCollection(ctx, TENANT, foreign(dim));
                var col = dim == 384 ? TAXONOMY_CENTROIDS.EMBEDDING_384
                    : dim == 768 ? TAXONOMY_CENTROIDS.EMBEDDING_768 : TAXONOMY_CENTROIDS.EMBEDDING_1024;
                for (String coll : List.of(collection(dim), foreign(dim))) {
                    ctx.insertInto(TAXONOMY_CENTROIDS,
                            TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION, TAXONOMY_CENTROIDS.COLLECTION_ID,
                            TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, col,
                            TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT)
                        .values(TENANT, coll, CollectionIds.require(ctx, TENANT, coll), coll.equals(collection(dim)) ? 1L : 2L,
                            model(dim), Vector.of(unit(dim)), "c", 1)
                        .execute();
                }
            }
        }
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void reset() {
        seen.clear();
    }

    private long registryId(int dim, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return CollectionIds.require(DSL.using(su, SQLDialect.POSTGRES), TENANT, collection);
        }
    }

    /** The statements of the ANN function the call ran; non-vacuity: at least one, so a probe that saw nothing fails. */
    private List<Seen> annStatements(int dim) {
        List<Seen> ann = seen.stream().filter(s -> s.sql().contains("taxonomy_ann_query_" + dim)).toList();
        assertThat(ann).as("non-vacuity: the probe saw the ANN statement for dim %d (saw %d statements)", dim, seen.size()).isNotEmpty();
        return ann;
    }

    @Test
    void gate3Holds_annQueryPassesTheRegistryRowsId_atEveryDim_sameAndCrossCollection() throws Exception {
        var repo = new TaxonomyCentroidRepository(tenantScope, () -> true);
        int checked = 0;
        for (int dim : DIMS) {
            for (boolean cross : new boolean[] {false, true}) {
                seen.clear();
                String source = collection(dim);
                List<AnnHit> hits = repo.annQuery(TENANT, unit(dim), source, cross, 10);
                assertThat(hits).as("dim %d cross=%s answers", dim, cross).extracting(AnnHit::topicId)
                    .containsExactly(cross ? 2L : 1L);
                Seen call = annStatements(dim).getFirst();
                assertThat(call.params()).as("dim %d: seven parameters, the registry id last", dim).hasSize(7);
                assertThat(call.params().get(7)).as("dim %d: the last bound value is the registry row's collection_id", dim)
                    .isEqualTo(registryId(dim, source));
                assertThat(call.sql()).as("dim %d: no subselect on the registry in the statement", dim)
                    .doesNotContain("catalog_collections").doesNotContainIgnoringCase("(select");
                checked++;
            }
        }
        assertThat(checked).as("non-vacuity: three dims, same and cross").isEqualTo(6);
    }

    @Test
    void gate3Never_annQueryKeepsTheTextCall() {
        var repo = new TaxonomyCentroidRepository(tenantScope, Gate3.never());
        for (int dim : DIMS) {
            seen.clear();
            assertThat(repo.annQuery(TENANT, unit(dim), collection(dim), false, 10)).extracting(AnnHit::topicId).containsExactly(1L);
            assertThat(annStatements(dim).getFirst().params()).as("dim %d: the six-parameter text call", dim).hasSize(6);
        }
    }

    // ---- the recording DataSource ----------------------------------------------------------------------------------------

    private DataSource recording(DataSource delegate) {
        return (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class},
            (proxy, method, args) -> {
                Object result = invoke(delegate, method, args);
                return "getConnection".equals(method.getName()) ? connection((Connection) result) : result;
            });
    }

    private Connection connection(Connection real) {
        return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> {
                if ("prepareStatement".equals(method.getName()) && args != null && args[0] instanceof String sql) {
                    return statement((PreparedStatement) invoke(real, method, args), sql);
                }
                return invoke(real, method, args);
            });
    }

    private PreparedStatement statement(PreparedStatement real, String sql) {
        Map<Integer, Object> params = new TreeMap<>();
        seen.add(new Seen(sql, params));
        return (PreparedStatement) Proxy.newProxyInstance(PreparedStatement.class.getClassLoader(), new Class<?>[] {PreparedStatement.class},
            (proxy, method, args) -> {
                if (method.getName().startsWith("set") && args != null && args.length >= 2 && args[0] instanceof Integer i) {
                    params.put(i, args[1]);
                }
                return invoke(real, method, args);
            });
    }

    private static Object invoke(Object target, java.lang.reflect.Method method, Object[] args) throws Throwable {
        try {
            return method.invoke(target, args);
        } catch (InvocationTargetException e) {
            throw e.getCause();
        }
    }
}
