// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24, linda 2026-11-22): every id-path twin {@code nexus.<name>_by_id} that vectors-077 adds carries
 * its original's posture exactly. PUBLIC may keep EXECUTE on a twin only because the original is what it is: a SECURITY DEFINER
 * twin of an INVOKER original, or one without the original's {@code SET search_path}, would read past row-level security.
 *
 * <p>On a migrated template, each {@code nexus} function named {@code %_by_id} is paired with the {@code nexus} function of the
 * name without the suffix whose identity arguments are the twin's with the id parameter put back as the label
 * ({@code p_collection_ids bigint[]} as {@code p_collections text[]}, {@code p_collection_id bigint} as {@code p_collection text}).
 * The pair must agree on {@code prolang}, {@code prosecdef}, {@code proconfig}, {@code proowner}, {@code provolatile},
 * {@code proparallel}, {@code proleakproof} and {@code proacl} (both NULL, or the same aclitems; compared as a sorted set, since
 * the order of an ACL's items carries no privilege). Every difference is reported with the pair and the column.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35bIdTwinAttributesTest {

    /** vectors-077-100..139 (the search functions) and 140 (chunk_live_owners): 41 twins. */
    private static final int TWINS = 41;
    private static final String SUFFIX = "_by_id";

    private static final List<String> COLUMNS = List.of(
        "prolang", "prosecdef", "proconfig", "proowner", "provolatile", "proparallel", "proleakproof", "proacl");

    private PostgreSQLContainer<?> pg;
    /** proname + "(" + identity arguments + ")" -> column -> value as text (null kept). */
    private final Map<String, Map<String, String>> functions = new LinkedHashMap<>();

    @BeforeAll
    void readTheCatalog() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Table<?> p = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
            Table<?> n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
            Field<Object> oid = DSL.field(DSL.name("p", "oid"));
            Field<String> name = DSL.field(DSL.name("p", "proname"), String.class);
            Field<String> args = DSL.function(DSL.name("pg_catalog", "pg_get_function_identity_arguments"), String.class, oid);
            List<Field<String>> cols = new ArrayList<>();
            for (String c : COLUMNS) {
                cols.add(DSL.field(DSL.name("p", c)).cast(SQLDataType.VARCHAR).as(c));
            }
            List<Field<?>> select = new ArrayList<>(List.of(name, args.as("args")));
            select.addAll(cols);
            for (Record r : ctx.select(select)
                    .from(p)
                    .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                    .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                    .fetch()) {
                Map<String, String> attrs = new LinkedHashMap<>();
                for (String c : COLUMNS) {
                    attrs.put(c, r.get(c, String.class));
                }
                functions.put(r.get(name) + "(" + r.get("args", String.class) + ")", attrs);
            }
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    /** The original's key for a twin's key: the suffix dropped and the id parameter put back as the label. */
    static String originalOf(String twinKey) {
        int open = twinKey.indexOf('(');
        String fn = twinKey.substring(0, open);
        String args = twinKey.substring(open);
        return fn.substring(0, fn.length() - SUFFIX.length())
            + args.replace("p_collection_ids bigint[]", "p_collections text[]")
                  .replace("p_collection_id bigint", "p_collection text");
    }

    /** An ACL as its sorted items, so two ACLs that grant the same compare equal whatever their order. */
    static String normalized(String column, String value) {
        if (value == null || !column.equals("proacl")) {
            return value;
        }
        String[] items = value.replaceAll("^\\{|}$", "").split(",");
        Arrays.sort(items);
        return String.join(",", items);
    }

    @Test
    void everyTwinHasItsOriginalsPosture() {
        List<String> twins = functions.keySet().stream()
            .filter(k -> k.substring(0, k.indexOf('(')).endsWith(SUFFIX)).sorted().toList();
        assertThat(twins).as("non-vacuity: vectors-077-100..140 add %d id twins", TWINS).hasSize(TWINS);

        List<String> orphans = new ArrayList<>();
        List<String> differences = new ArrayList<>();
        for (String twin : twins) {
            String original = originalOf(twin);
            Map<String, String> o = functions.get(original);
            if (o == null) {
                orphans.add(twin + " has no original " + original);
                continue;
            }
            Map<String, String> t = functions.get(twin);
            for (String c : COLUMNS) {
                if (!Objects.equals(normalized(c, o.get(c)), normalized(c, t.get(c)))) {
                    differences.add(twin + " vs " + original + ": " + c + " is " + t.get(c) + ", the original's is " + o.get(c));
                }
            }
        }
        assertThat(orphans).as("every twin pairs with an original").isEmpty();
        assertThat(differences).as("a twin copies its original's posture exactly").isEmpty();
    }

    @Test
    void thePairingRule_putsTheLabelBack() {
        assertThat(originalOf("plain_search_384_by_id(p_query nexus.vector, p_collection_ids bigint[], p_n integer)"))
            .isEqualTo("plain_search_384(p_query nexus.vector, p_collections text[], p_n integer)");
        assertThat(originalOf("search_topic_scoped_384_by_id(p_topic_label text, p_collection_id bigint, p_n integer)"))
            .isEqualTo("search_topic_scoped_384(p_topic_label text, p_collection text, p_n integer)");
        assertThat(normalized("proacl", "{b=X/o,=X/o}")).isEqualTo(normalized("proacl", "{=X/o,b=X/o}"));
        assertThat(normalized("proacl", null)).isNull();
    }
}
