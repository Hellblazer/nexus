// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.vectors.Rdr229ChunksFk;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.TreeMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21) CENSUS of the validation and constraint statements the per-leaf change touches, pinned to the bound each one runs
 * under (Sam's ruling, T2 {@code nexus/sam-ruling-validate-bounds-lock-wait-only-2026-11-23}: a validation that blocks no writer has a lock wait
 * only; a step that blocks writers keeps its tight bound). The rule is the test: every text with a {@code VALIDATE CONSTRAINT} runs with
 * {@link DataMove#NO_STATEMENT_BOUND_MS}; every text with an {@code ADD CONSTRAINT} runs under {@link Rdr229SwapMove#STATEMENT_TIMEOUT_MS}; no text
 * has both. A new statement of either kind in any of these texts changes a count and fails here until it is classified.
 *
 * <pre>
 * move (registry row)                          statement                                     write-blocking  lock (PostgreSQL 17, sql-altertable / explicit-locking)                      bound
 * rdr229-swap-chunks on_complete               3 x ADD CONSTRAINT .. NOT VALID (child keys)  yes             SHARE ROW EXCLUSIVE on the child, on chunks and its leaves, for an instant   1400 ms, 700 ms lock
 * rdr229-swap-chunks on_complete               1 x ADD CONSTRAINT .. NOT VALID per leaf      yes             SHARE ROW EXCLUSIVE on the leaf and on the registry, for an instant          1400 ms
 * rdr229-fkvalidate-&lt;child&gt;-chunk-fk (x3)       1 x VALIDATE CONSTRAINT                       no              SHARE UPDATE EXCLUSIVE on the child, ROW SHARE on chunks                     none, 5 s lock wait
 * rdr229-fkvalidate-chunks-leaf-&lt;leaf&gt;         1 x VALIDATE CONSTRAINT                       no              SHARE UPDATE EXCLUSIVE on the leaf, ROW SHARE on the registry                none, 5 s lock wait
 * rdr229-fkadopt-chunks on_complete            1 x ADD CONSTRAINT (parent, adopts leaf keys) yes             SHARE ROW EXCLUSIVE on chunks and every leaf, ACCESS EXCLUSIVE on the registry   1400 ms, 700 ms lock
 * rdr229-fkadopt-chunks on_complete            2 x ADD CONSTRAINT .. NOT VALID (late leaf)   yes             SHARE ROW EXCLUSIVE on the leaf and on the registry, for an instant          1400 ms
 * rdr229-stamp-&lt;table&gt; (booted rows)            VALIDATE CONSTRAINT (P3.1 CHECKs)             no              SHARE UPDATE EXCLUSIVE on the table (and its leaves)                         none, 5 s lock wait
 * rdr229-fkvalidate-&lt;table&gt; (booted rows)       VALIDATE CONSTRAINT (P3.3a id twins)          no              SHARE UPDATE EXCLUSIVE on the table, ROW SHARE on the registry               none, 5 s lock wait
 * </pre>
 *
 * The parent ADD's ACCESS EXCLUSIVE on the registry and the leaf ADD's locks are the PostgreSQL 17.11 observations of T1 {@code pg17-fk-adopt} (pg_locks
 * while the statement's transaction was open). The booted rows are what the real walk leaves (vectors-070, -072 and -081-1).
 */
class Rdr229ValidationBoundsCensusIntegrationTest extends DataMoveIntegrationBase {

    private record Booted(String label, int bound, String onComplete) { }

    private static final List<Booted> booted = new ArrayList<>();

    private static final Pattern DOLLAR_BODY = Pattern.compile("\\$(oc|aoc|ltpl|lver|lv|atpl|aver)\\$.*?\\$\\1\\$", Pattern.DOTALL);
    private static final Pattern INSERT_BOUND = Pattern.compile("'nexus', '([a-z_]+)', (\\d+), (\\d+), (\\d+),");

    @BeforeAll
    void captureTheWalk() {
        Field<String> label = DSL.field(DSL.name("label"), String.class);
        ctx().select(label, DSL.field(DSL.name("statement_timeout_ms"), Integer.class), DSL.field(DSL.name("on_complete"), String.class))
            .from(table("nexus", "data_moves")).orderBy(label)
            .fetch().forEach(r -> booted.add(new Booted(r.value1(), r.value2(), r.value3())));
    }

    private static int count(String needle, String text) {
        int n = 0;
        for (int i = text.indexOf(needle); i >= 0; i = text.indexOf(needle, i + needle.length())) {
            n++;
        }
        return n;
    }

    private static List<String> bodies(String tag, String text) {
        List<String> out = new ArrayList<>();
        Matcher m = Pattern.compile("\\$" + tag + "\\$(.*?)\\$" + tag + "\\$", Pattern.DOTALL).matcher(text);
        while (m.find()) {
            out.add(m.group(1));
        }
        return out;
    }

    /** One text and what it runs under. */
    private record Text(String move, String text, int boundMs) { }

    private static void rule(Text t, int add, int validate) {
        assertThat(count("ADD CONSTRAINT", t.text())).as("%s: ADD CONSTRAINT statements", t.move()).isEqualTo(add);
        assertThat(count("VALIDATE CONSTRAINT", t.text())).as("%s: VALIDATE CONSTRAINT statements", t.move()).isEqualTo(validate);
        assertThat(t.text()).as("%s: no index build", t.move()).doesNotContain("CREATE INDEX").doesNotContain("CREATE UNIQUE INDEX");
        if (validate > 0) {
            assertThat(add).as("%s: a text does not both add and validate", t.move()).isZero();
            assertThat(t.boundMs()).as("%s: a validation blocks no writer, so it has a lock wait only", t.move()).isEqualTo(DataMove.NO_STATEMENT_BOUND_MS);
        }
        if (add > 0) {
            assertThat(t.boundMs()).as("%s: a statement that takes SHARE ROW EXCLUSIVE keeps the tight bound", t.move())
                .isEqualTo(Rdr229SwapMove.STATEMENT_TIMEOUT_MS);
        }
    }

    @Test
    void theChunksSwapAndTheMovesItRegistersAreClassified() {
        String swap = Rdr229SwapMove.spec("chunks").onComplete();
        String residue = DOLLAR_BODY.matcher(swap).replaceAll("''");   // the swap's own statements, without the texts it registers

        // the registrations: three child validations and the per-leaf validation (no bound) and the adoption move (tight)
        Map<String, List<Integer>> bounds = new LinkedHashMap<>();
        Matcher b = INSERT_BOUND.matcher(residue);
        while (b.find()) {
            bounds.computeIfAbsent(b.group(1), k -> new ArrayList<>()).add(Integer.parseInt(b.group(4)));
            assertThat(Integer.parseInt(b.group(3))).as("sleep_ms of a registered move").isGreaterThanOrEqualTo(0);
        }
        assertThat(bounds.keySet()).as("every registration the swap makes, by driving table")
            .containsExactlyInAnyOrder("catalog_document_chunks", "chunk_orphaned_at", "topic_assignments", "catalog_collections");
        for (String child : Rdr229SwapMove.CHILD_TABLES) {
            assertThat(bounds.get(child)).as("%s: its validation move carries no statement bound", child).containsExactly(DataMove.NO_STATEMENT_BOUND_MS);
        }
        assertThat(bounds.get("catalog_collections")).as("the per-leaf validation move (no bound) and the adoption move (tight), in that order")
            .containsExactly(DataMove.NO_STATEMENT_BOUND_MS, Rdr229SwapMove.STATEMENT_TIMEOUT_MS);

        // the swap's own on_complete: the three child keys and the leaf key, every one NOT VALID, none validated
        Text own = new Text("rdr229-swap-chunks", residue, Rdr229SwapMove.spec("chunks").statementTimeoutMs());
        rule(own, 4, 0);
        assertThat(count("NOT VALID", residue)).as("every key the swap adds is NOT VALID").isEqualTo(4);
        assertThat(residue).as("and the composite is not added by the swap").doesNotContain(Rdr229ChunksFk.PARENT_CONSTRAINT + " " + Rdr229ChunksFk.FK_DEFINITION);

        // the three child validation moves
        List<String> children = bodies("oc", swap);
        assertThat(children).as("three child validation moves").hasSize(3);
        for (String c : children) {
            rule(new Text("child validation", c, bounds.get(Rdr229SwapMove.CHILD_TABLES.get(0)).get(0)), 0, 1);
        }

        // the per-leaf validation move: one VALIDATE statement, written once and formatted per leaf
        String swapWithoutAdopt = Pattern.compile("\\$aoc\\$.*?\\$aoc\\$", Pattern.DOTALL).matcher(swap).replaceAll("''");
        List<String> leafTemplates = bodies("lv", swapWithoutAdopt);
        assertThat(leafTemplates).as("one per-leaf validation text in the swap (the adoption move's copy is inside its own text)").hasSize(1);
        rule(new Text("per-leaf validation", leafTemplates.get(0), bounds.get("catalog_collections").get(0)), 0, 1);

        // the adoption move: the leaf key NOT VALID (a late leaf) and the parent key, validated by adopting
        List<String> adopts = bodies("aoc", swap);
        assertThat(adopts).as("one adoption move").hasSize(1);
        String adopt = adopts.get(0);
        rule(new Text("rdr229-fkadopt-chunks", DOLLAR_BODY.matcher(adopt).replaceAll("''"), bounds.get("catalog_collections").get(1)), 3, 0);
        assertThat(count("NOT VALID", DOLLAR_BODY.matcher(adopt).replaceAll("''"))).as("only a late leaf's key is NOT VALID (the reconcile runs twice: before the lock and under it)")
            .isEqualTo(2);
        assertThat(adopt).as("the parent key is added with the one definition the leaves use")
            .contains("ADD CONSTRAINT " + Rdr229ChunksFk.PARENT_CONSTRAINT + " " + Rdr229ChunksFk.FK_DEFINITION + ";");
        assertThat(adopt).as("and a leaf key too").contains(Rdr229ChunksFk.FK_DEFINITION + " NOT VALID");
        assertThat(adopt).as("the parent key is added only after the registry-wide lock and the leaf check").containsSubsequence(
            "LOCK TABLE nexus.chunks IN SHARE ROW EXCLUSIVE MODE", "IF pending THEN", "ADD CONSTRAINT " + Rdr229ChunksFk.PARENT_CONSTRAINT);
    }

    @Test
    void everyKeyTheChangeAddsUsesTheOneDefinition() {
        String swap = Rdr229SwapMove.spec("chunks").onComplete();
        // the leaf key (in the swap and in the adoption move) and the parent key spell FK_DEFINITION, so PostgreSQL adopts the leaf keys
        assertThat(count(Rdr229ChunksFk.FK_DEFINITION, DOLLAR_BODY.matcher(swap).replaceAll("''"))).as("the swap's leaf key").isEqualTo(1);
        assertThat(count(Rdr229ChunksFk.FK_DEFINITION, bodies("aoc", swap).get(0))).as("the adoption move's leaf key (reconciled twice) and parent key").isEqualTo(3);
        assertThat(Rdr229ChunksFk.FK_DEFINITION).as("RESTRICT on delete, the default on update: what the match check reads")
            .contains("ON DELETE RESTRICT").doesNotContain("ON UPDATE").doesNotContain("DEFERRABLE").doesNotContain("MATCH");
        assertThat(Rdr229ChunksFk.hasValidatedMatch("x")).as("the match check compares the adopted key's actions the same way")
            .contains("confdeltype = 'r'").contains("confupdtype = 'a'").contains("confmatchtype = 's'").contains("NOT k.condeferrable")
            .contains("k.convalidated").contains("k.conkey =").contains("k.confkey =");
    }

    @Test
    void everyBootedMoveThatValidatesHasNoStatementBound_andNoBootedMoveAddsAConstraint() {
        assertThat(booted).as("non-vacuity: the walk registered its moves").hasSizeGreaterThanOrEqualTo(21);
        List<String> validating = new ArrayList<>();
        for (Booted m : booted) {
            String text = m.onComplete() == null ? "" : m.onComplete();
            int validate = count("VALIDATE CONSTRAINT", text);
            int add = count("ADD CONSTRAINT", text);
            if (validate == 0 && add == 0) {
                continue;
            }
            assertThat(m.label()).as("a booted move with a constraint statement is one of the two families vectors-081-1 classifies")
                .matches("rdr229-(stamp|fkvalidate)-.*");
            rule(new Text(m.label(), text, m.bound()), add, validate);
            assertThat(validate).as("%s: validates", m.label()).isGreaterThanOrEqualTo(1);
            validating.add(m.label());
        }
        assertThat(validating.stream().filter(l -> l.startsWith("rdr229-stamp-")).count()).as("the stamp moves (15: the queue's table is absent)").isEqualTo(15L);
        assertThat(validating.stream().filter(l -> l.startsWith("rdr229-fkvalidate-")).count()).as("the P3.3a twin validation moves (6: same)").isEqualTo(6L);
    }

    /**
     * Every {@code ADD CONSTRAINT} and {@code VALIDATE CONSTRAINT} in the engine's main sources, by file, with the exact count of each (comments
     * and javadoc included: a change to either number is a reason to look again) and why the file is allowed. A statement that is a
     * data move's text is classified by the tests above; the rest is not a move and is named here so a new one cannot arrive unclassified.
     * The four boot-time admin validators keep their 30-minute bound (linda, 2026-11-23: not this change's).
     */
    @Test
    void everyConstraintStatementInTheMainSourcesIsAllowlistedWithItsExactCount() throws Exception {
        Map<String, String> allowed = new LinkedHashMap<>();
        // move texts, classified by the tests above
        allowed.put("datamove/DataMove.java", "0,1 move: javadoc of the no-bound sentinel");
        allowed.put("vectors/Rdr229ChunksFk.java", "2,1 move: the leaf and parent ADD (tight bound) and the leaf VALIDATE (no bound)");
        allowed.put("vectors/Rdr229SwapMove.java", "1,1 move: the child key ADD (tight bound) and the child VALIDATE (no bound)");
        // not moves
        allowed.put("vectors/LeafIndexPass.java", "5,0 runtime pass: ADD CONSTRAINT UNIQUE of the id keys under a DDL lock timeout (P3.3b)");
        allowed.put("vectors/ChunkConstraintAdmin.java", "1,1 boot-time admin validator: 30 min bound, linda 2026-11-23");
        allowed.put("db/ManifestCheckAdmin.java", "0,1 boot-time admin validator: 30 min bound, linda 2026-11-23");
        allowed.put("db/CatalogTtlCheckAdmin.java", "1,1 boot-time admin validator: 30 min bound, linda 2026-11-23");
        allowed.put("db/ChashConstraintValidator.java", "0,4 boot-time admin validator: 30 min bound, linda 2026-11-23");
        allowed.put("db/ManifestCheck.java", "1,0 template of the manifest CHECK, added NOT VALID");
        allowed.put("db/AdminDdlSession.java", "0,1 comment");
        allowed.put("db/ChashConformanceConstraints.java", "0,1 comment");
        allowed.put("db/SchemaMigrator.java", "0,3 comments and a log line about changesets that validate");
        allowed.put("http/CatalogTtlCheckHandler.java", "0,1 comment");
        allowed.put("http/ManifestCheckHandler.java", "0,1 comment");
        allowed.put("http/ValidateConstraintsHandler.java", "0,1 comment");
        allowed.put("http/ValidateConstraintsJobs.java", "0,1 comment");
        allowed.put("http/VersionHandler.java", "0,2 comments");
        allowed.put("Main.java", "0,1 comment");
        allowed.put("NexusService.java", "0,1 comment");

        Map<String, String> found = new TreeMap<>();
        Path root = Path.of("src/main/java/dev/nexus/service");
        try (var files = Files.walk(root)) {
            for (Path f : files.filter(x -> x.toString().endsWith(".java")).toList()) {
                String text = Files.readString(f);
                int add = count("ADD CONSTRAINT", text);
                int validate = count("VALIDATE CONSTRAINT", text);
                if (add + validate > 0) {
                    found.put(root.relativize(f).toString().replace('\\', '/'), add + "," + validate);
                }
            }
        }
        Map<String, String> expected = new TreeMap<>();
        allowed.forEach((k, v) -> expected.put(k, v.substring(0, v.indexOf(' '))));
        assertThat(found).as("non-vacuity: the sources were read").hasSizeGreaterThanOrEqualTo(15);
        assertThat(found).as("every file with an ADD CONSTRAINT or VALIDATE CONSTRAINT, with the exact counts, is allowlisted above").isEqualTo(expected);
    }
}
