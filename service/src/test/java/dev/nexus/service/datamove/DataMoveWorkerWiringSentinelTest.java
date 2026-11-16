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
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * nexus-f3yqd.2.13: a sentinel for the interim wiring. {@code DataMoveWorker.create} wires {@code MoveFinisher.RUNS_ONLY}
 * (a move whose runs are done is left open forever) and {@code FailureHandler.BLOCK_ALWAYS} (a permanent failure is retried
 * forever), which is safe only while no changeset registers a move. The first changeset that INSERTs into
 * {@code nexus.data_moves} must come with the verification (nexus-f3yqd.2.14) and failure-class (.15) beads' wiring; until it
 * does, this test is red.
 */
class DataMoveWorkerWiringSentinelTest {

    private static final Path MAIN = Path.of("src", "main");
    private static final Pattern REGISTERS_A_MOVE =
        Pattern.compile("(?is)insert\\s+into\\s+\"?nexus\"?\\s*\\.\\s*\"?data_moves\\b");

    @Test
    void noMainChangelogRegistersAMove_whileCreateStillWiresTheInterimFinisherAndHandler() throws IOException {
        Path changelogs = MAIN.resolve("resources").resolve("db");
        List<String> registering = new ArrayList<>();
        int scanned = 0;
        try (Stream<Path> files = Files.walk(changelogs)) {
            for (Path f : files.filter(p -> p.toString().endsWith(".xml") || p.toString().endsWith(".sql")).toList()) {
                scanned++;
                if (REGISTERS_A_MOVE.matcher(Files.readString(f)).find()) {
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
