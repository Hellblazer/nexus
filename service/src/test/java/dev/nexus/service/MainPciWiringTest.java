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

    /**
     * linda's census of 19d19081dc, item 1: production builds the rename readiness on {@code IdReaders.present()} and nothing else. The only way to replace the
     * id-readers signal is {@code CompletionBootWork#idReadersPresent(BooleanSupplier)}, which is package-private and used by test support only: no main source
     * calls it, so an injected {@code () -> true} can never reach production; and Main builds the check through the one factory that has no such parameter.
     */
    @Test
    void productionBuildsTheReadinessOnIdReadersPresentAlone_noMainSourceCallsTheInjectingSeam() throws Exception {
        // the seam is package-private, so the compiler already confines its callers to this package; PciReconciler has a public method of the same name
        // that is its own (the reconciler's signal), so the scan is of the package that can reach the check's
        try (var files = Files.walk(Path.of("src/main/java/dev/nexus/service/datamove"))) {
            for (Path f : (Iterable<Path>) files.filter(p -> p.toString().endsWith(".java"))::iterator) {
                if (f.endsWith("CompletionBootWork.java")) {
                    continue;
                }
                assertThat(code(Files.readString(f))).as(f + " does not call the test-only id-readers seam").doesNotContain("idReadersPresent(");
            }
        }
        String own = code(Files.readString(Path.of("src/main/java/dev/nexus/service/datamove/CompletionBootWork.java")));
        assertThat(own).as("the default is IdReaders::present").contains("BooleanSupplier idReadersPresent = dev.nexus.service.vectors.IdReaders::present;");
        assertThat(own).as("the seam is package-private").contains("\n    void idReadersPresent(java.util.function.BooleanSupplier present)");
        assertThat(own).as("and only the field and that seam assign it").containsPattern("this\\.idReadersPresent = Objects\\.requireNonNull\\(present");
        assertThat(code(main())).as("Main starts the check through atBoot, which has no id-readers parameter").contains("CompletionBootWork.atBoot(");
    }

    @Test
    void theProductionRepositoryIsHandedTheRealSweep_notNone() throws Exception {
        String src = code(main());   // code only: the right text in a trailing comment must not satisfy the pin
        assertThat(src).as("the sweep is built from the pool and the validated settings")
            .contains("PciIndexSweep.create(ds, dev.nexus.service.db.PgSession.startupPciSettings())");
        assertThat(src).as("the repository takes the sweep as its index set, and the one gate 3 signal (RDR-229 P3.5c)")
            .containsPattern("new PgVectorRepository\\(tenantScope,\\s*docEmbedRouter,\\s*qryEmbedRouter,\\s*pciSweep,\\s*gate3\\)");
        assertThat(src).as("Main never names the empty set").doesNotContain("PciIndexSet.NONE");
    }

    @Test
    void theSweepStartsAfterTheService_andStopsAfterTheReaper_beforeThePoolCloses() throws Exception {
        String src = code(main());
        int serviceStart = src.indexOf("service.start();");
        int sweepStart = src.indexOf("pciSweep.start();");
        int hook = shutdownSteps(src);
        int listenerStop = src.indexOf("service.stop();", hook);
        // nexus-f3yqd: terminateAtShutdown reaps the pool's backends only.
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

    /**
     * nexus-y6aj0: the shutdown steps that do not touch ONNX Runtime live in one Runnable, because two paths run
     * them: the shutdown hook, and the gate's expired-bound path, which leaves without the hook phase. The order
     * pins in this class read that block.
     */
    private static int shutdownSteps(String src) {
        int block = src.indexOf("Runnable stopAllButEmbedders = () -> {");
        assertThat(block).as("the shared shutdown steps").isPositive();
        return block;
    }

    @Test
    void theHookAndTheExpiredBoundPathBothRunTheSharedSteps_andOnlyTheHookClosesEmbeddersAndThePool() throws Exception {
        String src = code(main());
        int block = shutdownSteps(src);
        int blockEnd = matchingClose(src, src.indexOf('{', block));
        int expired = src.indexOf("OrtInitGate.process().onBoundExpired(stopAllButEmbedders);");
        int hook = src.indexOf("Runtime.getRuntime().addShutdownHook");
        int hookRuns = src.indexOf("stopAllButEmbedders.run();", hook);
        int embedders = src.indexOf("docEmbedRouter.close();", hook);
        int poolClose = src.indexOf("ds.close();", hook);

        assertThat(expired).as("the expired-bound path is handed the shared steps, nothing more").isGreaterThan(blockEnd);
        assertThat(hook).isGreaterThan(blockEnd);
        assertThat(hookRuns).as("the hook runs the shared steps first").isGreaterThan(hook);
        assertThat(embedders).as("then closes the embedders").isGreaterThan(hookRuns);
        assertThat(poolClose).as("then the pool").isGreaterThan(embedders);
        String steps = src.substring(block, blockEnd);
        assertThat(steps).as("the shared steps run with native model work possibly live: no session close, and no"
            + " pool close, which can stall on a dead PG with nothing left to end the process")
            .doesNotContain("EmbedRouter.close()").doesNotContain("ds.close()");
        assertThat(src.indexOf("stopAllButEmbedders.run();", hookRuns + 1)).as("run once by the hook").isNegative();
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
        assertThat(reconciler).as("an in-flight CREATE INDEX CONCURRENTLY ignores the interrupt and is left to"
            + " finish in Postgres (nexus-f3yqd), so the hook must not wait for the pass").doesNotContain("awaitTermination");
        String leafBloat = code(Files.readString(Path.of("src/main/java/dev/nexus/service/vectors/LeafBloatSweep.java")));
        assertThat(leafBloat).as("the same holds for the leaf bloat read: it may sit on a silent socket")
            .doesNotContain("awaitTermination");
    }

    /**
     * nexus-43ulx.19: the DDL half is built with the admin values and the boot nonce, starts after the service and
     * after the read half, and stops in the hook right after the listener and BEFORE the pool's reaper, so a pass
     * cannot open a new builder connection after the reaper has ended the old one.
     */
    @Test
    void theReconcilerStartsAfterTheReadHalf_andStopsAfterTheListener_beforeTheReaper() throws Exception {
        String src = code(main());
        int serviceStart = src.indexOf("service.start();");
        int sweepStart = src.indexOf("pciSweep.start();");
        int create = src.indexOf("PciReconciler.create(ds, adminConnection, pciBootNonce, pciSweep,");
        int reconcilerStart = src.indexOf("pciReconciler.start();");
        int hook = shutdownSteps(src);
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
        assertThat(reaper).as("and before the pool's reaper").isGreaterThan(reconcilerStop);
        assertThat(src.indexOf("pciReconciler.start();", reconcilerStart + 1)).as("started once").isNegative();
        assertThat(src.indexOf("stopPciReconciler.run();", reconcilerStop + 1)).as("stopped once").isNegative();
        assertThat(src).as("the placeholder no-op is gone").doesNotContain("stopPciReconciler = () -> { }");
    }

    /**
     * nexus-4lnn1.1.3 (RDR-230): the recall canary's query ring. Every engine builds, binds and starts the writer (not
     * only the builder-lock holder), over the SAME valid-index set the router uses; the reconciler is handed the age
     * bound so that the holder's pass runs it; the hook stops the writer without a flush. Without the bind the handler
     * admits nothing and no test of the ring would notice; without the expiry argument rows on idle rings never age.
     */
    @Test
    void theQueryRingIsBoundAndStartedInEveryEngine_andTheReconcilerGetsItsAgeBound() throws Exception {
        String src = code(main());
        int ringNew = src.indexOf("new dev.nexus.service.vectors.PciQueryRing(tenantScope, pciSweep)");
        int expiryNew = src.indexOf("new dev.nexus.service.vectors.PciQueryRingExpiry(tenantScope, service::ringTenants)");
        int bind = src.indexOf("service.queryRing(pciQueryRing);");
        int ringStart = src.indexOf("pciQueryRing.start();");
        int create = src.indexOf("PciReconciler.create(ds, adminConnection, pciBootNonce, pciSweep,");
        int hook = shutdownSteps(src);
        int ringStop = src.indexOf("pciQueryRing.stop();", hook);

        assertThat(ringNew).as("built over the pool's scope and the router's own index set").isPositive();
        assertThat(expiryNew).as("the age bound takes the reaper-plus-ledger tenants").isPositive();
        assertThat(bind).as("bound to the vector routes after the service exists").isGreaterThan(src.indexOf("var service = new NexusService("));
        assertThat(ringStart).isGreaterThan(bind);
        assertThat(create).as("the reconciler is built after the ring").isGreaterThan(ringStart);
        assertThat(src.substring(create, src.indexOf(";", create))).as("and is handed the age bound")
            .contains("pciQueryRingExpiry::expireAll");
        assertThat(ringStop).as("stopped in the shutdown hook").isGreaterThan(hook);
        assertThat(src.indexOf("pciQueryRing.start();", ringStart + 1)).as("started once").isNegative();
        assertThat(src).as("a stop that flushes would hold the hook on a hung database").doesNotContain("pciQueryRing.flushNow");
    }

    /**
     * nexus-4lnn1.1.4 (RDR-230): the recall canary runs on the builder-lock holder's pass through the SAME repository
     * the vector routes use (its serving statement path, not a copy) and reads the ring in the same scope. Without the
     * two arguments {@code PciReconciler.create} would be the overload with no canary, and nothing else would notice.
     */
    @Test
    void theReconcilerIsHandedTheServingRepositoryAndTheScopeForTheRecallCanary() throws Exception {
        String src = code(main());
        int create = src.indexOf("PciReconciler.create(ds, adminConnection, pciBootNonce, pciSweep,");
        int repo = src.indexOf("var pgVectorRepo = new PgVectorRepository(");
        assertThat(create).isPositive();
        String call = src.substring(create, src.indexOf(";", create));
        assertThat(call).contains("pciQueryRingExpiry::expireAll").contains("pgVectorRepo").contains("tenantScope");
        assertThat(call.indexOf("pgVectorRepo")).as("the repository, then the scope").isLessThan(call.indexOf("tenantScope"));
        assertThat(repo).as("the repository the routes serve from exists before the reconciler is built")
            .isPositive().isLessThan(create);
        assertThat(src.indexOf("new PgVectorRepository(", repo + "var pgVectorRepo = ".length() + 1)).as("one repository, not a second for the canary")
            .isNegative();
    }

    /**
     * nexus-43ulx.23: {@code per_collection_indexes} on {@code /v1/status} is bound in Main from the SAME sweep and
     * reconciler that run (not a copy, not {@code PciIndexSet.NONE}), after both exist. Without the call the route
     * silently omits the key and every client reads "cannot tell"; no other test would notice. nexus-4lnn1.1.5: the
     * maintenance object is read from the same reconciler, per request, in the same call.
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
                + "pciReconciler\\.status\\(\\),\\s*pciReconciler\\.maintenance\\(\\)\\)");
        assertThat(src.indexOf("service.perCollectionIndexes(", bind + 1)).as("bound once").isNegative();
    }

    /**
     * nexus-f3yqd.3: {@code boot_work} on {@code /v1/status}. One registry, built after the service exists, bound to the
     * status route and handed to the reconciler (the first producer), all as plain statements of {@code main}. A
     * dropped {@code service.bootWork(...)} leaves the route reporting an empty list for ever; a dropped
     * {@code .bootWork(...)} on the reconciler leaves the entry empty while a build runs; neither fails any other test.
     * The reconciler's call takes the registry and nothing else: there is no argument that makes the unauthenticated
     * route name a collection (the step carries the build phase only).
     */
    @Test
    void theBootWorkRegistryIsBuiltOnce_boundToTheStatusRoute_andHandedToTheReconcilerAlone() throws Exception {
        String src = code(main());
        int serviceNew = src.indexOf("var service = new NexusService(");
        int registryNew = src.indexOf("var bootWork = new BootWorkRegistry(java.time.Clock.systemUTC());");
        int bind = src.indexOf("service.bootWork(bootWork);");
        int create = src.indexOf("PciReconciler.create(ds, adminConnection, pciBootNonce, pciSweep,");
        int reconcilerStart = src.indexOf("pciReconciler.start();");

        assertThat(serviceNew).isPositive();
        assertThat(registryNew).as("built from the real clock").isGreaterThan(serviceNew);
        assertThat(bind).as("bound to the status route after it is built").isGreaterThan(registryNew);
        assertThat(create).as("the reconciler is built after the registry").isGreaterThan(bind);
        String call = src.substring(create, src.indexOf(";", create));
        assertThat(call).as("and is handed that registry, and nothing else, as its boot_work")
            .containsPattern("\\)\\s*\\.bootWork\\(bootWork\\)$");
        assertThat(reconcilerStart).as("bound before the reconciler starts").isGreaterThan(create);
        assertThat(src.indexOf("var bootWork =", registryNew + 1)).as("one registry").isNegative();
        assertThat(src.indexOf("service.bootWork(", bind + 1)).as("bound once").isNegative();
        assertThat(src.indexOf(".bootWork(", create + call.length())).as("one producer so far").isNegative();

        int mainDepth = depthAt(src, src.indexOf("var ds = new HikariDataSource(hikari);"));
        assertThat(depthAt(src, bind)).as("the bind is at main's top level, not in a block").isEqualTo(mainDepth);
        assertThat(precedingToken(src, bind)).as("it follows a statement, not an if/else/loop head").isIn(";", "}");
    }

    /**
     * nexus-f3yqd.2.17: the data-move boot_work reporter. Started once after the worker, from the registry the status route
     * reads (the same {@code bootWork}), closed in the shared shutdown steps beside the worker's stop; and an engine whose
     * worker did not start says so in boot_work, or nx doctor reads "no data move is open" from an engine that works none.
     * None of those fails any other test, so the text is pinned.
     */
    @Test
    void theDataMoveReporterStartsAfterTheWorker_closesWithIt_andANoWorkerBootSaysSo() throws Exception {
        String src = code(main());
        int worker = src.indexOf("var dataMoveWorker = startDataMoveWorker(");
        int reporter = src.indexOf("var moveBootWork = startMoveBootWork(bootWork, dataMoveWorker, adminConnection);");
        int bind = src.indexOf("service.bootWork(bootWork);");
        int block = shutdownSteps(src);
        int blockEnd = matchingClose(src, src.indexOf('{', block));
        String steps = src.substring(block, blockEnd);

        assertThat(worker).isPositive();
        assertThat(reporter).as("built from the one registry, after the worker it reads").isGreaterThan(worker);
        assertThat(bind).as("that registry is the one the status route reads").isLessThan(reporter);
        assertThat(src.indexOf("var moveBootWork =", reporter + 1)).as("one reporter").isNegative();
        int mainDepth = depthAt(src, src.indexOf("var ds = new HikariDataSource(hikari);"));
        assertThat(depthAt(src, reporter)).as("a top-level statement of main, not under a condition").isEqualTo(mainDepth);
        assertThat(precedingToken(src, reporter)).isIn(";", "}");

        int workerStop = steps.indexOf("dataMoveWorker.stop();");
        int reporterClose = steps.indexOf("moveBootWork.close();");
        assertThat(workerStop).as("the worker is stopped in the shared shutdown steps").isPositive();
        assertThat(reporterClose).as("and the reporter closed after it, in the same steps").isGreaterThan(workerStop);

        int method = src.indexOf("MoveBootWork startMoveBootWork(");
        assertThat(method).isPositive();
        String body = src.substring(method, matchingClose(src, src.indexOf('{', method)));
        int noWorker = body.indexOf("if (worker == null)");
        int says = body.indexOf("MoveBootWork.reportWorkerNotRunning(bootWork);");
        int ret = body.indexOf("return null;", noWorker);
        assertThat(noWorker).isPositive();
        assertThat(says).as("a missing worker is reported to boot_work").isGreaterThan(noWorker);
        assertThat(ret).as("before the method gives up on it").isGreaterThan(says);
        int build = body.indexOf("new dev.nexus.service.datamove.MoveBootWork(");
        assertThat(build).as("and the reporter is built after").isGreaterThan(ret);
        int caught = body.indexOf("catch (Throwable e)", build);
        int saysReporter = body.indexOf("MoveBootWork.reportReporterNotRunning(bootWork);", caught);
        assertThat(caught).as("a reporter that cannot be built or started is caught").isGreaterThan(build);
        assertThat(saysReporter).as("and boot_work says so, or nx doctor reads green over unseen moves")
            .isGreaterThan(caught);
        assertThat(body.indexOf("return null;", saysReporter)).as("before the method gives up on it").isPositive();
    }

    /**
     * nexus-dxsjc: the leaf bloat sweep behind {@code chunks_leaf_bloat} on {@code /v1/status}. Without the start call
     * the cache never fills and the doctor row reads the object as "reads failing" forever; without the bind the route
     * omits the key and every client reads "cannot tell"; without the stop the daemon thread outlives the pool. None
     * of those fails any other test, so the text is pinned: built from the pool and the validated settings, started
     * once after the service, bound once from the SAME sweep's own status before the service is declared ready, and
     * stopped once in the hook after the reaper and before the pool closes. All plain statements, none guarded.
     */
    @Test
    void theLeafBloatSweepIsStartedAfterTheService_boundToTheStatusRoute_andStoppedBeforeThePoolCloses()
            throws Exception {
        String src = code(main());
        int serviceStart = src.indexOf("service.start();");
        int create = src.indexOf("var leafBloatSweep = dev.nexus.service.vectors.LeafBloatSweep.create("
            + "ds, dev.nexus.service.db.PgSession.startupPciSettings());");
        int start = src.indexOf("leafBloatSweep.start();");
        int bind = src.indexOf("service.chunksLeafBloat(");
        int ready = src.indexOf("event=service_ready");
        int hook = shutdownSteps(src);
        int listenerStop = src.indexOf("service.stop();", hook);
        int reaper = src.indexOf("BackendReaper.terminateAtShutdown(", hook);
        int stop = src.indexOf("leafBloatSweep.stop();", hook);
        int poolClose = src.indexOf("ds.close();", hook);

        assertThat(serviceStart).isPositive();
        assertThat(create).as("built from the pool and the validated settings").isGreaterThan(serviceStart);
        assertThat(start).as("started after it is built").isGreaterThan(create);
        assertThat(bind).as("bound after the sweep is started").isGreaterThan(start);
        assertThat(ready).as("and before the service is declared ready").isGreaterThan(bind);
        assertThat(src).as("served from the running sweep's own status and period, nothing re-read")
            .containsPattern("service\\.chunksLeafBloat\\(\\(\\) -> dev\\.nexus\\.service\\.http\\."
                + "StatusHandler\\.ChunksLeafBloat\\.of\\(\\s*leafBloatSweep\\.status\\(\\),\\s*"
                + "leafBloatSweep\\.periodSeconds\\(\\)\\)\\);");
        assertThat(hook).isGreaterThan(start);
        assertThat(reaper).isGreaterThan(listenerStop);
        assertThat(stop).as("stopped after the reaper, so a read that ignores the interrupt cannot delay it")
            .isGreaterThan(reaper);
        assertThat(poolClose).as("before the hook closes the pool, which aborts a stuck read").isGreaterThan(stop);
        assertThat(src.indexOf("var leafBloatSweep =", create + 1)).as("built once").isNegative();
        assertThat(src.indexOf("leafBloatSweep.start();", start + 1)).as("started once").isNegative();
        assertThat(src.indexOf("service.chunksLeafBloat(", bind + 1)).as("bound once").isNegative();
        assertThat(src.indexOf("leafBloatSweep.stop();", stop + 1)).as("stopped once").isNegative();

        int mainDepth = depthAt(src, src.indexOf("var ds = new HikariDataSource(hikari);"));
        assertThat(depthAt(src, start)).as("leafBloatSweep.start() is at main's top level, not in a block")
            .isEqualTo(mainDepth);
        assertThat(precedingToken(src, start)).as("it follows a statement, not an if/else/loop head")
            .isIn(";", "}");
        assertThat(depthAt(src, bind)).as("the bind is at main's top level").isEqualTo(mainDepth);
        assertThat(depthAt(src, stop)).as("leafBloatSweep.stop() is at the hook's top level, beside service.stop()")
            .isEqualTo(depthAt(src, listenerStop));
        assertThat(precedingToken(src, stop)).as("it follows a statement, not an if/else/loop head").isIn(";", "}");
    }

    /**
     * nexus-f3yqd: the shutdown hook ends the POOL's backends and nothing of the index builder's. The call takes the
     * pool's values only; Main no longer names the builder's {@code application_name}, so a build in flight finishes in
     * Postgres and the next engine adopts it. Pinned on the arguments, since {@code main} cannot run in a test.
     */
    @Test
    void theShutdownReaperIsHandedThePoolValuesOnly_andMainNamesNoBuilderBackend() throws Exception {
        String src = code(main());
        int hook = shutdownSteps(src);

        assertThat(src.substring(hook)).as("inside the hook, the one five-argument call")
            .containsPattern("BackendReaper\\.terminateAtShutdown\\(\\s*dbUrl,\\s*dbUser,\\s*dbPass,\\s*"
                + "applicationName,\\s*ds\\.getHikariPoolMXBean\\(\\)\\.getActiveConnections\\(\\)\\)");
        assertThat(src).as("Main holds no builder application name to reap")
            .doesNotContain("pciBuilderApplicationName").doesNotContain("builderApplicationName");
        assertThat(src).as("the admin values are the ones resolved from NX_DB_ADMIN_* once, at boot")
            .containsPattern("var adminConnection = dev\\.nexus\\.service\\.db\\.AdminConnection\\.resolve\\("
                + "System::getenv,\\s*dbUrl,\\s*dbUser,\\s*dbPass\\);");
        assertThat(src).as("and the nonce is the pool name's own last segment, the one the reconciler is built with")
            .containsPattern("pciBootNonce\\s*=\\s*dev\\.nexus\\.service\\.db\\.BackendReaper\\."
                + "bootNonce\\(applicationName\\);")
            .containsPattern("PciReconciler\\.create\\(ds,\\s*adminConnection,\\s*pciBootNonce,");
        assertThat(src.indexOf("adminConnection =", src.indexOf("adminConnection =") + 1))
            .as("adminConnection is assigned once").isNegative();
    }

    /** {@code terminateAtShutdown} is the pool's reap alone: it takes no admin values and no builder name. */
    @Test
    void theShutdownReapTakesNoAdminValuesAndNoBuilderName() throws Exception {
        String reaper = code(Files.readString(Path.of("src/main/java/dev/nexus/service/db/BackendReaper.java")));
        assertThat(reaper).containsPattern("public static int terminateAtShutdown\\(String poolUrl,\\s*String poolUser,\\s*"
            + "String poolPassword,\\s*String poolApplicationName,\\s*int poolActive\\)\\s*\\{\\s*"
            + "return terminateOwnBackends\\(poolUrl,\\s*poolUser,\\s*poolPassword,\\s*poolApplicationName,\\s*"
            + "poolActive\\);");
        assertThat(reaper).as("nothing in the reaper reaches the builder's backend")
            .doesNotContain("AdminConnection").doesNotContain("builderApplicationName");
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

    /**
     * RDR-229 P3.4 (nexus-p2m1x.22): the completion check reads gate 3 from the PCI reconciler ({@code gate3Source()}), and the
     * reconciler starts no id build until the check's LATEST read says gate 2 holds (a null read holds nothing).
     */
    @Test
    void theCompletionCheckReadsGate3FromTheReconciler_andTheReconcilersIdBuildsWaitForGate2() throws Exception {
        String src = code(main());

        assertThat(src).as("gate 3's source is the reconciler's, not NOT_INSTALLED")
            .contains("startCompletionBootWork(bootWork, adminConnection, pciReconciler.gate3Source(), gate3Latch)");
        // linda 2026-11-23: the latch is read synchronously BEFORE the listener binds, and a read that cannot answer ends the boot.
        int latchRead = src.indexOf("Gate3Latch.readAtBoot(adminConnection)");
        int listener = src.indexOf("service.start();");
        assertThat(latchRead).as("the latch is read at boot").isPositive();
        assertThat(latchRead).as("before the listener binds").isLessThan(listener);
        String failure = src.substring(latchRead, listener);
        assertThat(failure).contains("rdr229_gate3_latch_boot_read_failed").contains("System.exit(1)");
        assertThat(src).as("a latch the check never read is never guessed: the reporter is handed the one read at boot")
            .contains("CompletionCheck.Gate3LatchWriter.IMMEDIATE, gate3Latch");
        assertThat(src).as("a latch written while PCI was off makes the reconciler build the id indexes first")
            .contains("pciReconciler.buildIdIndexesFirstWhen(() -> gate3Latch.pciOff() || completionCheck != null && completionCheck.gate3LatchedPciOff())");
        int gate = src.indexOf("pciReconciler.idBuildGate(");
        assertThat(gate).as("the id-build gate is wired").isPositive();
        assertThat(src.indexOf("var completionCheck = startCompletionBootWork(")).isLessThan(gate);
        String lambda = src.substring(gate, src.indexOf("});", gate));
        assertThat(lambda).contains("completionCheck.latest()").contains("latest != null").contains("latest.gate2()");
        assertThat(src.indexOf("pciReconciler.idBuildGate(", gate + 1)).as("wired once").isNegative();
        int retire = src.indexOf("pciReconciler.retireNameIndexesWhen(");
        assertThat(retire).as("the name family retires on the latched gate 3").isPositive();
        String retireLambda = src.substring(retire, src.indexOf(");", retire) + 2);   // the one-line call, not the next block
        assertThat(retireLambda.length()).as("bounded to the call").isLessThan(200);
        assertThat(retireLambda).as("the LATCHED value, never the live gate 3").contains("completionCheck.gate3Latched()")
            .doesNotContain(".gate3()");
        assertThat(retireLambda).as("and the latch read at boot answers before the check exists (or when it never started)")
            .contains("gate3Latch.latched() ||");
        assertThat(retireLambda).as("and only an engine that took the singleton lock retires: with NX_ENGINE_SINGLETON_LOCK=off or a pooler a peer may serve names")
            .contains("singletonLock != null &&");
    }

    /**
     * RDR-229 P3.5a (nexus-p2m1x.23), review SB1 (linda 2026-11-23): the ONE {@code Gate3} Main hands the service, which hands it on to
     * the rename guard, the route boundary and the repositories, is the LATCHED signal: the boot latch (read before the service is
     * built, so the signal has no unset state) until the completion check exists, then the check's {@code gate3Latched()}, which never
     * returns to false. It is never the live {@code gate3()}, and there is no second lambda. The registry takes no {@code Gate3}.
     */
    @Test
    void theRoutesGate3IsTheLatchedAccessor_oneInstance_neverTheLiveGate3() throws Exception {
        String src = code(main());

        assertThat(src).as("the signal: the check's latched value once it exists, the boot latch before; elsinore's a46a1bf70e text")
            .containsPattern("dev\\.nexus\\.service\\.datamove\\.Gate3\\s+gate3\\s*=\\s*\\(\\)\\s*->\\s*\\{\\s*var\\s+c\\s*=\\s*completionRef\\.get\\(\\);"
                + "\\s*return\\s+c\\s*!=\\s*null\\s*\\?\\s*c\\.gate3Latched\\(\\)\\s*:\\s*bootLatch\\.latched\\(\\);\\s*\\};");
        assertThat(src.split("Gate3\\s+gate3\\s*=", -1).length - 1).as("one Gate3 instance, no second lambda").isEqualTo(1);
        assertThat(src).as("never the live gate 3, never a never-latched signal").doesNotContain(".gate3()").doesNotContain("Gate3.never()");

        int latchRead = src.indexOf("Gate3Latch.readAtBoot(adminConnection)");
        int bootLatch = src.indexOf("final var bootLatch = gate3Latch;");
        int signal = src.indexOf("Gate3 gate3 =");
        int service = src.indexOf("new NexusService(port, token, ds,");
        int start = src.indexOf("service.start();");
        assertThat(latchRead).as("the boot latch is read").isPositive();
        assertThat(bootLatch).as("the signal closes over it").isGreaterThan(latchRead);
        assertThat(signal).isGreaterThan(bootLatch);
        assertThat(service).as("the latch is read before the service is built: no unset state").isGreaterThan(signal);
        assertThat(start).isGreaterThan(service);
        assertThat(src.substring(service, src.indexOf(";", service)))
            .as("the service takes the one signal and the readiness").containsPattern("gate3,\\s*renameReadiness\\)");

        int check = src.indexOf("var completionCheck = startCompletionBootWork(");
        int handOver = src.indexOf("completionRef.set(completionCheck);");
        assertThat(handOver).as("the signal moves to the check's latch as soon as the check is built").isGreaterThan(check);

        assertThat(src).as("an unset readiness fails closed: the drop move is missing")
            .containsPattern("c\\s*==\\s*null\\s*\\?\\s*dev\\.nexus\\.service\\.datamove\\.RenameReadiness\\.Missing\\.NAME_KEY_DROP_MOVE");

        String service2 = code(Files.readString(Path.of("src/main/java/dev/nexus/service/NexusService.java")));
        assertThat(service2).as("one guard, built once, over the readiness and the fail-closed one-row catalog rename (P3.7 replaces it here)")
            .containsOnlyOnce("new dev.nexus.service.db.RenameGuard(")
            .contains("new dev.nexus.service.db.RenameGuard(renameReadiness, new dev.nexus.service.db.CollectionOneRowRename(gate3))")
            .contains("new CatalogRepository(tenantScope, gate3, renameGuard)")
            .as("the registry takes no Gate3").contains("CollectionRegistry.shared()").doesNotContain("CollectionRegistry.shared(gate3)");
    }

    /**
     * RDR-229 P3.7 (linda 2026-11-23, stage-2 item 1): production refuses a rename before completion. The engine {@code Main} builds hands the rename
     * readiness to the {@code NexusService} as a lambda over the completion check, and until the check exists that lambda answers the drop move is missing,
     * so a rename on a latched install is refused (fails closed); once it exists it answers the check's own readiness.
     */
    @Test
    void productionMainBuildsTheServiceWithTheCompletionChecksReadiness_andRefusesARenameBeforeCompletion() throws Exception {
        String src = code(main());
        assertThat(src).as("the readiness is the check's, read per call").contains("c.renameReadiness().missing()");
        assertThat(src).as("and unset it says the drop move is missing: a rename on a latched install is refused")
            .contains("c == null ? dev.nexus.service.datamove.RenameReadiness.Missing.NAME_KEY_DROP_MOVE");
        assertThat(src).as("the service is built with the one signal and the readiness").containsPattern("gate3,\\s*renameReadiness\\)");
    }

    /**
     * RDR-229 P3.7 (nexus-p2m1x.27): the name-key drop move's registration pass starts once, at main's top level, after the completion check it
     * reads, handed that check's latest read, and closes in the shutdown steps; its method returns null without a completion check.
     */
    @Test
    void theNameKeyDropPassStartsAfterTheCompletionCheck_readsItsLatest_closesInTheShutdownSteps_andNeedsACompletionCheck() throws Exception {
        String src = code(main());
        int check = src.indexOf("var completionCheck = startCompletionBootWork(");
        int start = src.indexOf("var nameKeyDropPass = startNameKeyDropPass(");
        int hook = shutdownSteps(src);
        int close = src.indexOf("nameKeyDropPass.close();", hook);

        assertThat(check).isPositive();
        assertThat(start).as("started after the completion check it reads").isGreaterThan(check);
        assertThat(src.indexOf("var nameKeyDropPass = startNameKeyDropPass(", start + 1)).as("started once").isNegative();
        int mainDepth = depthAt(src, src.indexOf("var ds = new HikariDataSource(hikari);"));
        assertThat(depthAt(src, start)).as("a top-level statement of main").isEqualTo(mainDepth);
        assertThat(src).as("handed the check the engine built").contains("startNameKeyDropPass(adminConnection, completionCheck)");
        assertThat(close).as("closed in the shared shutdown steps").isGreaterThan(hook);
        assertThat(src.substring(src.lastIndexOf("if (nameKeyDropPass != null)", close), close)).doesNotContain("else");

        int method = src.indexOf("static dev.nexus.service.datamove.NameKeyDropPass startNameKeyDropPass(");
        assertThat(method).isPositive();
        int body = src.indexOf('{', src.indexOf(')', src.indexOf("completionCheck)", method)));
        String text = src.substring(body, matchingClose(src, body));
        int noCheck = text.indexOf("if (completionCheck == null) {");
        assertThat(noCheck).as("no completion check, no pass: and said so").isNotNegative();
        assertThat(text.substring(noCheck, text.indexOf('}', noCheck))).contains("name_key_drop_pass_not_started").contains("return null;");
        assertThat(text.indexOf("new dev.nexus.service.datamove.NameKeyDropPass(")).as("the pass is built after the guard").isGreaterThan(noCheck);
        assertThat(text).as("reads the check's latest read, not a snapshot held here").contains("completionCheck::latest");
    }

    /**
     * RDR-229 P3.3b (nexus-p2m1x.20): the per-leaf id unique-index pass is started once, at main's top level, after the completion check
     * it reads, handed that check's LATEST read of gate 1 (a null read holds nothing), and closed in the shutdown steps; and its method
     * returns null without a completion check, before it builds the pass. P3.3c (nexus-p2m1x.21) deleted the placeholder constant and its
     * guard: the pass starts under its other conditions only, and {@code MainLeafIndexPassGuardTest} holds that.
     */
    @Test
    void theLeafIndexPassStartsAfterTheCompletionCheck_readsItsLatestGate1_closesInTheShutdownSteps_andNeedsACompletionCheck() throws Exception {
        String src = code(main());
        int check = src.indexOf("var completionCheck = startCompletionBootWork(");
        int start = src.indexOf("var leafIndexPass = startLeafIndexPass(");
        int hook = shutdownSteps(src);
        int close = src.indexOf("leafIndexPass.close();", hook);
        int reaper = src.indexOf("BackendReaper.terminateAtShutdown(", hook);

        assertThat(check).isPositive();
        assertThat(start).as("started after the completion check it reads").isGreaterThan(check);
        assertThat(src.indexOf("var leafIndexPass = startLeafIndexPass(", start + 1)).as("started once").isNegative();
        int mainDepth = depthAt(src, src.indexOf("var ds = new HikariDataSource(hikari);"));
        assertThat(depthAt(src, start)).as("a top-level statement of main, whatever NX_SEARCH_PCI says").isEqualTo(mainDepth);
        assertThat(precedingToken(src, start)).isIn(";", "}");
        assertThat(src).as("handed the check the engine built").contains("startLeafIndexPass(bootWork, adminConnection, completionCheck)");
        assertThat(close).as("closed in the shared shutdown steps").isGreaterThan(hook);
        assertThat(close).as("before the reaper").isLessThan(reaper);
        assertThat(depthAt(src, close)).as("a statement of the null guard inside the steps")
            .isEqualTo(depthAt(src, src.indexOf("service.stop();", hook)) + 1);
        assertThat(src.substring(src.lastIndexOf("if (leafIndexPass != null)", close), close)).doesNotContain("else");

        int method = src.indexOf("static dev.nexus.service.vectors.LeafIndexPass startLeafIndexPass(");
        assertThat(method).isPositive();
        int body = src.indexOf('{', src.indexOf(')', src.indexOf("completionCheck)", method)));
        String text = src.substring(body, matchingClose(src, body));
        int noCheck = text.indexOf("if (completionCheck == null) {");
        int build = text.indexOf("new dev.nexus.service.vectors.LeafIndexPass(");
        assertThat(text).as("no placeholder guard: P3.3c deleted it").doesNotContain("PLACEHOLDER");
        assertThat(noCheck).as("no completion check, no pass: and said so").isNotNegative();
        assertThat(text.substring(noCheck, text.indexOf('}', noCheck))).contains("leaf_index_pass_not_started").contains("return null;");
        assertThat(build).as("the pass is built after the guard").isGreaterThan(noCheck);
        assertThat(text).as("the gate is the check's latest read; null holds nothing")
            .contains("completionCheck.latest()").contains("latest != null && latest.gate1()");
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
