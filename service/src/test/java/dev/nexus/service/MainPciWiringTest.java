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
    void theSweepStartsAfterTheService_andStopsAfterTheReaper_beforeThePoolCloses() throws Exception {
        String src = code(main());
        int serviceStart = src.indexOf("service.start();");
        int sweepStart = src.indexOf("pciSweep.start();");
        int hook = src.indexOf("Runtime.getRuntime().addShutdownHook");
        int listenerStop = src.indexOf("service.stop();", hook);
        int reaper = src.indexOf("BackendReaper.terminateOwnBackends(", hook);
        int sweepStop = src.indexOf("pciSweep.stop();", hook);
        int poolClose = src.indexOf("ds.close();", hook);

        assertThat(serviceStart).isPositive();
        assertThat(sweepStart).as("started").isGreaterThan(serviceStart);
        assertThat(hook).isGreaterThan(sweepStart);
        assertThat(listenerStop).as("the listener stops first").isGreaterThan(hook);
        assertThat(reaper).as("the reaper runs next, inside the container's 10 s grace").isGreaterThan(listenerStop);
        assertThat(sweepStop).as("the sweep stops after the reaper, so a read that ignores the interrupt cannot"
            + " delay it").isGreaterThan(reaper);
        assertThat(poolClose).as("before the hook closes the pool, which aborts a stuck read").isGreaterThan(sweepStop);
        assertThat(src.indexOf("pciSweep.start();", sweepStart + 1)).as("started once").isNegative();
        assertThat(src.indexOf("pciSweep.stop();", sweepStop + 1)).as("stopped once").isNegative();
    }

    @Test
    void stopDoesNotWait() throws Exception {
        String sweep = code(Files.readString(Path.of("src/main/java/dev/nexus/service/vectors/PciIndexSweep.java")));
        assertThat(sweep).as("a stop that waits starts the reaper late exactly when a read is hung on a silent socket")
            .doesNotContain("awaitTermination");
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

    /**
     * PgSession parses its env in static initializers, so the first PgSession static call anywhere in the process
     * runs the parse and fails with ExceptionInInitializerError on a malformed value. The boot validation block, whose
     * catch reports event=pg_session_env_invalid and exits 1, must be that first call. Nothing before it in the
     * code (comments excluded) may name PgSession or a class that calls it on the way: LocalOnnxAdmission.fromEnv
     * (local-mode branch), TenantScope (seedEmbeddingProfile's withTenant -> PgSession.gucBatch), the repositories.
     */
    @Test
    void theBootValidationBlockPrecedesTheFirstPgSessionUse() throws Exception {
        String src = code(main());
        int firstValidation = src.indexOf("PgSession.startupEfSearchFloor()");
        assertThat(firstValidation).as("the validation block exists").isPositive();

        String before = src.substring(0, firstValidation);
        assertThat(before).as("no PgSession static call precedes the validation block")
            .doesNotContain("PgSession");
        assertThat(before).doesNotContain("LocalOnnxAdmission").doesNotContain("new TenantScope(")
            .doesNotContain("seedEmbeddingProfile").doesNotContain("PgVectorRepository(")
            .doesNotContain("NexusService(");

        // It is the try block whose catch reports the event, not a bare call.
        int tryOpen = src.lastIndexOf("try {", firstValidation);
        int catchEvent = src.indexOf("event=pg_session_env_invalid", firstValidation);
        int nextTry = src.indexOf("try {", firstValidation);
        assertThat(tryOpen).isPositive();
        assertThat(src.substring(tryOpen, firstValidation)).as("the try opens right at the validation block")
            .doesNotContain("catch").doesNotContain("System.exit");
        assertThat(catchEvent).as("its catch reports event=pg_session_env_invalid").isGreaterThan(firstValidation);
        assertThat(nextTry).as("before any other try block").isGreaterThan(catchEvent);

        // And the four NX_SEARCH_PCI* settings are among what it validates.
        assertThat(src.indexOf("PgSession.logPciBootSettings(")).isBetween(firstValidation, catchEvent);
        // The first uses that would have escaped come after it.
        assertThat(src.indexOf("seedEmbeddingProfile")).isGreaterThan(catchEvent);
        assertThat(src.indexOf("LocalOnnxAdmission.fromEnv()")).isGreaterThan(catchEvent);
    }

    /** Main's text with block and line comments removed, so a word in prose cannot satisfy or break a pin. */
    private static String code(String src) {
        return src.replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("(?m)//.*$", "");
    }
}
