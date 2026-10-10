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
        String src = code(main());   // code only: the right text in a trailing comment must not satisfy the pin
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
        // nexus-43ulx.17: the pool's reaper and the builder's run together inside terminateAtShutdown.
        int reaper = src.indexOf("BackendReaper.terminateAtShutdown(", hook);
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

        // Unguarded: the read half runs whatever NX_SEARCH_PCI says, so neither call may sit under an if, a loop or
        // a ternary. Each is a plain statement (the token before it ends a statement or a block) at the brace depth
        // of its neighbour that always runs.
        int mainDepth = depthAt(src, src.indexOf("var ds = new HikariDataSource(hikari);"));
        assertThat(depthAt(src, serviceStart)).as("service.start() is a top-level statement of main")
            .isEqualTo(mainDepth);
        assertThat(depthAt(src, sweepStart)).as("pciSweep.start() is at main's top level, not in a block")
            .isEqualTo(mainDepth);
        assertThat(precedingToken(src, sweepStart)).as("pciSweep.start() follows a statement, not an if/else/loop head")
            .isIn(";", "}");
        assertThat(depthAt(src, sweepStop)).as("pciSweep.stop() is at the hook's top level, beside service.stop()")
            .isEqualTo(depthAt(src, listenerStop));
        assertThat(depthAt(src, sweepStop)).isEqualTo(depthAt(src, reaper));
        assertThat(precedingToken(src, sweepStop)).as("pciSweep.stop() follows a statement, not an if/else/loop head")
            .isIn(";", "}");
    }

    /** The validation catch must end the process; without it a refused boot runs on into the schema migration. */
    @Test
    void theValidationCatchReportsTheEvent_unwrapsTheInitializerError_andExitsOne() throws Exception {
        String src = code(main());
        int event = src.indexOf("event=pg_session_env_invalid");
        assertThat(event).isPositive();
        int catchOpen = src.lastIndexOf("catch (Throwable t) {", event);
        assertThat(catchOpen).as("the event is logged by the Throwable catch of the validation block").isPositive();
        int open = src.indexOf('{', catchOpen);
        int close = matchingClose(src, open);
        String body = src.substring(open + 1, close);

        assertThat(body).contains("event=pg_session_env_invalid");
        assertThat(body).as("a static-init failure arrives wrapped; the cause carries the variable's message")
            .contains("instanceof ExceptionInInitializerError").contains("getCause()");
        assertThat(body.strip()).as("the catch ends the process as its last statement").endsWith("System.exit(1);");
        assertThat(depthAt(src, open + 1 + body.lastIndexOf("System.exit(1);")))
            .as("System.exit(1) is a statement of the catch itself, not of a nested if")
            .isEqualTo(depthAt(src, open + 1));
        assertThat(precedingToken(src, open + 1 + body.lastIndexOf("System.exit(1);"))).isEqualTo(";");
    }

    @Test
    void stopDoesNotWait() throws Exception {
        String sweep = code(Files.readString(Path.of("src/main/java/dev/nexus/service/vectors/PciIndexSweep.java")));
        assertThat(sweep).as("a stop that waits starts the reaper late exactly when a read is hung on a silent socket")
            .doesNotContain("awaitTermination");
        String reconciler = code(Files.readString(
            Path.of("src/main/java/dev/nexus/service/vectors/PciReconciler.java")));
        assertThat(reconciler).as("an in-flight CREATE INDEX CONCURRENTLY ignores the interrupt; the builder reaper"
            + " ends it, so the hook must not wait for the pass").doesNotContain("awaitTermination");
    }

    /**
     * nexus-43ulx.19: the DDL half is built with the admin values and the boot nonce, starts after the service and
     * after the read half, and stops in the hook right after the listener and BEFORE the builder reaper, so a pass
     * cannot open a new builder connection after the reaper has ended the old one.
     */
    @Test
    void theReconcilerStartsAfterTheReadHalf_andStopsAfterTheListener_beforeTheReaper() throws Exception {
        String src = code(main());
        int serviceStart = src.indexOf("service.start();");
        int sweepStart = src.indexOf("pciSweep.start();");
        int create = src.indexOf("PciReconciler.create(ds, adminConnection, pciBootNonce, pciSweep,");
        int reconcilerStart = src.indexOf("pciReconciler.start();");
        int hook = src.indexOf("Runtime.getRuntime().addShutdownHook");
        int stopBinding = src.indexOf("Runnable stopPciReconciler = pciReconciler::stop;");
        int listenerStop = src.indexOf("service.stop();", hook);
        int reconcilerStop = src.indexOf("stopPciReconciler.run();", hook);
        int reaper = src.indexOf("BackendReaper.terminateAtShutdown(", hook);

        assertThat(create).as("built from the pool, the admin values, the boot nonce and the real sweep").isPositive();
        assertThat(create).isGreaterThan(sweepStart);
        assertThat(sweepStart).isGreaterThan(serviceStart);
        assertThat(reconcilerStart).as("started after the read half").isGreaterThan(create);
        assertThat(stopBinding).as("the hook's stop is the reconciler's own stop()").isPositive().isLessThan(hook);
        assertThat(listenerStop).isGreaterThan(hook);
        assertThat(reconcilerStop).as("stopped right after the listener").isGreaterThan(listenerStop);
        assertThat(reaper).as("and before the builder reaper").isGreaterThan(reconcilerStop);
        assertThat(src.indexOf("pciReconciler.start();", reconcilerStart + 1)).as("started once").isNegative();
        assertThat(src.indexOf("stopPciReconciler.run();", reconcilerStop + 1)).as("stopped once").isNegative();
        assertThat(src).as("the placeholder no-op is gone").doesNotContain("stopPciReconciler = () -> { }");
    }

    /**
     * nexus-43ulx.23: {@code per_collection_indexes} on {@code /v1/status} is bound in Main from the SAME sweep and
     * reconciler that run (not a copy, not {@code PciIndexSet.NONE}), after both exist. Without the call the route
     * silently omits the key and every client reads "cannot tell"; no other test would notice.
     */
    @Test
    void theStatusObjectIsBoundFromTheRunningSweepAndReconciler_afterBothExist() throws Exception {
        String src = code(main());
        int reconcilerStart = src.indexOf("pciReconciler.start();");
        int bind = src.indexOf("service.perCollectionIndexes(");
        int ready = src.indexOf("event=service_ready");
        assertThat(bind).as("Main binds the status source").isPositive();
        assertThat(bind).as("after the reconciler it reads").isGreaterThan(reconcilerStart);
        assertThat(bind).as("before the service is declared ready").isLessThan(ready);
        assertThat(src).as("built from the running sweep's and reconciler's own status, nothing re-read")
            .containsPattern("StatusHandler\\.PerCollectionIndexes\\.of\\(\\s*pciSweep\\.status\\(\\),\\s*"
                + "pciReconciler\\.status\\(\\)\\)");
        assertThat(src.indexOf("service.perCollectionIndexes(", bind + 1)).as("bound once").isNegative();
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
        assertThat(before).as("no reflective spelling of a PgSession touch either")
            .doesNotContain("Class.forName").doesNotContain("loadClass(").doesNotContain("MethodHandles")
            .doesNotContain("java.lang.reflect");
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

    /**
     * The source with block and line comments removed, so a word in prose cannot satisfy or break a pin. Scans
     * string and char literals, so a {@code //} inside one (a URL) does not eat the rest of its line. Newlines inside
     * a block comment are kept, which keeps line numbers.
     */
    private static String code(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int i = 0;
        int n = src.length();
        while (i < n) {
            char c = src.charAt(i);
            if (c == '/' && i + 1 < n && src.charAt(i + 1) == '/') {
                while (i < n && src.charAt(i) != '\n') {
                    i++;
                }
            } else if (c == '/' && i + 1 < n && src.charAt(i + 1) == '*') {
                int end = src.indexOf("*/", i + 2);
                end = end < 0 ? n : end + 2;
                for (int k = i; k < end; k++) {
                    if (src.charAt(k) == '\n') {
                        out.append('\n');
                    }
                }
                i = end;
            } else if (c == '"' || c == '\'') {
                int end = literalEnd(src, i);
                out.append(src, i, end);
                i = end;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    /** Index just past the string or char literal that opens at {@code start}. */
    private static int literalEnd(String src, int start) {
        char quote = src.charAt(start);
        int i = start + 1;
        while (i < src.length() && src.charAt(i) != quote && src.charAt(i) != '\n') {
            i += src.charAt(i) == '\\' ? 2 : 1;
        }
        return Math.min(i + 1, src.length());
    }

    /** Open braces minus close braces before {@code idx}, outside string and char literals. */
    private static int depthAt(String code, int idx) {
        assertThat(idx).as("a statement the pin looks for is missing").isNotNegative();
        int depth = 0;
        int i = 0;
        while (i < idx) {
            char c = code.charAt(i);
            if (c == '"' || c == '\'') {
                i = literalEnd(code, i);
                continue;
            }
            if (c == '{') {
                depth++;
            } else if (c == '}') {
                depth--;
            }
            i++;
        }
        return depth;
    }

    /** Index of the brace that closes the one at {@code open}. */
    private static int matchingClose(String code, int open) {
        int depth = 0;
        int i = open;
        while (i < code.length()) {
            char c = code.charAt(i);
            if (c == '"' || c == '\'') {
                i = literalEnd(code, i);
                continue;
            }
            if (c == '{') {
                depth++;
            } else if (c == '}' && --depth == 0) {
                return i;
            }
            i++;
        }
        throw new AssertionError("unbalanced braces from " + open);
    }

    /** The last non-whitespace character before {@code idx}, as a string; "" at the start of the text. */
    private static String precedingToken(String code, int idx) {
        int i = idx - 1;
        while (i >= 0 && Character.isWhitespace(code.charAt(i))) {
            i--;
        }
        return i < 0 ? "" : String.valueOf(code.charAt(i));
    }
}
