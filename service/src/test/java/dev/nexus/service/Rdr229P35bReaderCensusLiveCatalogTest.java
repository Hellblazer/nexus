// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24): the census of SQL readers of a collection name column
 * ({@code rdr229/p35b-reader-census.tsv}, generated from the changelog by {@code scripts/rdr229_p35b_reader_census.py}) must
 * name the same objects as the catalog of a fully migrated database. The script reads changelog text, so an object it cannot parse
 * (one built another way, one a later changeset replaced in a shape it misses) is absent from the file and would be left on the name
 * when P3.5b redefines the readers. This test asks the database which objects read a name column:
 * <ul>
 *   <li>views: {@code pg_depend} on a name column of a {@code nexus} relation, through the view's rewrite rule;</li>
 *   <li>functions: language sql or plpgsql, kind {@code f}, whose source (comments and string literals blanked) has a name column as a word;</li>
 *   <li>triggers: a column of {@code tgattr} that is a name column, or a name column in the WHEN clause;</li>
 *   <li>policies: a name column in the qualifier or the check.</li>
 * </ul>
 * The name columns are the {@code name_column} column of {@code db/rdr229/p31-checks.tsv}. Compared as sets of (kind, object), both ways,
 * the difference printed with the evidence line. A function that mentions a name column only in a write (an INSERT column list, a SET
 * target) is not in the census by design (writes are P3.1's); the query here does not tell reads from writes, so such an object would
 * show as live-only and is reported, not absorbed into the census.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35bReaderCensusLiveCatalogTest {

    private static final String CHECKS_LIST = "db/rdr229/p31-checks.tsv";
    private static final String CENSUS = "rdr229/p35b-reader-census.tsv";

    private PostgreSQLContainer<?> pg;
    private final Map<String, String> live = new LinkedHashMap<>();
    private final TreeSet<String> census = new TreeSet<>();
    private List<String> nameColumns;

    @BeforeAll
    void readBothSides() throws Exception {
        nameColumns = nameColumnsOfTheChecksList();
        readCensus();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            liveViews(ctx);
            liveFunctions(ctx);
            liveTriggers(ctx);
            livePolicies(ctx);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void theQueryExaminedTheCatalog_notAnEmptySet() {
        assertThat(nameColumns).as("the name columns of the checks list").containsExactlyInAnyOrder(
            "collection", "physical_collection", "source_collection", "superseded_by", "target_collection");
        assertThat(census).as("non-vacuity: the census file has rows").hasSizeGreaterThan(50);
        assertThat(live.keySet()).as("non-vacuity: the live query found readers of each kind it is built for")
            .anyMatch(k -> k.startsWith("function\t"))
            .anyMatch(k -> k.startsWith("view\t"))
            .anyMatch(k -> k.startsWith("trigger\t"));
        assertThat(live.keySet()).as("known form B and form A readers").contains(
            "view\tnexus.live_chunks", "view\tnexus.collection_vector_stats", "view\tnexus.topics_with_counts",
            "trigger\tnexus.catalog_documents.catalog_documents_owner_to_collection");
    }

    @Test
    void everyLiveReaderIsInTheCensus() {
        List<String> extra = new ArrayList<>();
        for (var e : live.entrySet()) {
            if (!census.contains(e.getKey())) {
                extra.add(e.getKey().replace('\t', ' ') + "   <- " + e.getValue());
            }
        }
        assertThat(extra).as("objects the catalog says read a name column that the census does not list (%d live, %d census)",
            live.size(), census.size()).isEmpty();
    }

    @Test
    void everyCensusObjectIsLive() {
        List<String> missing = new ArrayList<>();
        for (String key : census) {
            if (!live.containsKey(key)) {
                missing.add(key.replace('\t', ' '));
            }
        }
        assertThat(missing).as("census objects the catalog query did not find (%d live, %d census)", live.size(), census.size())
            .isEmpty();
    }

    // ---- the catalog side --------------------------------------------------------------------------------------------

    private static Table<?> cat(String name, String alias) {
        return DSL.table(DSL.name("pg_catalog", name)).as(alias);
    }

    private static Field<String> str(String table, String column) {
        return DSL.field(DSL.name(table, column), String.class);
    }

    private void liveViews(DSLContext ctx) {
        Field<String> view = str("v", "relname");
        Field<String> relkind = DSL.field(DSL.name("v", "relkind")).cast(SQLDataType.VARCHAR);
        Field<String> column = str("a", "attname");
        Field<String> table = str("t", "relname");
        var rows = ctx.selectDistinct(view, relkind, table, column)
            .from(cat("pg_depend", "d"))
            .join(cat("pg_class", "dc")).on(DSL.field(DSL.name("dc", "oid")).eq(DSL.field(DSL.name("d", "classid"))))
            .join(cat("pg_class", "dr")).on(DSL.field(DSL.name("dr", "oid")).eq(DSL.field(DSL.name("d", "refclassid"))))
            .join(cat("pg_rewrite", "r")).on(DSL.field(DSL.name("r", "oid")).eq(DSL.field(DSL.name("d", "objid"))))
            .join(cat("pg_class", "v")).on(DSL.field(DSL.name("v", "oid")).eq(DSL.field(DSL.name("r", "ev_class"))))
            .join(cat("pg_namespace", "vn")).on(DSL.field(DSL.name("vn", "oid")).eq(DSL.field(DSL.name("v", "relnamespace"))))
            .join(cat("pg_class", "t")).on(DSL.field(DSL.name("t", "oid")).eq(DSL.field(DSL.name("d", "refobjid"))))
            .join(cat("pg_namespace", "tn")).on(DSL.field(DSL.name("tn", "oid")).eq(DSL.field(DSL.name("t", "relnamespace"))))
            .join(cat("pg_attribute", "a")).on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("t", "oid")))
                .and(DSL.field(DSL.name("a", "attnum")).eq(DSL.field(DSL.name("d", "refobjsubid")))))
            .where(str("dc", "relname").eq("pg_rewrite")).and(str("dr", "relname").eq("pg_class"))
            .and(str("vn", "nspname").eq("nexus")).and(str("tn", "nspname").eq("nexus"))
            .and(relkind.in("v", "m"))
            .and(DSL.field(DSL.name("v", "oid")).ne(DSL.field(DSL.name("t", "oid"))))
            .and(column.in(nameColumns))
            .orderBy(view, column)
            .fetch();
        for (var r : rows) {
            String kind = "m".equals(r.get(relkind)) ? "matview" : "view";
            live.putIfAbsent(kind + "\tnexus." + r.get(view), "depends on " + r.get(table) + "." + r.get(column));
        }
    }

    private void liveFunctions(DSLContext ctx) {
        Field<String> name = str("p", "proname");
        Field<String> src = str("p", "prosrc");
        var rows = ctx.select(name, src)
            .from(cat("pg_proc", "p"))
            .join(cat("pg_namespace", "n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
            .join(cat("pg_language", "l")).on(DSL.field(DSL.name("l", "oid")).eq(DSL.field(DSL.name("p", "prolang"))))
            .where(str("n", "nspname").eq("nexus"))
            .and(DSL.field(DSL.name("p", "prokind")).cast(SQLDataType.VARCHAR).eq("f"))
            .and(str("l", "lanname").in("sql", "plpgsql"))
            .orderBy(name)
            .fetch();
        for (var r : rows) {
            String original = r.get(src);
            String blanked = blank(original);
            String[] origLines = original.split("\n", -1);
            String[] blankLines = blanked.split("\n", -1);
            for (int i = 0; i < blankLines.length; i++) {
                for (String col : nameColumns) {
                    if (word(col).matcher(blankLines[i]).find()) {
                        live.putIfAbsent("function\tnexus." + r.get(name),
                            "prosrc line " + (i + 1) + " (" + col + "): " + origLines[i].strip());
                        break;
                    }
                }
                if (live.containsKey("function\tnexus." + r.get(name))) {
                    break;
                }
            }
        }
    }

    private void liveTriggers(DSLContext ctx) {
        // the (relation, attnum) of every name column, to read tgattr
        Map<String, String> nameAt = new LinkedHashMap<>();
        for (var r : ctx.select(str("c", "relname"), DSL.field(DSL.name("a", "attnum")).cast(SQLDataType.VARCHAR), str("a", "attname"))
                .from(cat("pg_attribute", "a"))
                .join(cat("pg_class", "c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("a", "attrelid"))))
                .join(cat("pg_namespace", "n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(str("n", "nspname").eq("nexus")).and(str("a", "attname").in(nameColumns))
                .fetch()) {
            nameAt.put(r.value1() + "#" + r.value2(), r.value3());
        }
        Field<String> tgname = str("t", "tgname");
        Field<String> relname = str("c", "relname");
        Field<String> tgattr = DSL.field(DSL.name("t", "tgattr")).cast(SQLDataType.VARCHAR);
        Field<String> def = DSL.function("pg_get_triggerdef", String.class, DSL.field(DSL.name("t", "oid")));
        var rows = ctx.select(tgname, relname, tgattr, def)
            .from(cat("pg_trigger", "t"))
            .join(cat("pg_class", "c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("t", "tgrelid"))))
            .join(cat("pg_namespace", "n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(str("n", "nspname").eq("nexus"))
            .and(DSL.field(DSL.name("t", "tgisinternal"), Boolean.class).isFalse())
            .and(DSL.field(DSL.name("t", "tgparentid")).cast(SQLDataType.BIGINT).eq(0L))
            .fetch();
        for (var r : rows) {
            String key = "trigger\tnexus." + r.get(relname) + "." + r.get(tgname);
            for (String attnum : r.get(tgattr).trim().split("\\s+")) {
                String col = nameAt.get(r.get(relname) + "#" + attnum);
                if (col != null) {
                    live.putIfAbsent(key, "tgattr includes " + col);
                }
            }
            String definition = r.get(def);
            int when = definition.indexOf(" WHEN (");
            if (when >= 0) {
                String clause = blank(definition.substring(when));
                for (String col : nameColumns) {
                    if (word(col).matcher(clause).find()) {
                        live.putIfAbsent(key, "WHEN clause names " + col);
                    }
                }
            }
        }
    }

    private void livePolicies(DSLContext ctx) {
        Field<String> policy = str("p", "policyname");
        Field<String> table = str("p", "tablename");
        Field<String> qual = str("p", "qual");
        Field<String> check = str("p", "with_check");
        var rows = ctx.select(policy, table, qual, check)
            .from(DSL.table(DSL.name("pg_catalog", "pg_policies")).as("p"))
            .where(str("p", "schemaname").eq("nexus"))
            .fetch();
        for (var r : rows) {
            for (String text : new String[] {r.get(qual), r.get(check)}) {
                if (text == null) {
                    continue;
                }
                for (String col : nameColumns) {
                    if (word(col).matcher(blank(text)).find()) {
                        live.putIfAbsent("policy\tnexus." + r.get(table) + "." + r.get(policy), "policy text names " + col);
                    }
                }
            }
        }
    }

    // ---- text helpers ------------------------------------------------------------------------------------------------

    private static Pattern word(String column) {
        return Pattern.compile("(?<![\\w$])" + Pattern.quote(column) + "(?![\\w$])");
    }

    /** Blanks {@code --} comments and single-quoted literals, keeping offsets and newlines (the census script's strip rule). */
    static String blank(String s) {
        StringBuilder out = new StringBuilder(s.length());
        int i = 0;
        int n = s.length();
        while (i < n) {
            if (s.startsWith("--", i)) {
                int j = s.indexOf('\n', i);
                j = j < 0 ? n : j;
                out.append(" ".repeat(j - i));
                i = j;
            } else if (s.charAt(i) == '\'') {
                int j = i + 1;
                while (j < n) {
                    if (s.charAt(j) == '\'' && j + 1 < n && s.charAt(j + 1) == '\'') {
                        j += 2;
                        continue;
                    }
                    if (s.charAt(j) == '\'') {
                        break;
                    }
                    j++;
                }
                out.append('\'');
                for (int k = i + 1; k < Math.min(j, n); k++) {
                    out.append(s.charAt(k) == '\n' ? '\n' : ' ');
                }
                out.append('\'');
                i = j + 1;
            } else {
                out.append(s.charAt(i));
                i++;
            }
        }
        return out.toString();
    }

    // ---- the file side -----------------------------------------------------------------------------------------------

    private static List<String> nameColumnsOfTheChecksList() throws IOException {
        TreeSet<String> out = new TreeSet<>();
        for (String line : resourceLines(CHECKS_LIST)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) {
                continue;
            }
            String col = line.split("\t", -1)[1];
            if (!col.equals("-")) {
                out.add(col);
            }
        }
        return new ArrayList<>(out);
    }

    private void readCensus() throws IOException {
        for (String line : resourceLines(CENSUS)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("kind\t")) {
                continue;
            }
            String[] f = line.split("\t", -1);
            census.add(f[0] + "\t" + f[1]);
        }
    }

    private static List<String> resourceLines(String path) throws IOException {
        try (InputStream in = Rdr229P35bReaderCensusLiveCatalogTest.class.getClassLoader().getResourceAsStream(path)) {
            assertThat(in).as("%s is on the classpath", path).isNotNull();
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            List<String> lines = new ArrayList<>();
            for (String l = reader.readLine(); l != null; l = reader.readLine()) {
                lines.add(l);
            }
            return lines;
        }
    }
}
