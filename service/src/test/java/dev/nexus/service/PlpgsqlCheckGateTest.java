// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.images.builder.ImageFromDockerfile;
import org.testcontainers.utility.DockerImageName;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 (nexus-3wh8d.8): a static-analysis gate over the nexus schema's PL/pgSQL routines, run with the
 * {@code plpgsql_check} extension. PostgreSQL checks a plpgsql body against its tables only when the body runs,
 * so a write site that missed the {@code embedding_model} column or kept a three-column conflict target would
 * surface only when some path executed it (the RDR's "a write site missed in the rewrite" failure mode). This
 * gate catches an arity mismatch or a conflict target that matches no unique constraint at build time. It cannot
 * see an INSERT that omits a NOT NULL column (the omitted column is not a static error), so a writer that never
 * lists {@code embedding_model} is caught only by the runtime write-family tests.
 *
 * <p>The database is the FULL master changelog walked into an image built {@code FROM} the engine tests' own
 * pgvector image plus the {@code postgresql-17-plpgsql-check} package, then {@code CREATE EXTENSION
 * plpgsql_check}, so it holds only the LIVE body of every function. The shared {@link PgContainerHelper#IMAGE}
 * is not changed; this class has its own container.
 *
 * <ol>
 *   <li>Every function {@code vectors-030-1} defines or redefines: zero findings at any level (warnings
 *       included), except the findings in {@link #ALLOWED_IN_VECTORS_030}, each with its reason.</li>
 *   <li>Every other live plpgsql routine in the nexus schema: zero findings at level {@code error}. There is no
 *       ratchet over legacy warnings.</li>
 * </ol>
 */
class PlpgsqlCheckGateTest {

    private static final String ADMIN = "nexus_admin_plcheck";
    private static final String ADMIN_PASS = "nexus_admin_plcheck_pass";

    /** A finding accepted in a vectors-030 function, with why. A null field matches anything; an entry that matches nothing fails the gate. */
    record Allowed(String function, String level, String messageContains, String reason) {
        boolean matches(PlpgsqlCheck.Finding f) {
            return (function == null || function.equals(f.function()))
                && (level == null || level.equals(f.level()))
                && (messageContains == null || f.message().contains(messageContains));
        }
    }

    /** Findings accepted in the functions vectors-030-1 defines, one by one. */
    static final List<Allowed> ALLOWED_IN_VECTORS_030 = List.of(
        // The three GC/reaper movers create a TEMP TABLE inside the body and read it afterwards. The relation does
        // not exist when the analyser runs, so every later reference is reported. These are the bodies' own run-time
        // tables (vectors-022, vectors-024), not missing objects. The analyser skips the statements over those
        // tables, so a column dropped from the INSERT list of one of these three movers is NOT caught here: they
        // are covered by their runtime tests (GcQuarantineOrphansBoundedTest, PgVectorRepositoryGcQuarantineTest,
        // ChunkReaperIntegrationTest).
        new Allowed("gc_quarantine_orphans", "error", "_wbfpw16_victim", "run-time TEMP TABLE the body creates"),
        new Allowed("gc_quarantine_orphans_bounded", "error", "_a6mon_victim", "run-time TEMP TABLE the body creates"),
        new Allowed("reaper_quarantine_chunks", "error", "_x9_victim", "run-time TEMP TABLE the body creates"),
        // p_quarantined_at is used in each of these three, inside the INSERT ... SELECT that reads the temp table
        // the analyser cannot resolve; it skips the statement's expressions and so sees no use.
        new Allowed("gc_quarantine_orphans", "warning extra", "unused parameter \"p_quarantined_at\"", "used in the statement over the run-time temp table"),
        new Allowed("gc_quarantine_orphans_bounded", "warning extra", "unused parameter \"p_quarantined_at\"", "used in the statement over the run-time temp table"),
        new Allowed("reaper_quarantine_chunks", "warning extra", "unused parameter \"p_quarantined_at\"", "used in the statement over the run-time temp table"),
        // Legacy lines carried verbatim from catalog-043 and its predecessors: a text, integer or bigint
        // assigned from an expression of a near type. A cast would change a body this changeset otherwise leaves as it was.
        new Allowed("gc_restore_rereferenced", "performance", "target type is different type than source type", "legacy line carried verbatim (catalog-043)"),
        new Allowed("gc_restore_rereferenced_bounded", "performance", "target type is different type than source type", "legacy line carried verbatim (catalog-043)"),
        // The privilege keyword comes from aclexplode and the grantee goes through quote_ident; neither is caller text.
        new Allowed("partition_copy_access", "security", "text type variable is not sanitized", "privilege keyword from aclexplode, grantee quote_ident'ed"));

    static PostgreSQLContainer<?> pg;
    static List<PlpgsqlCheck.Finding> findings;
    static long buildMillis;

    @BeforeAll
    static void walkAndCheck() throws Exception {
        long t0 = System.nanoTime();
        var image = new ImageFromDockerfile()
            .withDockerfileFromBuilder(b -> b
                .from(PgContainerHelper.IMAGE)
                .run("apt-get update && apt-get install -y --no-install-recommends postgresql-17-plpgsql-check"
                    + " && rm -rf /var/lib/apt/lists/*")
                .build());
        String imageName = image.get();
        buildMillis = (System.nanoTime() - t0) / 1_000_000;
        pg = new PostgreSQLContainer<>(DockerImageName.parse(imageName).asCompatibleSubstituteFor("postgres"))
            .withDatabaseName("postgres").withUsername("postgres").withPassword("postgres")
            .withUrlParam("sslmode", "disable");
        pg.start();
        Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        try (var ds = new HikariDataSource(cfg)) {
            SchemaMigrator.migrate(ds);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.runSuperuserDdl(su, "CREATE EXTENSION plpgsql_check");
            findings = PlpgsqlCheck.findFindings(su, List.of("nexus"));
        }
    }

    @AfterAll
    static void stop() {
        if (pg != null) pg.stop();
    }

    /** The nexus functions vectors-030-1 creates or replaces: the part-A objects and every step 7.8 redefinition. */
    static java.util.Set<String> vectors030Functions() throws IOException {
        Path xml = Path.of("src", "main", "resources", "db", "changelog", "vectors-030-model-tenant-partition-functions.xml");
        String text = Files.readString(xml, StandardCharsets.UTF_8);
        Matcher m = Pattern.compile("CREATE\\s+OR\\s+REPLACE\\s+FUNCTION\\s+nexus\\.(\\w+)", Pattern.CASE_INSENSITIVE).matcher(text);
        var names = new TreeSet<String>();
        while (m.find()) names.add(m.group(1));
        return names;
    }

    @Test
    void measuredBaseline_isReported() throws Exception {
        Map<String, Long> byLevel = findings.stream().collect(Collectors.groupingBy(PlpgsqlCheck.Finding::level, TreeMap::new, Collectors.counting()));
        var v030 = vectors030Functions();
        long inV030 = findings.stream().filter(f -> v030.contains(f.function())).count();
        PartitionScratch.evidence("PLPGSQL_CHECK baseline: image build " + buildMillis + " ms; findings by level over schema nexus " + byLevel
            + "; in the " + v030.size() + " vectors-030 functions: " + inV030);
        System.out.println("PLPGSQL_CHECK baseline: image build " + buildMillis + " ms; findings by level " + byLevel + "; in vectors-030 functions: " + inV030);
        findings.stream().filter(f -> !v030.contains(f.function())).forEach(f -> System.out.println("PLPGSQL_CHECK legacy " + f.describe()));
        findings.stream().filter(f -> v030.contains(f.function())).forEach(f -> System.out.println("PLPGSQL_CHECK v030 " + f.describe()));
        assertThat(v030).as("the file defines the part-A and 7.8 functions").hasSizeGreaterThanOrEqualTo(20);
    }

    @Test
    void everyFunctionVectors030Defines_hasNoFindingsAtAnyLevel_beyondTheAllowList() throws Exception {
        var v030 = vectors030Functions();
        List<PlpgsqlCheck.Finding> mine = findings.stream().filter(f -> v030.contains(f.function())).toList();
        List<PlpgsqlCheck.Finding> unexpected = new ArrayList<>();
        boolean[] used = new boolean[ALLOWED_IN_VECTORS_030.size()];
        for (var f : mine) {
            boolean ok = false;
            for (int i = 0; i < ALLOWED_IN_VECTORS_030.size(); i++) {
                if (ALLOWED_IN_VECTORS_030.get(i).matches(f)) {
                    ok = true;
                    used[i] = true;
                }
            }
            if (!ok) unexpected.add(f);
        }
        assertThat(unexpected.stream().map(PlpgsqlCheck.Finding::describe).toList())
            .as("plpgsql_check findings in functions vectors-030-1 defines (all_warnings on)").isEmpty();
        for (int i = 0; i < used.length; i++) {
            assertThat(used[i]).as("allow-list entry matched nothing, so it is stale: %s", ALLOWED_IN_VECTORS_030.get(i)).isTrue();
        }
    }

    @Test
    void everyOtherLivePlpgsqlRoutineInNexus_hasNoFindingAtLevelError() throws Exception {
        var v030 = vectors030Functions();
        var errors = findings.stream().filter(f -> !v030.contains(f.function()))
            .filter(f -> f.level().equals("error")).map(PlpgsqlCheck.Finding::describe).toList();
        assertThat(errors).as("plpgsql_check errors in routines vectors-030-1 does not define").isEmpty();
    }
}
