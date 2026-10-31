// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Routines.catalogSourceFormat;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.62: the derived {@code source_format} rule has ONE SQL definition, {@code
 * nexus.catalog_source_format(content_type, file_path)} (vectors-043), and the Java copy the label stage keeps
 * ({@link ManifestLabelStage#derivedFormat}) must agree with it on every input.
 *
 * <p>Three things are pinned:
 * <ul>
 *   <li>PARITY. The same (content_type, file_path) rows go through the Java rule and through the function, and the
 *       answers are equal. The rows are the edge cases the gate named (a {@code .md} file under catalog type {@code
 *       code}, an upper-case {@code .PDF}, a directory dot, no extension, an unknown type) and the ones around them
 *       (a dotfile, a double extension, a trailing dot or slash, NULL arguments, a type in the wrong case). The
 *       matrix must reach every answer the rule has, so a matrix that drifted into one branch cannot pass quietly.</li>
 *   <li>CATALOG PROPERTIES. LANGUAGE sql, IMMUTABLE, PARALLEL SAFE, SECURITY INVOKER, no SET clause, scalar,
 *       executable by the service role: what lets the planner inline it and a GENERATED column (nexus-6pfgb.23)
 *       call it.</li>
 *   <li>NO SECOND COPY. The three {@code manifest_search_<dim>} bodies call the function in code (comments removed)
 *       and no longer hold the inline CASE.</li>
 * </ul>
 * Everything database-side is typed jOOQ or a catalog read, so this class adds nothing to the raw-SQL ratchet.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogSourceFormatParityTest {

    private static final String SVC_ROLE = "svc_src_format";
    private static final String SVC_PASS = "svc_src_format_pass";

    /** One row of the matrix; {@code null} stands for SQL NULL on the SQL side and a null reference in Java. */
    private record In(String contentType, String filePath) {
        @Override
        public String toString() {
            return "(" + contentType + ", " + filePath + ")";
        }
    }

    private static final List<In> MATRIX = List.of(
        // The extension is read first: a markdown file under any catalog type is markdown, a .pdf is pdf.
        new In("code", "notes/readme.md"),
        new In("code", "src/alpha.py"),
        new In("prose", "notes/d.md"),
        new In("rdr", "docs/rdr/x.MARKDOWN"),
        new In("paper", "papers/c.pdf"),
        new In("code", "src/scan.PDF"),
        new In("knowledge", "x.Md"),
        // No extension: the catalog type speaks.
        new In("code", "Makefile"),
        new In("prose", "LICENSE"),
        new In("rdr", ""),
        new In("pdf", ""),
        new In("paper", ""),
        new In("", "papers/C.PDF"),
        // A directory dot is not an extension.
        new In("", "dir.pdf/file"),
        new In("prose", "dir.md/file"),
        new In("code", "dir.pdf/file"),
        // A dotfile has no extension; a double extension is its last one.
        new In("", ".gitignore"),
        new In("", ".pdf"),
        new In("", "dir/.pdf"),
        new In("", "a.tar.pdf"),
        new In("", "a.pdf.gz"),
        new In("prose", "a.md.bak"),
        // Trailing dot, trailing slash, a stray newline, non-ASCII before the dot.
        new In("", "a."),
        new In("", "a.pdf/"),
        new In("", "a.pdf\n"),
        new In("", "résumé.PDF"),
        // Unknown types and absent arguments derive none, except where the extension answers.
        new In("knowledge", ""),
        new In("docs", "a.txt"),
        new In("", ""),
        new In(null, null),
        new In("code", null),
        new In(null, "a.md"),
        new In(null, "a.pdf"),
        new In("Code", "x"),
        new In("PDF", ""),
        new In(" code", ""));

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    private String sql(DSLContext ctx, In in) {
        return ctx.select(catalogSourceFormat(
                DSL.val(in.contentType(), SQLDataType.VARCHAR), DSL.val(in.filePath(), SQLDataType.VARCHAR)))
            .fetchOne(0, String.class);
    }

    @Test
    void javaAndSqlAgreeOnEveryRowOfTheMatrix_andTheMatrixReachesEveryAnswer() throws Exception {
        List<String> disagreements = new ArrayList<>();
        Set<String> answers = new LinkedHashSet<>();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (In in : MATRIX) {
                String java = ManifestLabelStage.derivedFormat(in.contentType(), in.filePath());
                String db = sql(ctx, in);
                answers.add(String.valueOf(java));
                if (!String.valueOf(java).equals(String.valueOf(db))) {
                    disagreements.add(in + ": java=" + java + " sql=" + db);
                }
            }
        }
        assertThat(disagreements).as("rows where the Java rule and nexus.catalog_source_format differ").isEmpty();
        // Non-vacuity: the matrix exercises each branch of the rule, so an agreement is not an agreement on 'null'.
        assertThat(answers).as("answers the matrix reached").containsExactlyInAnyOrder(
            "pdf", "markdown", "code", "prose", "null");
    }

    @Test
    void theNamedEdgeCases_haveTheirExpectedAnswerOnTheSqlSide() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(sql(ctx, new In("code", "notes/readme.md"))).as(".md under catalog type code").isEqualTo("markdown");
            assertThat(sql(ctx, new In("code", "src/scan.PDF"))).as("upper-case .PDF").isEqualTo("pdf");
            assertThat(sql(ctx, new In("", "dir.pdf/file"))).as("a directory dot").isNull();
            assertThat(sql(ctx, new In("code", "Makefile"))).as("no extension, catalog type code").isEqualTo("code");
            assertThat(sql(ctx, new In("knowledge", ""))).as("an unknown type").isNull();
            assertThat(sql(ctx, new In(null, null))).as("NULL arguments").isNull();
        }
    }

    @Test
    void functionIsSqlImmutableParallelSafeSecurityInvokerAndExecutableByTheServiceRole() throws Exception {
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc"));
        var lang = DSL.table(DSL.name("pg_catalog", "pg_language"));
        var nsp = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
        try (Connection su = pg.createConnection("")) {
            var rec = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("l", "lanname"), String.class),
                        DSL.field(DSL.name("p", "provolatile"), String.class),
                        DSL.field(DSL.name("p", "prosecdef"), Boolean.class),
                        DSL.cast(DSL.field(DSL.name("p", "proconfig")), SQLDataType.VARCHAR),
                        DSL.field(DSL.name("p", "proretset"), Boolean.class),
                        DSL.field(DSL.name("p", "proparallel"), String.class),
                        DSL.function("has_function_privilege", Boolean.class, DSL.inline(SVC_ROLE),
                            DSL.field(DSL.name("p", "oid"), Object.class), DSL.inline("EXECUTE")))
                .from(proc.as("p"))
                .join(lang.as("l")).on(DSL.field(DSL.name("l", "oid")).eq(DSL.field(DSL.name("p", "prolang"))))
                .join(nsp.as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")
                    .and(DSL.field(DSL.name("p", "proname"), String.class).eq("catalog_source_format")))
                .fetch();
            assertThat(rec).as("exactly one function of that name (no overload to inline the wrong one)").hasSize(1);
            var r = rec.get(0);
            assertThat(r.value1()).as("LANGUAGE sql, the only language the planner inlines").isEqualTo("sql");
            assertThat(r.value2()).as("IMMUTABLE, what a GENERATED column needs").isEqualTo("i");
            assertThat(r.value3()).as("SECURITY INVOKER: a SECURITY DEFINER function is never inlined").isFalse();
            assertThat(r.value4()).as("no SET clause: a function with proconfig is never inlined").isNull();
            assertThat(r.value5()).as("scalar, not set-returning").isFalse();
            assertThat(r.value6()).as("PARALLEL SAFE").isEqualTo("s");
            assertThat(r.value7()).as("nexus_svc can execute it").isTrue();
        }
    }

    private static final Pattern SQL_LINE_COMMENT = Pattern.compile("--[^\n]*");

    @Test
    void theThreeManifestSearchBodiesCallTheFunction_andHoldNoSecondCopyOfTheRule() throws Exception {
        var src = DSL.field(DSL.name("p", "prosrc"), String.class);
        try (Connection su = pg.createConnection("")) {
            for (String proname : List.of("manifest_search_384", "manifest_search_768", "manifest_search_1024")) {
                List<String> bodies = DSL.using(su, SQLDialect.POSTGRES).select(src)
                    .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
                    .where(DSL.field(DSL.name("p", "proname"), String.class).eq(proname))
                    .fetch(src);
                assertThat(bodies).as("non-vacuity: exactly one %s", proname).hasSize(1);
                String body = SQL_LINE_COMMENT.matcher(bodies.get(0)).replaceAll("");
                assertThat(body).as(proname).contains("nexus.catalog_source_format(d.content_type, d.file_path)");
                assertThat(body).as(proname + " must not open-code the extension rule")
                    .doesNotContain("substring(d.file_path").doesNotContain("'markdown'");
            }
        }
    }
}
