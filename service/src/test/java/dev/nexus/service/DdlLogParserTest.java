// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * nexus-sz651: {@link DdlLogParser} counts the golden log exactly as tests/_ddl_counter.py does.
 * The golden files are byte-identical copies of tests/fixtures/ddl (a Python test checks that).
 */
class DdlLogParserTest {

    private static String resource(String name) throws IOException {
        try (InputStream in = DdlLogParserTest.class.getResourceAsStream("/ddl/" + name)) {
            assertThat(in).as("resource ddl/" + name).isNotNull();
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    /** Through text, so a long and an int of the same value compare equal. */
    private static JsonNode asJson(Object o) {
        try {
            ObjectMapper m = new ObjectMapper();
            return m.readTree(m.writeValueAsString(o));
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void the_golden_log_splits_boot_and_migration_from_tests_and_measures_the_wal() throws IOException {
        DdlLogParser.Parsed parsed = DdlLogParser.parse(resource("golden-pg-log.txt"));
        JsonNode expected = new ObjectMapper().readTree(resource("golden.json"));
        assertThat(asJson(DdlLogParser.tally(parsed.entries().stream().filter(DdlLogParser.Entry::boot).toList())))
            .isEqualTo(expected.get("boot"));
        assertThat(asJson(DdlLogParser.tally(
            parsed.entries().stream().filter(e -> e.beforeMark() && !e.boot()).toList())))
            .isEqualTo(expected.get("migration"));
        assertThat(asJson(DdlLogParser.tally(parsed.entries().stream().filter(e -> !e.beforeMark()).toList())))
            .isEqualTo(expected.get("tests"));
        assertThat(parsed.marks()).containsExactly("start", "ready", "end");
        assertThat(asJson(DdlLogParser.walSplit(parsed.stats()))).isEqualTo(expected.get("wal"));
    }

    @Test
    void a_phase_whose_marks_are_not_both_in_the_log_is_null_not_zero() {
        String log = "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl_mark: start wal=100 dbsize=5\n"
            + "2026-10-19 05:00:00.200 UTC [7] db=x LOG:  nx_ddl_mark: ready wal=250 dbsize=-1\n";
        java.util.Map<String, Long> wal = DdlLogParser.walSplit(DdlLogParser.parse(log).stats());
        assertThat(wal.get("migration_bytes")).isEqualTo(150L);
        assertThat(wal.get("tests_bytes")).isNull();
        assertThat(wal.get("total_bytes")).isNull();
        assertThat(wal.get("dbsize_growth_bytes")).isNull();
    }

    @Test
    void statements_that_fire_no_event_hold_no_claim_over_a_later_nested_event() {
        for (String sql : List.of(
            "GRANT svc TO bob", "REVOKE ADMIN OPTION FOR svc FROM bob", "GRANT CONNECT ON DATABASE d TO r",
            "REVOKE CONNECT ON DATABASE d FROM r", "GRANT SET ON PARAMETER work_mem TO r",
            "GRANT CREATE ON TABLESPACE pg_default TO r", "COMMENT ON DATABASE d IS 'x'",
            "COMMENT ON ROLE r IS 'x'", "CLUSTER t USING i", "REASSIGN OWNED BY a TO b",
            "ALTER ROLE r SET work_mem='5MB'", "DROP ROLE r")) {
            String log = "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: " + sql + "\n"
                + "2026-10-19 05:00:00.200 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n";
            assertThat(DdlLogParser.parse(log).entries()).as(sql).hasSize(2);
        }
    }

    @Test
    void statements_that_do_fire_an_event_are_still_one_command() {
        for (String sql : List.of(
            "GRANT SELECT ON t TO r", "REVOKE SELECT ON t FROM r", "GRANT USAGE ON SCHEMA public TO r",
            "GRANT ALL ON ALL TABLES IN SCHEMA public TO r", "COMMENT ON TABLE t IS 'x'",
            "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO r", "DROP OWNED BY r")) {
            String log = "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: " + sql + "\n"
                + "2026-10-19 05:00:00.101 UTC [7] db=x LOG:  nx_ddl: GRANT\n";
            assertThat(DdlLogParser.parse(log).entries()).as(sql).hasSize(1);
        }
    }

    @Test
    void reindex_is_never_logged_so_its_event_is_one_nested_command() {
        String log = "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl: REINDEX\n";
        List<DdlLogParser.Entry> entries = DdlLogParser.parse(log).entries();
        assertThat(entries).hasSize(1);
        assertThat(entries.get(0).nested()).isTrue();
        assertThat(entries.get(0).tag()).isEqualTo("REINDEX");
    }

    @Test
    void a_client_statement_and_its_event_are_one_command() {
        String log = "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: DROP TABLE a\n"
            + "2026-10-19 05:00:00.101 UTC [7] db=x LOG:  nx_ddl: DROP TABLE\n";
        List<DdlLogParser.Entry> entries = DdlLogParser.parse(log).entries();
        assertThat(entries).hasSize(1);
        assertThat(entries.get(0).nested()).isFalse();
        assertThat(entries.get(0).kind()).isEqualTo("drop");
    }

    @Test
    void an_event_with_no_client_statement_is_nested() {
        String log = "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n";
        List<DdlLogParser.Entry> entries = DdlLogParser.parse(log).entries();
        assertThat(entries).hasSize(1);
        assertThat(entries.get(0).nested()).isTrue();
        assertThat(entries.get(0).kind()).isEqualTo("create_table");
    }

    @Test
    void shared_object_ddl_and_failed_statements_hold_no_claim() {
        String log = "2026-10-19 05:00:00.100 UTC [7] db=x LOG:  statement: CREATE ROLE r\n"
            + "2026-10-19 05:00:00.200 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n"
            + "2026-10-19 05:00:00.300 UTC [8] db=x LOG:  statement: ALTER TABLE a ADD COLUMN c int\n"
            + "2026-10-19 05:00:00.301 UTC [8] db=x ERROR:  relation \"a\" does not exist\n"
            + "2026-10-19 05:00:00.400 UTC [8] db=x LOG:  nx_ddl: CREATE TABLE\n";
        assertThat(DdlLogParser.parse(log).entries()).extracting(DdlLogParser.Entry::nested)
            .containsExactly(false, true, false, true);
    }

    @Test
    void a_log_that_never_reached_ready_is_all_before_the_mark() throws IOException {
        String log = String.join("\n", resource("golden-pg-log.txt").lines()
            .filter(l -> !l.contains("nx_ddl_mark")).toList());
        List<DdlLogParser.Entry> entries = DdlLogParser.parse(log).entries();
        assertThat(entries).hasSize(29).allMatch(DdlLogParser.Entry::beforeMark)
            .noneMatch(DdlLogParser.Entry::boot);
    }

    @Test
    void kinds_and_tags() {
        assertThat(DdlLogParser.classify("create unique index i on t(a)")).isEqualTo("create_index");
        assertThat(DdlLogParser.classify("CREATE UNLOGGED TABLE a (id int)")).isEqualTo("create_table");
        assertThat(DdlLogParser.classify("-- why\nDROP INDEX i")).isEqualTo("drop");
        assertThat(DdlLogParser.classify("/* c */ ALTER TABLE a ADD COLUMN b int")).isEqualTo("alter");
        assertThat(DdlLogParser.classify("CREATE OR REPLACE FUNCTION f()")).isEqualTo("other");
        assertThat(DdlLogParser.classify("")).isEqualTo("other");
        assertThat(DdlLogParser.tagOf("CREATE OR REPLACE FUNCTION f()")).isEqualTo("CREATE FUNCTION");
        assertThat(DdlLogParser.tagOf("GRANT SELECT ON t TO r")).isEqualTo("GRANT");
        assertThat(DdlLogParser.tagOf("")).isEqualTo("UNKNOWN");
    }

    @Test
    void folding_logs_sums_into_buckets_and_attributes_databases_to_classes() throws IOException {
        DdlCounts.Totals totals = new DdlCounts.Totals(); // not the fork's: the real file must stay real
        totals.noteDatabase("nexus_test_1", "SomeIntegrationTest", false, 3_000L);
        totals.noteRelease("nexus_test_1", 4_250L);
        totals.fold(resource("golden-pg-log.txt"), true, "unused");
        totals.fold(resource("golden-pg-log.txt"), false, "GoldenDedicatedTest");
        JsonNode out = asJson(totals.snapshot(2));
        assertThat(out.at("/buckets/boot/total").asLong()).as("boot of both containers, never counted below").isEqualTo(6);
        assertThat(out.at("/buckets/shared_migration/total").asLong()).isEqualTo(9);
        assertThat(out.at("/buckets/shared_tests/total").asLong()).isEqualTo(17);
        assertThat(out.at("/buckets/dedicated/total").asLong()).isEqualTo(26);
        assertThat(out.at("/containers/shared").asLong()).isEqualTo(1);
        assertThat(out.at("/containers/dedicated").asLong()).isEqualTo(1);
        assertThat(out.at("/containers_unread").asLong()).isEqualTo(2);
        assertThat(out.at("/ready_mark_seen").asBoolean()).isTrue();
        assertThat(out.at("/wal/shared_migration_bytes").asLong()).isEqualTo(1_000);
        assertThat(out.at("/wal/shared_tests_bytes").asLong()).isEqualTo(4_500);
        assertThat(out.at("/wal/dedicated_bytes").asLong()).isEqualTo(5_500);
        assertThat(out.at("/wal/dbsize_growth_bytes").asLong()).isEqualTo(3_400);
        assertThat(out.at("/wal/incomplete_containers").asLong()).isZero();
        assertThat(out.at("/wal/unreleased_databases").asLong()).isZero();
        assertThat(out.at("/per_class/GoldenDedicatedTest/ddl").asLong()).isEqualTo(26);
        assertThat(out.at("/per_class/GoldenDedicatedTest/wal_bytes").asLong()).isEqualTo(5_500);
        assertThat(out.at("/per_class/SomeIntegrationTest/databases").asLong()).isEqualTo(1);
        assertThat(out.at("/per_class/SomeIntegrationTest/wal_bytes").asLong())
            .as("the cluster's WAL between the class's CREATE DATABASE and the end of its drop").isEqualTo(1_250);
        // the eleven commands the log runs in nexus_test_1
        assertThat(out.at("/per_class/SomeIntegrationTest/ddl").asLong()).isEqualTo(11);
    }

    @Test
    void a_container_whose_marks_are_missing_is_incomplete_not_free() {
        DdlCounts.Totals totals = new DdlCounts.Totals();
        totals.fold("2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n", false, "NoMarksTest");
        totals.fold("2026-10-19 05:00:00.100 UTC [7] db=x LOG:  nx_ddl: CREATE TABLE\n", true, "unused");
        JsonNode out = asJson(totals.snapshot());
        assertThat(out.at("/wal/incomplete_containers").asLong()).isEqualTo(2);
        assertThat(out.at("/ready_mark_seen").asBoolean()).isFalse();
    }

    @Test
    void an_empty_database_s_commands_are_their_own_bucket_and_still_attributed_to_its_class() throws IOException {
        DdlCounts.Totals totals = new DdlCounts.Totals();
        totals.noteDatabase("nexus_test_1", "SomeMigrationTest", true, -1);
        totals.fold(resource("golden-pg-log.txt"), true, "unused");
        JsonNode out = asJson(totals.snapshot());
        assertThat(out.at("/buckets/shared_migration/total").asLong()).isEqualTo(9);
        assertThat(out.at("/buckets/shared_empty/total").asLong()).as("the eleven commands run in nexus_test_1").isEqualTo(11);
        assertThat(out.at("/buckets/shared_tests/total").asLong()).as("the rest of the tests' time").isEqualTo(6);
        assertThat(out.at("/buckets/dedicated/total").asLong()).isZero();
        assertThat(out.at("/per_class/SomeMigrationTest/databases").asLong()).isEqualTo(1);
        assertThat(out.at("/per_class/SomeMigrationTest/ddl").asLong()).isEqualTo(11);
    }

    @Test
    void the_probe_install_block_is_the_probe_file_after_its_connect_line_and_has_no_psql_syntax() {
        String block = DdlCounts.probeInstallBlock();
        assertThat(block).startsWith("DO $nxdo$ BEGIN EXECUTE $nxq$").endsWith("$nxq$; END $nxdo$");
        assertThat(block).contains("CREATE EVENT TRIGGER nx_ddl_probe", "nx_ddl_probe.fire()", "nx_ddl_probe.mark(");
        assertThat(block).doesNotContain("\\connect").doesNotContain("\\set").doesNotContain("\\gset");
        // The start mark is the container's own, run after the install: not part of an empty database's block.
        assertThat(block).doesNotContain("mark('start')").doesNotContain("nx-ddl-probe-end");
        assertThat(block).contains("wal=% dbsize=%");
        // The probe is installed once only: the template1 half of the file is not in the block.
        assertThat(block.split("CREATE EVENT TRIGGER", -1)).hasSize(2);
    }
}
