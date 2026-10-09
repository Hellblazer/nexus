// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (nexus-43ulx.12): {@code Main.main} is the single production construction site of
 * {@code PgVectorRepository}, and it cannot be run in a test (it exits the JVM and blocks the main thread). So the
 * wiring is pinned on its text: the repository is handed the real sweep, never {@code PciIndexSet.NONE} (which would
 * make every single-collection statement walk at 1000 forever, silently), and the sweep starts after the service
 * and stops before the pool closes.
 */
class MainPciWiringTest {

    private static String main() throws Exception {
        // Maven runs a module's tests with the module directory as cwd.
        return Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
    }

    @Test
    void theProductionRepositoryIsHandedTheRealSweep_notNone() throws Exception {
        String src = main();
        assertThat(src).as("the sweep is built from the pool and the validated settings")
            .contains("PciIndexSweep.create(ds, dev.nexus.service.db.PgSession.startupPciSettings())");
        assertThat(src).as("the repository takes the sweep as its index set")
            .containsPattern("new PgVectorRepository\\(tenantScope,\\s*docEmbedRouter,\\s*qryEmbedRouter,\\s*pciSweep\\)");
        assertThat(src).as("Main never names the empty set").doesNotContain("PciIndexSet.NONE");
    }

    @Test
    void theSweepStartsAfterTheService_andStopsBeforeThePoolCloses() throws Exception {
        String src = main();
        int serviceStart = src.indexOf("service.start();");
        int sweepStart = src.indexOf("pciSweep.start();");
        int hook = src.indexOf("Runtime.getRuntime().addShutdownHook");
        int sweepStop = src.indexOf("pciSweep.stop();", hook);
        int poolClose = src.indexOf("ds.close();", hook);

        assertThat(serviceStart).isPositive();
        assertThat(sweepStart).as("started").isGreaterThan(serviceStart);
        assertThat(hook).isGreaterThan(sweepStart);
        assertThat(sweepStop).as("stopped in the shutdown hook").isGreaterThan(hook);
        assertThat(poolClose).as("before the hook closes the pool").isGreaterThan(sweepStop);
        assertThat(src.indexOf("pciSweep.start();", sweepStart + 1)).as("started once").isNegative();
    }

    @Test
    void theRepositoryIsBuiltAfterTheBootSettingsAreValidated() throws Exception {
        String src = main();
        int validated = src.indexOf("PgSession.logPciBootSettings(");
        int repo = src.indexOf("new PgVectorRepository(");
        assertThat(validated).isPositive();
        assertThat(repo).as("a malformed NX_SEARCH_PCI* value must reach the boot catch, not escape main")
            .isGreaterThan(validated);
    }
}
