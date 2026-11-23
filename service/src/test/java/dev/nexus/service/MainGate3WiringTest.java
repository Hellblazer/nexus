// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the one gate 3 signal the Java readers switch to the id on, as {@code Main} builds it and
 * {@code NexusService} hands it on. {@code Main.main} cannot run in a test, and every constructor short of the production one
 * passes {@code Gate3.never()}, so dropping the signal anywhere would compile and stay green; the wiring is pinned on the text,
 * comments stripped so a commented-out line cannot satisfy it.
 *
 * <p>The order is the point (pre-review of 23db1bf651, both reviewers): the boot latch is read before {@code NexusService} is
 * built, because building it schedules the re-embed runner and the sweeps over the catalog; a signal that answered "not
 * latched" while the read was still retrying would let a latched install read or write under a stale label on a slow restart.
 */
class MainGate3WiringTest {

    private static String code(String file) throws Exception {
        // Maven runs a module's tests with the module directory as cwd.
        String src = Files.readString(Path.of("src/main/java/dev/nexus/service/" + file));
        return src.replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
    }

    @Test
    void theBootLatchIsReadBeforeTheServiceIsBuilt_andTheServiceTakesTheSignal() throws Exception {
        String main = code("Main.java");
        int latchRead = main.indexOf("Gate3Latch.readAtBoot(adminConnection)");
        int service = main.indexOf("new NexusService(port, token, ds,");
        int start = main.indexOf("service.start();");
        assertThat(latchRead).as("Main reads the boot latch").isPositive();
        assertThat(service).as("the latch is read before the service, whose constructor schedules the catalog's work")
            .isGreaterThan(latchRead);
        assertThat(start).isGreaterThan(service);
        assertThat(main.substring(service, main.indexOf(";", service)))
            .as("the production service takes the one signal and the readiness").containsPattern("gate3,\\s*renameReadiness\\)");
        assertThat(main).as("Main never builds a never-latched signal").doesNotContain("Gate3.never()");
    }

    @Test
    void theSignalAnswersTheBootLatchUntilTheCompletionCheckRuns_thenTheChecksLatch() throws Exception {
        String main = code("Main.java");
        assertThat(main).as("the signal reads the check once it exists, the boot latch before")
            .containsPattern("return\\s+c\\s*!=\\s*null\\s*\\?\\s*c\\.gate3Latched\\(\\)\\s*:\\s*bootLatch\\.latched\\(\\);");
        int check = main.indexOf("startCompletionBootWork(bootWork, adminConnection,");
        int handOver = main.indexOf("completionRef.set(completionCheck);");
        assertThat(check).isPositive();
        assertThat(handOver).as("the signal moves to the check's latch as soon as the check is built").isGreaterThan(check);
        assertThat(main.substring(check, handOver)).as("nothing runs between the check's start and the hand-over")
            .doesNotContain(";\n        var ");
    }

    @Test
    void theServiceHandsTheSignalToItsRepositories() throws Exception {
        String service = code("NexusService.java");
        assertThat(service).as("the catalog reads the id from gate 3").contains("new CatalogRepository(tenantScope, gate3, renameGuard)");
        assertThat(service).as("and so do the centroids")
            .contains("new dev.nexus.service.vectors.TaxonomyCentroidRepository(tenantScope, gate3)");
    }
}
