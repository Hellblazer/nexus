// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.ArbiterTwins;
import dev.nexus.service.datamove.IdUniqueIndexPass;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.TaxonomyAnnQueryById;
import dev.nexus.service.vectors.TaxonomyAnnQueryById.Declared;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.Arrays;
import java.util.List;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24/.25): {@link TaxonomyAnnQueryById} is written by hand (the build's jOOQ codegen walks a fresh install,
 * which skips vectors-084), so nothing generated ties it to the function. This reads the catalog: on a database where vectors-084
 * applied ({@code -Dnexus.test.formA.applied=true}), the bigint overload of {@code nexus.taxonomy_ann_query_<dim>} at 384, 768 and 1024
 * has, by {@code pg_get_function_arguments} and {@code pg_get_function_result}, the argument names, types and order
 * {@link TaxonomyAnnQueryById#PARAMETERS} declares and the RETURNS TABLE columns {@link TaxonomyAnnQueryById#COLUMNS} declares; the
 * call binds one value to each parameter and has one field to each column; and the database accepts the call. On the fresh walk the
 * overload is absent, which is why gate 3 waits for vectors-084-1/2/3 ({@link ArbiterTwins#GATE3_WAITS_FOR_CHANGESETS}): annQuery calls
 * it from gate 3 with no fallback.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyAnnQueryByIdSignatureTest {

    private static final int[] DIMS = {384, 768, 1024};

    /** One overload as the catalog lists it. */
    private record Overload(String identityArguments, String arguments, String result) {
        boolean byId() {
            return identityArguments.endsWith(" bigint");   // the text overload ends in p_tenant text
        }
    }

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private Connection catalogConnection;
    private DSLContext ctx;
    private DSLContext catalog;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();   // the migrated clone, with vectors-084 applied when the run asked for form A
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
        // pg_get_function_arguments spells a type unqualified when its schema is on the search path: the catalog reads run on a
        // connection whose path is pg_catalog alone, so nexus.vector is always spelled so
        catalogConnection = pg.createConnection("");
        catalogConnection.setAutoCommit(true);
        catalog = DSL.using(catalogConnection, SQLDialect.POSTGRES);
        catalog.set(DSL.name("search_path"), DSL.inline("pg_catalog")).execute();
    }

    @AfterAll
    void stop() throws Exception {
        if (catalogConnection != null) catalogConnection.close();
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    private List<Overload> overloads(int dim) {
        var oid = DSL.field(DSL.name("p", "oid"));
        Field<String> identity = DSL.function("pg_get_function_identity_arguments", String.class, oid);
        Field<String> arguments = DSL.function("pg_get_function_arguments", String.class, oid);
        Field<String> result = DSL.function("pg_get_function_result", String.class, oid);
        return catalog.select(identity, arguments, result)
            .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("p", "proname"), String.class).eq("taxonomy_ann_query_" + dim))
            .fetch(r -> new Overload(r.get(identity), r.get(arguments), r.get(result)));
    }

    private static String spelled(List<Declared> declared) {
        return String.join(", ", declared.stream().map(d -> d.name() + " " + d.pgType()).toList());
    }

    private static Vector unit(int dim) {
        float[] v = new float[dim];
        v[0] = 1.0f;
        return Vector.of(v);
    }

    @Test
    void formA_everyByIdOverloadMatchesTheDeclaredNamesTypesAndOrder_andTheCallBindsAndSelectsExactlyThose() {
        Assumptions.assumeTrue(Rdr229CompleteStamps.formAApplied(), "the by-id overload exists only once vectors-084 applies (form A)");
        int checked = 0;
        for (int dim : DIMS) {
            List<Overload> all = overloads(dim);
            assertThat(all).as("non-vacuity: dim %d has its text overload and the by-id one, so the bigint filter told them apart", dim).hasSize(2);
            List<Overload> byId = all.stream().filter(Overload::byId).toList();
            assertThat(byId).as("exactly one bigint overload of taxonomy_ann_query_%d", dim).hasSize(1);
            Overload o = byId.getFirst();
            assertThat(o.arguments()).as("dim %d: pg_get_function_arguments: the IN arguments' names and types, in order", dim)
                .isEqualTo(spelled(TaxonomyAnnQueryById.PARAMETERS));
            assertThat(o.result()).as("dim %d: pg_get_function_result: the RETURNS TABLE columns, in order", dim)
                .isEqualTo("TABLE(" + spelled(TaxonomyAnnQueryById.COLUMNS) + ")");

            var call = TaxonomyAnnQueryById.call(dim, unit(dim), "c", false, 10, "m", "t", 1L);
            String rendered = DSL.using(SQLDialect.POSTGRES).render(DSL.select().from(call));
            assertThat(rendered.chars().filter(c -> c == '?').count()).as("dim %d: the call binds one value to each parameter (%s)", dim, rendered)
                .isEqualTo(TaxonomyAnnQueryById.PARAMETERS.size());
            assertThat(Arrays.stream(call.fields()).map(Field::getName).toList()).as("dim %d: the call selects the declared columns", dim)
                .containsExactlyElementsOf(TaxonomyAnnQueryById.COLUMNS.stream().map(Declared::name).toList());
            // the database resolves the call as built (overload, argument types): a collection id no row has, so it answers no rows
            assertThat(ctx.select().from(call).fetch()).as("dim %d: the call runs", dim).isEmpty();
            checked++;
        }
        assertThat(checked).as("non-vacuity: three widths").isEqualTo(3);
        assertThat(IdUniqueIndexPass.waitingChangesets(ctx)).as("under form A vectors-084 is recorded, so gate 3 has nothing to wait for on it")
            .doesNotContainAnyElementsOf(ArbiterTwins.GATE3_WAITS_FOR_CHANGESETS);
    }

    @Test
    void freshWalk_theByIdOverloadIsAbsent_whichIsWhyGate3WaitsForVectors084() {
        Assumptions.assumeFalse(Rdr229CompleteStamps.formAApplied(), "on the form A clone vectors-084 applied, so the overload exists (the form A test reads it)");
        for (int dim : DIMS) {
            List<Overload> all = overloads(dim);
            assertThat(all.stream().filter(Overload::byId).toList()).as("dim %d: vectors-084 is CONTINUE-gated and skipped on the fresh walk", dim).isEmpty();
            assertThat(all).as("non-vacuity: dim %d has its text overload, so the absence is of the by-id one only", dim).hasSize(1);
        }
        assertThat(ctx.fetchCount(DSL.table(DSL.name("public", "databasechangelog")), DSL.field(DSL.name("id"), String.class).like("vectors-084-%")))
            .as("non-vacuity: the changesets are unrecorded, which is what gate 3 reads").isZero();
        assertThat(IdUniqueIndexPass.waitingChangesets(ctx)).as("gate 3 names the three it waits for").containsAll(ArbiterTwins.GATE3_WAITS_FOR_CHANGESETS);
        assertThat(IdUniqueIndexPass.allValid(ctx)).as("gate 3's further condition does not hold without the overload").isFalse();
    }
}
