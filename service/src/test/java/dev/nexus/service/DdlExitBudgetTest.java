// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

/**
 * nexus-t4y7b: the JVM-exit hook of the DDL counter reads the logs of every container still live. Each read
 * can take three 60 s tries when a docker exec wedges, so the hook as a whole runs under one deadline: it
 * stops waiting, reports what it did not read, and counts the abandoned read as a log error (the ratchet
 * refuses a run with log errors). The assertions are on the count FILE the hook flushes, which is what the
 * ratchet reads.
 */
class DdlExitBudgetTest {

    private static <T> T withCountDir(java.util.concurrent.Callable<T> countDirBody) throws Exception {
        Path dir = Files.createTempDirectory("nx-t4y7b-counts");
        String previous = System.getProperty(DdlCounts.DIR_PROPERTY);
        System.setProperty(DdlCounts.DIR_PROPERTY, dir.toString());
        try (AutoCloseable isolated = DdlCounts.isolateForTest()) {
            return countDirBody.call();
        } finally {
            if (previous == null) {
                System.clearProperty(DdlCounts.DIR_PROPERTY);
            } else {
                System.setProperty(DdlCounts.DIR_PROPERTY, previous);
            }
            deleteTree(dir);
        }
    }

    private static void deleteTree(Path dir) throws IOException {
        try (Stream<Path> walk = Files.walk(dir)) {
            for (Path p : walk.sorted(Comparator.reverseOrder()).toList()) {
                Files.deleteIfExists(p);
            }
        }
    }

    /** The one count file this fork wrote into the isolated directory. */
    private static JsonNode flushedFile() throws IOException {
        Path dir = Path.of(System.getProperty(DdlCounts.DIR_PROPERTY));
        List<Path> files;
        try (Stream<Path> s = Files.list(dir)) {
            files = s.filter(p -> p.toString().endsWith(".json")).toList();
        }
        assertThat(files).as("this fork's count file").hasSize(1);
        return new ObjectMapper().readTree(files.get(0).toFile());
    }

    @Test
    @org.junit.jupiter.api.Timeout(60) // a hook with no budget would wait on the wedged read forever; fail instead of hang
    void the_exit_hook_gives_up_after_its_budget_and_counts_the_read_it_abandoned() throws Exception {
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false registers nothing");
        assertThat(DdlCounts.EXIT_BUDGET)
            .as("the production budget: inside surefire's 30 s forkedProcessExitTimeoutInSeconds, so the flush lands")
            .isEqualTo(Duration.ofSeconds(15));
        withCountDir(() -> {
            for (int i = 0; i < 3; i++) {
                DdlCounts.register((FailFastPostgreSQLContainer) PgContainerHelper.newContainer());
            }
            JsonNode registered = flushedFile();
            assertThat(registered.at("/containers_unread").asLong()).as("three registered, none read").isEqualTo(3L);
            long errorsBefore = registered.at("/log_errors").asLong();
            CountDownLatch wedged = new CountDownLatch(1);
            AtomicInteger reads = new AtomicInteger();
            long started = System.nanoTime();
            JsonNode atReturn;
            int readsAtReturn;
            try {
                // As production's record(): claim the container (it leaves the live set), then read it.
                DdlCounts.onJvmExit(Duration.ofMillis(300), c -> {
                    if (!DdlCounts.claimLive(c)) {
                        return;
                    }
                    reads.incrementAndGet();
                    try {
                        wedged.await(); // an exec that never returns
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                });
                // Read before the wedge is released: the worker then goes on to the next container.
                readsAtReturn = reads.get();
                atReturn = flushedFile();
            } finally {
                wedged.countDown();
            }
            Duration took = Duration.ofNanos(System.nanoTime() - started);
            assertThat(took).as("the hook returned at its budget, not after the wedged read").isLessThan(Duration.ofSeconds(10));
            assertThat(readsAtReturn).as("the first container's read wedged the worker; the others were never started")
                .isEqualTo(1);
            assertThat(atReturn.at("/log_errors").asLong() - errorsBefore)
                .as("the FLUSHED file counts the abandoned read as a log error, so the ratchet refuses the run")
                .isEqualTo(1L);
            assertThat(atReturn.at("/containers_unread").asLong())
                .as("the wedged container was claimed; the two the worker never reached stay unread")
                .isEqualTo(2L);
            return null;
        });
    }

    @Test
    void a_hook_that_finishes_inside_its_budget_adds_no_error() throws Exception {
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false registers nothing");
        withCountDir(() -> {
            DdlCounts.register((FailFastPostgreSQLContainer) PgContainerHelper.newContainer());
            long errorsBefore = flushedFile().at("/log_errors").asLong();
            AtomicInteger reads = new AtomicInteger();
            DdlCounts.onJvmExit(Duration.ofSeconds(10), c -> {
                if (DdlCounts.claimLive(c)) {
                    reads.incrementAndGet();
                }
            });
            assertThat(reads.get()).isEqualTo(1);
            JsonNode after = flushedFile();
            assertThat(after.at("/log_errors").asLong()).isEqualTo(errorsBefore);
            assertThat(after.at("/containers_unread").asLong()).as("every container was read").isEqualTo(0L);
            return null;
        });
    }

    /**
     * nexus-ccg9y (4): the no-arg hook the JVM runs at exit hands {@link DdlCounts#EXIT_BUDGET} and
     * {@link DdlCounts#record} to {@link DdlCounts#exitRunner}. The real no-arg hook is called with a runner that
     * checks the budget it is given and then runs the production one; the recorder is identified by what it does to a
     * registered container (claims it, folds its log read and flushes: an unstarted container's read fails, which
     * {@code record} counts as one log error), which a recorder that only claimed it, or did nothing, would not do.
     * A hook body that bypassed the runner, or gave it another budget or recorder, fails here.
     */
    @Test
    void the_no_arg_exit_hook_runs_the_production_budget_and_the_real_recorder() throws Exception {
        assumeTrue(DdlCounts.enabled(), "needs the DDL counter: -Dnexus.ddl.count=false registers nothing");
        withCountDir(() -> {
            FailFastPostgreSQLContainer c = (FailFastPostgreSQLContainer) PgContainerHelper.newContainer();
            DdlCounts.register(c);
            long errorsBefore = flushedFile().at("/log_errors").asLong();
            assertThat(flushedFile().at("/containers_unread").asLong()).isEqualTo(1L);
            AtomicInteger runs = new AtomicInteger();
            DdlCounts.ExitRunner production = DdlCounts.exitRunner;
            DdlCounts.exitRunner = (budget, recorder) -> {
                runs.incrementAndGet();
                assertThat(budget).as("the production budget").isSameAs(DdlCounts.EXIT_BUDGET);
                production.run(budget, recorder);
            };
            try {
                DdlCounts.onJvmExit();                       // the real no-arg hook, through the seam it calls
            } finally {
                DdlCounts.exitRunner = production;
            }
            assertThat(runs.get()).as("the no-arg hook went through the runner").isEqualTo(1);
            JsonNode after = flushedFile();
            assertThat(after.at("/containers_unread").asLong()).as("the recorder took the container").isEqualTo(0L);
            assertThat(after.at("/log_errors").asLong() - errorsBefore)
                .as("and it is record(): the unstarted container's log read failed and was counted").isEqualTo(1L);

            return null;
        });
    }
}
