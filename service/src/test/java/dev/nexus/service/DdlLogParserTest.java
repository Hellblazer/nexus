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
    void the_golden_log_splits_migration_from_tests() throws IOException {
        DdlLogParser.Parsed parsed = DdlLogParser.parse(resource("golden-pg-log.txt"));
        JsonNode expected = new ObjectMapper().readTree(resource("golden.json"));
        assertThat(asJson(DdlLogParser.tally(parsed.entries().stream().filter(DdlLogParser.Entry::beforeMark).toList())))
            .isEqualTo(expected.get("migration"));
        assertThat(asJson(DdlLogParser.tally(parsed.entries().stream().filter(e -> !e.beforeMark()).toList())))
            .isEqualTo(expected.get("tests"));
        assertThat(parsed.marks()).containsExactly("ready");
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
        assertThat(entries).hasSize(17).allMatch(DdlLogParser.Entry::beforeMark);
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
        totals.noteDatabase("nexus_test_1", "SomeIntegrationTest", false);
        totals.fold(resource("golden-pg-log.txt"), true, "unused");
        totals.fold(resource("golden-pg-log.txt"), false, "GoldenDedicatedTest");
        JsonNode out = asJson(totals.snapshot());
        assertThat(out.at("/buckets/shared_migration/total").asLong()).isEqualTo(9);
        assertThat(out.at("/buckets/shared_tests/total").asLong()).isEqualTo(8);
        assertThat(out.at("/buckets/dedicated/total").asLong()).isEqualTo(17);
        assertThat(out.at("/containers/shared").asLong()).isEqualTo(1);
        assertThat(out.at("/containers/dedicated").asLong()).isEqualTo(1);
        assertThat(out.at("/ready_mark_seen").asBoolean()).isTrue();
        assertThat(out.at("/per_class/GoldenDedicatedTest/ddl").asLong()).isEqualTo(17);
        assertThat(out.at("/per_class/SomeIntegrationTest/databases").asLong()).isEqualTo(1);
        // the four commands the log runs in nexus_test_1 (a GRANT, a DROP, two CREATE INDEX)
        assertThat(out.at("/per_class/SomeIntegrationTest/ddl").asLong()).isEqualTo(4);
    }

    @Test
    void an_empty_database_s_commands_are_their_own_bucket_and_still_attributed_to_its_class() throws IOException {
        DdlCounts.Totals totals = new DdlCounts.Totals();
        totals.noteDatabase("nexus_test_1", "SomeMigrationTest", true);
        totals.fold(resource("golden-pg-log.txt"), true, "unused");
        JsonNode out = asJson(totals.snapshot());
        assertThat(out.at("/buckets/shared_migration/total").asLong()).isEqualTo(9);
        assertThat(out.at("/buckets/shared_empty/total").asLong()).as("the four commands run in nexus_test_1").isEqualTo(4);
        assertThat(out.at("/buckets/shared_tests/total").asLong()).as("the rest of the tests' time").isEqualTo(4);
        assertThat(out.at("/buckets/dedicated/total").asLong()).isZero();
        assertThat(out.at("/per_class/SomeMigrationTest/databases").asLong()).isEqualTo(1);
        assertThat(out.at("/per_class/SomeMigrationTest/ddl").asLong()).isEqualTo(4);
    }

    @Test
    void the_probe_install_block_is_the_probe_file_after_its_connect_line_and_has_no_psql_syntax() {
        String block = DdlCounts.probeInstallBlock();
        assertThat(block).startsWith("DO $nxdo$ BEGIN EXECUTE $nxq$").endsWith("$nxq$; END $nxdo$");
        assertThat(block).contains("CREATE EVENT TRIGGER nx_ddl_probe", "nx_ddl_probe.fire()", "nx_ddl_probe.mark(");
        assertThat(block).doesNotContain("\\connect").doesNotContain("\\set").doesNotContain("\\gset");
        // The probe is installed once only: the template1 half of the file is not in the block.
        assertThat(block.split("CREATE EVENT TRIGGER", -1)).hasSize(2);
    }
}
