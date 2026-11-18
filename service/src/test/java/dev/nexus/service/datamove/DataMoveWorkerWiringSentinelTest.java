// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * nexus-f3yqd.2.13: a sentinel for the interim wiring. {@code DataMoveWorker.create} wires {@code MoveFinisher.RUNS_ONLY}
 * (a move whose runs are done is left open forever) and {@code FailureHandler.BLOCK_ALWAYS} (a permanent failure is retried
 * forever), which is safe only while no changeset registers a move. The first changeset that INSERTs into
 * {@code nexus.data_moves}, in SQL or as a Liquibase {@code <insert>} or {@code <loadData>}, must come with the
 * verification (nexus-f3yqd.2.14) and failure-class (.15) beads' wiring; until it does, this test is red.
 */
class DataMoveWorkerWiringSentinelTest {

    private static final Path MAIN = Path.of("src", "main");
    private static final Pattern SQL_INSERT =
        Pattern.compile("(?is)insert\\s+into\\s+\"?nexus\"?\\s*\\.\\s*\"?data_moves\\b");
    /** A Liquibase change that writes rows: the tag, whatever order its attributes come in. */
    private static final Pattern LIQUIBASE_ROWS = Pattern.compile("(?is)<(?:insert|loadData)\\b[^>]*>");
    private static final Pattern TABLE_IS_DATA_MOVES = Pattern.compile("(?is)\\btableName\\s*=\\s*\"data_moves\"");
    private static final Pattern SCHEMA_ATTRIBUTE = Pattern.compile("(?is)\\bschemaName\\s*=\\s*\"([^\"]*)\"");

    /** True when {@code text} registers a move: SQL INSERT INTO nexus.data_moves, or a Liquibase insert or loadData of it. */
    static boolean registersAMove(String text) {
        if (SQL_INSERT.matcher(text).find()) {
            return true;
        }
        Matcher tag = LIQUIBASE_ROWS.matcher(text);
        while (tag.find()) {
            String attributes = tag.group();
            if (!TABLE_IS_DATA_MOVES.matcher(attributes).find()) {
                continue;
            }
            Matcher schema = SCHEMA_ATTRIBUTE.matcher(attributes);
            if (!schema.find() || schema.group(1).equals("nexus")) {   // absent: the changelog's default schema is nexus
                return true;
            }
        }
        return false;
    }

    @Test
    void theDetectorFindsEveryFormOfRegisteringAMove_andNothingElse() {
        assertThat(registersAMove("insert into nexus.data_moves (label) values ('x');")).isTrue();
        assertThat(registersAMove("insert  into \"nexus\".\"data_moves\" (label) values ('x')")).isTrue();
        assertThat(registersAMove("<insert schemaName=\"nexus\" tableName=\"data_moves\"><column name=\"label\"/></insert>"))
            .isTrue();
        assertThat(registersAMove("<insert tableName=\"data_moves\" schemaName=\"nexus\">")).isTrue();
        assertThat(registersAMove("<insert tableName=\"data_moves\"><column name=\"label\"/></insert>"))
            .as("no schemaName: the default schema is nexus").isTrue();
        assertThat(registersAMove("<loadData file=\"x.csv\" tableName=\"data_moves\"/>")).isTrue();
        assertThat(registersAMove("<insert\n    tableName=\"data_moves\"\n    schemaName=\"nexus\">")).isTrue();

        assertThat(registersAMove("<insert schemaName=\"other\" tableName=\"data_moves\">")).isFalse();
        assertThat(registersAMove("<insert schemaName=\"nexus\" tableName=\"service_tokens\">")).isFalse();
        assertThat(registersAMove("create table nexus.data_moves (label text not null);")).isFalse();
        assertThat(registersAMove("select * from nexus.data_moves")).isFalse();
    }

    @Test
    void noMainChangelogRegistersAMove_whileCreateStillWiresTheInterimFinisherAndHandler() throws IOException {
        Path changelogs = MAIN.resolve("resources").resolve("db");
        List<String> registering = new ArrayList<>();
        int scanned = 0;
        try (Stream<Path> files = Files.walk(changelogs)) {
            for (Path f : files.filter(p -> p.toString().endsWith(".xml") || p.toString().endsWith(".sql")).toList()) {
                scanned++;
                if (registersAMove(Files.readString(f))) {
                    registering.add(MAIN.relativize(f).toString());
                }
            }
        }
        assertThat(scanned).as("changelog files read (a walk that found none proves nothing)").isGreaterThan(100);

        String worker = Files.readString(MAIN.resolve("java/dev/nexus/service/datamove/DataMoveWorker.java"));
        int create = worker.indexOf("public static DataMoveWorker create(");
        assertThat(create).as("DataMoveWorker.create is found").isPositive();
        String body = worker.substring(create, worker.indexOf("\n    }\n", create));
        boolean interim = body.contains("MoveFinisher.RUNS_ONLY") || body.contains("FailureHandler.BLOCK_ALWAYS");

        assertThat(registering.isEmpty() || !interim)
            .as("changelogs %s register a move while DataMoveWorker.create wires the interim RUNS_ONLY / BLOCK_ALWAYS",
                registering)
            .isTrue();
    }
}
