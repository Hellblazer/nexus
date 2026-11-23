// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;
import java.util.List;

/** The two tables, as data. Evictor names are the ones {@code NexusService.tenantKeyedState()} registers. */
final class TenantKeyedStateTables {
    private TenantKeyedStateTables() { }

    private static final String TUPLE_WAITS = "TupleWaitRegistry";
    private static final String TOKEN_CACHE = "TokenCache";
    private static final String COLLECTIONS = "CollectionRegistry";
    private static final String GHOST_SWEEP = "CatalogRepository.ghostSweepCheckedTenants";
    private static final String MINT_RATE = "MintRateLimiter";
    private static final String OWNERLESS_LOG = "OwnerlessLogLimiter";
    private static final String REAPER = "ChunkReaper";
    private static final String DATA_SWEEP = "NexusService.dataSweepFailuresByTenant";
    private static final String CHUNK_KEYS_JOBS = "ChunkKeysSweepJobs";
    private static final String RECONCILE_JOBS = "EngineJobs.reconcile";
    private static final String MANIFEST_FILL_JOBS = "EngineJobs.manifestFill";
    private static final String VALIDATE_JOBS = "ValidateConstraintsJobs";

    private static final String HANDLE =
        "a TenantScope handle: it holds the DataSource wrapper only, the tenant is a per-call argument";
    private static final String CONSTANT = "a constant naming something (a GUC, header, env variable, scope or "
        + "default tenant); it holds no state";
    private static final String PCI_OFF = "PCI index state: populated only when NX_SEARCH_PCI=1, and the tenant "
        + "reset route answers 503 in that mode, so it is never carried across a reset";
    private static final String LEAF_BLOAT = "leaf bloat view (nexus-dxsjc): the sweep's last read of "
        + "pg_stat_user_tables, replaced as a whole on every read, so a leaf that is gone (its name carries a hash "
        + "of the tenant), as after a tenant reset, drops out at the next read; the status route serves counts "
        + "only";
    static final String RESULT = "a field of an immutable per-call value (result, parameter, outcome or "
        + "exception) built for one request or one pass and dropped with it; no long-lived structure holds it";
    private static final String COUNTER = "a process-wide counter or flag since boot, or a pass counter; not keyed "
        + "by tenant, and a data reset does not reset counters";
    private static final String COUNT_VIEW = "a bounded since-boot count view by route and key class: both "
        + "labels are fixed by the engine (route names and four classes), no key name or client text is kept, and "
        + "a request's tenant is never recorded in it, so a tenant reset has nothing to drop";
    private static final String TEST_HOOK = "a test-only hook, null in production";
    private static final String EXPORT_REGISTRY = "the tenant export's schema registry (nexus-csayy.1): "
        + "table and column names fixed at class load, the same for every tenant; not keyed by tenant";
    private static final String CONFIG = "process configuration set at boot or swapped as a whole; not keyed by "
        + "tenant";
    private static final String TEMPLATE = "tuple template schema, parsed at boot and immutable; "
        + "tenant-independent";
    private static final String ORT = "ONNX runtime init state, one per process; tenant-independent";
    private static final String JOOQ_COLUMN = "a jOOQ column reference to tenant_id, a constant naming the "
        + "column; it holds no state";
    private static final String PRINCIPAL = "an immutable value naming a tenant, carried on one request's or one "
        + "sweep step's stack; the long-lived holders of tenant principals are the keyed entries (TokenCache.map)";
    private static final String DATA_MOVE_STATES = "the data-move worker's view of its open moves, by move label (kind, "
        + "reason, times): nexus.data_moves is a global registry with no tenant column, so nothing here is a tenant's";
    private static final String DATA_MOVE_REPORT = "the data-move boot_work reporter's own bookkeeping, by move label or "
        + "boot_work id (counts, times, what one sample read): nexus.data_moves is a global registry with no tenant column, "
        + "so nothing here is a tenant's";
    private static final String DATA_MOVE_REPORT_SESSION = "the data-move boot_work reporter's own admin connection, kept "
        + "between samples; it reads the global registry and the dml_utils runs and carries no tenant setting";
    private static final String DATA_MOVE_HANDLE = "the data-move worker's own thread, one per engine; holds no data";
    private static final String DATA_MOVE_CALL = "the data-move worker's one call connection (or the statement in flight "
        + "on it), kept so a stop can cancel it; it carries a tenant setting only for the length of one run, cleared (or "
        + "the connection closed) after it, and holds no tenant data";
    private static final String REQUEST = "state of one search arm; dies with the request";
    private static final String EVICTOR_LIST = "the evictor list itself: an immutable List.copyOf of stateless "
        + "evictors";
    private static final String TOKEN_EPOCH = "a counter that only moves forward: invalidateTenant bumps it and "
        + "resolve compares it, so no tenant's data is in it";
    private static final String MULTI_GROUPS = "the wait groups one parked MultiWaiter registered in, so its "
        + "release decrements those and never a fresh group of the same key; it dies with that wait, and "
        + "TupleWaitRegistry.abortTenant reaches the waiter through the group's multiListeners";
    private static final String WAITER_LOCAL = "the subspaces one parked wait asked for, immutable; dies when "
        + "that request ends";
    private static final String REAPER_COUNTS = "the previous completed pass's tenant COUNTS (ints) and its end "
        + "time: no tenant identity, replaced by every pass";
    private static final String REAPER_LAST_RUN = "the previous completed pass's per-tenant result (tenant "
        + "names, collection names, refusal reasons), replaced wholesale by every pass and read only by tests "
        + "(ChunkReaper.lastRun); it can name a reset tenant until the next pass and nothing in the engine "
        + "decides on it, so it is not a cache a reset must clear";

    private static final String SINGLETON_LOCK = "the engine's one-engine-per-database lock (nexus-5tykh): its "
        + "own JDBC connection, the two callbacks Main hands it and a test-adjustable wait; one per process, "
        + "tenant-independent. Its "
        + "regain hook is what clears CollectionRegistry for every tenant";

    private static final String TOKEN_CACHE_REF = "a reference to the TokenCache, which is evicted under "
        + "TokenCache.map";

    static void fill(Map<String, String> keyed, Map<String, String> notKeyed) {
        // Keyed: a reset must clear these for the tenant.
        keyed.put("TupleWaitRegistry.groups", TUPLE_WAITS);
        keyed.put("TupleWaitRegistry.Group.multiListeners", TUPLE_WAITS);
        keyed.put("TupleWaitRegistry.Group.currentWaiter", TUPLE_WAITS);
        keyed.put("TupleWaitRegistry.perClaimantParked", TUPLE_WAITS);   // a count of live parks; aborting forces zero
        keyed.put("TokenCache.map", TOKEN_CACHE);
        keyed.put("CollectionRegistry.KNOWN", COLLECTIONS);
        // RDR-229 P3.5a (nexus-p2m1x.23): the id index beside the label index, filled and dropped with it. The two instance
        // fields are the maps one registry reads: the process-wide registry's alias KNOWN and KNOWN_BY_ID (evicted by the one
        // evictor), and a registry a test builds with new owns an isolated pair that no tenant reset reaches.
        keyed.put("CollectionRegistry.KNOWN_BY_ID", COLLECTIONS);
        keyed.put("CollectionRegistry.known", COLLECTIONS);
        keyed.put("CollectionRegistry.knownById", COLLECTIONS);
        keyed.put("CatalogRepository.ghostSweepCheckedTenants", GHOST_SWEEP);
        keyed.put("MintRateLimiter.credentials", MINT_RATE);
        keyed.put("MintRateLimiter.CredentialState.tenants", MINT_RATE);
        keyed.put("OwnerlessLogLimiter.states", OWNERLESS_LOG);
        keyed.put("ChunkReaper.timeoutStreaks", REAPER);
        keyed.put("ChunkReaper.resumeTenant", REAPER);
        keyed.put("ChunkReaper.resumeCollection", REAPER);
        keyed.put("ChunkReaper.resumeExpiry", REAPER);
        // RDR-252 Step 3 (nexus-igea9): the owner stamper's armed ack deadlines, one scheduled task per (tenant, base topic).
        keyed.put("AckDeadlineTimer.armed", "AckDeadlineTimer");
        keyed.put("AckDeadlineTimer.failures", "AckDeadlineTimer");
        keyed.put("NexusService.dataSweepFailuresByTenant", DATA_SWEEP);
        TenantKeyedStateCoverageEntries.fill(keyed, notKeyed);   // nexus-7fku2.1.2: TaxonomyCoverage and its invalidation helper
        keyed.put("TupleWaitRegistry.WaitKey.tenant", TUPLE_WAITS);   // the key of groups
        keyed.put("TokenCache.Entry.tenantId", TOKEN_CACHE);   // the value of map
        keyed.put("ChunkKeysSweepJobs.Job.callerTenant", CHUNK_KEYS_JOBS);   // the starter of a sweep job (nexus-v75nw)
        keyed.put("ChunkKeysSweepJobs.Outcome.body", CHUNK_KEYS_JOBS);   // the counts, held by the job's outcome
        // The shared job registry (RDR-193 Step 1). One field, one evictor name: each instance is evicted by its
        // own evictor (the chunk-keys instance through ChunkKeysSweepJobs.evictTenant, the reconcile instance as
        // EngineJobs.reconcile), and both reach these fields through EngineJobs.evictWhere.
        keyed.put("EngineJobs.jobs", RECONCILE_JOBS);   // the jobs of one instance
        keyed.put("EngineJobs.Job.tenant", RECONCILE_JOBS);   // the value of jobs
        keyed.put("EngineJobs.Job.outcome", RECONCILE_JOBS);   // the value of jobs: the tenant's result
        keyed.put("EngineJobs.Outcome.body", RECONCILE_JOBS);   // the result, held by the job's outcome
        // The manifest fill instance (nexus-f3yqd, R2) is a third EngineJobs instance, evicted as EngineJobs.manifestFill:
        // what its jobs keep on each job is the job's progress, a latest-snapshot slot the work replaces whole.
        keyed.put("ManifestFillJobs.Progress.snapshot", MANIFEST_FILL_JOBS);   // a job's progress: counts and a cursor

        // Not keyed: each with the reason it holds nothing a reset must clear.
        notKeyed.put("EngineJobs.Job.state", "a job's lifecycle enum (running, done, failed); no tenant's data, and the "
            + "job that holds it is evicted under EngineJobs.jobs");
        notKeyed.put("NexusService.chunkKeysSweepForTests", TEST_HOOK);
        notKeyed.put("CollectionTupleLock.Tuple.tenantId", "one component of a tuple a caller passes to take a per-tuple "
            + "advisory lock for that transaction; never stored, so a tenant reset has nothing to drop");
        notKeyed.put("RestoreIdFill.Filled.map", "one restore load's source-to-target id map, a value returned from a table's fill and dropped when the load ends (RDR-229 P3.11); never cached");
        notKeyed.put("RestoreIdFill.Filled.relabelled", "one restore load's relabels, a value returned from the registry's fill and reported once (RDR-229 P3.11); never cached");
        notKeyed.put("TenantImportSessions.servedModels", "the router's served-model supplier; holds no tenant's data");
        notKeyed.put("NexusService.chunkKeysCredentialLiveForTests", TEST_HOOK);
        notKeyed.put("EngineJobs.maxRuntime", "the wall-clock cap a job is started with (a configured duration, "
            + "settable by a test): no tenant's data");
        for (String f : List.of("NexusService.tenantScope", "TaxonomyCentroidRepository.tenantScope",
            "PgVectorRepository.tenantScope", "ReaperRepository.tenantScope", "WhoamiHandler.tenantScope",
            "RemapRepository.tenantScope", "CombinedWriteService.tenantScope", "TaxonomyRepository.tenantScope", "TaxonomyAnalytics.tenantScope",
            "TupleRepository.tenantScope", "TelemetryRepository.tenantScope", "LadderRepository.tenantScope",
            "PlanRepository.tenantScope", "MemoryRepository.tenantScope", "PipelineRepository.tenantScope",
            "CatalogRepository.tenantScope", "CatalogLinksRepository.tenantScope", "ChashRepository.tenantScope", "AspectRepository.tenantScope",
            "ScratchRepository.tenantScope", "ManifestLabelStage.tenantScope")) {
            notKeyed.put(f, HANDLE);
        }
        for (String f : List.of("NexusService.TUPLE_SWEEP_MAX_BATCHES_PER_TENANT_ENV",
            "NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT", "NexusService.DEFAULT_TENANT",
            "HttpUtil.TENANT_PARTITION_MISSING_REASON", "HttpUtil.TENANT_CREATION_BUSY_REASON",
            "HttpUtil.TENANT_CREATION_RETRY_AFTER_SECONDS", "HttpUtil.TENANT_CREATION_RESTORE_RETRY_AFTER_SECONDS",
            "AuthFilter.BOOTSTRAP_ANY_TENANT",
            "AuthFilter.TENANT_HEADER", "AuthFilter.MDC_TENANT", "TenantConstants.DEFAULT_TENANT", "TenantConstants.BOOTSTRAP_ANY_TENANT",
            "ChunksIsolationCheck.TENANT_POLICY", "TokenStore.SCOPE_TENANT", "TenantScope.DEFAULT_TENANT_GUC",
            "TenantScope.T1_TENANT_GUC", "ScratchRepository.T1_TENANT_GUC", "PciReconciler.SET_LOCAL_TENANT",
            "MintRateLimiter.MAX_TENANT_BUCKETS_PER_CREDENTIAL", "MintRateLimiter.tenantBurst",
            "MintRateLimiter.tenantSustainedPerMinute")) {
            notKeyed.put(f, CONSTANT);
        }
        notKeyed.put("AuditTags.TITLE_PATTERNS", "compiled audit-title patterns built once from the generated "
            + "AuditTags table (RDR-238 P1); the same for every tenant, never written after class init");
        // nexus-6pfgb.43: the retire-keys routes hold nothing between requests.
        for (String f : List.of("ChunkKeySweep.DryRun.tenant", "ChunkKeySweep.DryRun.collections",
            "ChunkKeySweep.DryRun.preconditions", "ChunkKeySweep.DryRun.blocking", "ChunkKeySweep.Applied.tenant",
            "ChunkKeySweep.Applied.blocked", "ChunkKeySweep.DryRun.details", "ChunkKeySweep.DryRun.accepted",
            "ChunkKeySweep.DryRun.samples", "ChunkKeySweep.Preconditions.counts",
            "ChunkKeySweep.Preconditions.details", "ChunkKeySweep.Resume.collections",
            "ChunkKeySweep.Resume.blocked")) {
            notKeyed.put(f, "a component of one sweep call's result record, built for that request and dropped "
                + "with its response; nothing keeps it between requests");
        }
        notKeyed.put("CatalogSensitiveReads.Page.documents", RESULT);   // nexus-f3yqd: one sensitive-list call's documents
        // nexus-uwggq.2.3-.2.5, .1.5: the RDR-241 Phase 2 catalog read routes, one page or one parsed filter per request
        notKeyed.put("CatalogDocReads.Page.documents", RESULT);
        notKeyed.put("CatalogMoves.Counts.absorbedTumblers", RESULT);   // RDR-254: one move's absorbed rows
        notKeyed.put("CatalogMoves.Report.asMap", RESULT);              // RDR-254: one move's report
        notKeyed.put("CatalogMoves.lockOrderHookForTests", TEST_HOOK);
        notKeyed.put("CatalogMoves.afterWritesHookForTests", TEST_HOOK);
        notKeyed.put("CatalogMoves.FoldPlan.pairsT", RESULT);   // RDR-254: the fold read for one owner move
        notKeyed.put("CatalogMoves.FoldPlan.pairsS", RESULT);   // RDR-254: the fold read for one owner move
        notKeyed.put("CatalogMoves.FoldPlan.orphans", RESULT);   // RDR-254: the fold read for one owner move
        notKeyed.put("CatalogMoves.FoldPlan.linked", RESULT);   // RDR-254: the fold read for one owner move
        notKeyed.put("CatalogMoves.FoldPlan.aliased", RESULT);   // RDR-254: the fold read for one owner move
        notKeyed.put("CatalogMoves.FoldPlan.withHighlights", RESULT);   // RDR-254: the fold read for one owner move
        notKeyed.put("CatalogDocReads.MetaFilter.equalities", RESULT);
        notKeyed.put("RenameOutcome.counts", RESULT);   // RDR-229 P3.5a: one rename's per-table counts
        notKeyed.put("CollectionSelectors.Route.slots", "a row of the route table, fixed when the class loads "
            + "(RDR-229 P3.5a): which request fields of one route name a collection; holds no tenant");
        notKeyed.put("CollectionSelectors.Slot.labelFields", "a row of the route table, fixed when the class loads "
            + "(RDR-229 P3.5a): the names one collection field is read under; holds no tenant");
        notKeyed.put("CatalogDocReads.MetaFilter.bounds", RESULT);
        notKeyed.put("CatalogSensitiveLocatorReads.Page.documents", RESULT);
        notKeyed.put("PdfStubMetadataReads.Page.collections", RESULT);   // nexus-f3yqd: one stub-metadata call's collections
        notKeyed.put("MemorySearchPage.Page.rows", RESULT);   // nexus-1gdv0.1.1: one memory-search call's page of rows
        // nexus-f3yqd.2.13 (RDR-246): the data-move worker. The registry it works is global, with no tenant column.
        notKeyed.put("MoveStates.byLabel", DATA_MOVE_STATES);
        notKeyed.put("MoveStates.calls", DATA_MOVE_STATES);   // nexus-f3yqd.2.17: the CALL in flight, by move label
        notKeyed.put("MoveBootWork.DbReader.session", DATA_MOVE_REPORT_SESSION);
        notKeyed.put("MoveBootWork.published", DATA_MOVE_REPORT);
        notKeyed.put("MoveBootWork.highestDone", DATA_MOVE_REPORT);
        notKeyed.put("MoveBootWork.lastAdvance", DATA_MOVE_REPORT);
        notKeyed.put("MoveBootWork.Snapshot.open", DATA_MOVE_REPORT);
        notKeyed.put("MoveBootWork.Snapshot.failed", DATA_MOVE_REPORT);
        notKeyed.put("MoveBootWork.Snapshot.progress", DATA_MOVE_REPORT);
        TenantKeyedStateTablesRdr229.fillCompletionCheck(notKeyed);   // RDR-229 P3.1 (nexus-p2m1x.17): the boot-time completion check is engine-wide
        TenantKeyedStateTablesRdr229.fillLeafIndexPass(notKeyed);   // RDR-229 P3.3b (nexus-p2m1x.20): the per-leaf id unique-index pass is engine-wide
        TenantKeyedStateTablesRdr229.fillIdUniqueIndexPass(notKeyed);   // RDR-229 P3.4 (nexus-p2m1x.22): the id-keyed index pass is engine-wide
        TenantKeyedStateTablesRdr229.fillP35c(notKeyed);   // RDR-229 P3.5c (nexus-p2m1x.25): the id-keyed readers' per-call state and two counters
        TenantKeyedStateTablesRdr229.fillRegistryRowReads(notKeyed);   // RDR-229 P3.5a (nexus-p2m1x.23): per-transaction registry-row read records
        // nexus-f3yqd.2.18: the engine's own database passwords, set once at startup; the engine's, so no tenant's
        notKeyed.put("SecretMask.SECRETS", "the engine's own database passwords (the admin and the application role), added once "
            + "at startup; process-wide credentials of the engine, never a tenant's, and nothing a tenant's reset has to clear");
        notKeyed.put("SecretMask.current", "the redactor built from the engine's own database passwords (the admin and the "
            + "application role), swapped in whole when one is added at startup so the log encoder and the failure texts can "
            + "remove them: process-wide credentials of the engine, never a tenant's, and nothing a tenant's reset has to clear");
        notKeyed.put("DataMoveWorker.loop", DATA_MOVE_HANDLE);
        notKeyed.put("DataMoveWorker.callConnection", DATA_MOVE_CALL);
        notKeyed.put("CallConnection.running", DATA_MOVE_CALL);
        notKeyed.put("VerifyingFinisher.inFlight", "the backend pid of the validation in flight (a move with no statement bound runs its "
            + "on_complete before its claim) and the engine's own admin values, kept so a stop can cancel it; no per-tenant state, "
            + "cleared when the statement ends");
        notKeyed.put("LeafCatalog.TenantLeaf.tenant", PRINCIPAL);
        notKeyed.put("MoveTenants.TENANT_ID", JOOQ_COLUMN);
        notKeyed.put("FailureHandler.Failure.tenant", PRINCIPAL);
        notKeyed.put("MoveFinisher.Context.tenants", PRINCIPAL);
        notKeyed.put("PdfStubMetadataReads.CollectionStubs.examples", RESULT);   // nexus-f3yqd: one collection's example names in that answer
        notKeyed.put("ChashConformanceConstraints.Report.constraints", RESULT);   // nexus-f3yqd: one chash-conformance call's entries (no tenant data: pg_catalog)
        notKeyed.put("ManifestForeignCollectionReads.Page.documents", RESULT);   // GH #1611: one foreign-collection call's documents
        notKeyed.put("UriSegmentCensusReads.Page.groups", RESULT);   // RDR-229 P1.2: one uri-segment-census call's groups
        notKeyed.put("ManifestForeignCollectionReads.Doc.collections", RESULT);   // GH #1611: one listed document's other collections
        // nexus-f3yqd: the census and drift answers' example lists, each built for one request and dropped with its response
        notKeyed.put("IndexRunCensusReads.Stale.examples", RESULT);
        notKeyed.put("IndexRunCensusReads.PostAnchor.examples", RESULT);
        notKeyed.put("IndexRunCensusReads.FailedHidden.examples", RESULT);
        notKeyed.put("NextSeqDriftReads.Page.examples", RESULT);
        // nexus-f3yqd (0.1.190): the engine-wide chash VALIDATE job. Its job tenant is a fixed non-tenant string, so
        // the tenant reset does not evict it; what a refused step says of a tenant (its bad-row count and example
        // chashes) is scrubbed by the ValidateConstraintsJobs evictor. The other lists are pg_catalog relation names.
        notKeyed.put("ValidateConstraintsJobs.JOB_TENANT", CONSTANT);   // the fixed (engine) owner of the one engine-wide job
        keyed.put("ValidateConstraintsJobs.Progress.refused", VALIDATE_JOBS);   // refused steps: tenant counts and example chashes
        keyed.put("ChashConstraintValidator.Step.tenantRows", VALIDATE_JOBS);   // the bad rows by tenant, held by a refused step
        keyed.put("ChashConstraintValidator.Step.exampleChashes", VALIDATE_JOBS);   // up to three chashes of a refused table (any tenant's when the admin pre-check found them: cleared on every reset)
        for (String f : List.of("ValidateConstraintsJobs.Progress.validated", "ValidateConstraintsJobs.Progress.wouldValidate",
            "ValidateConstraintsJobs.Progress.deferred")) {
            notKeyed.put(f, "a list of pg_catalog relation names and reasons of the engine-wide chash VALIDATE job (capped "
                + "at LIST_CAP), held on that job; they name no tenant and no row");
        }
        for (String f : List.of("ChashConstraintValidator.Planned.below", "ChashConstraintValidator.Precheck.examples",
            "ChashConformanceConstraints.Tree.copies")) {
            notKeyed.put(f, "a component of one validate run's plan record (constraint rows of pg_catalog, or up to "
                + "three example chashes of a refused table that the step above copies), built in that run and dropped "
                + "with it; nothing keeps it between runs and it is not a tenant's state");
        }
        notKeyed.put("DmlUtilsInstaller.status", "the latest data-move library install outcome (nexus-f3yqd.2.11): one "
            + "last-result slot for the engine's one database, written by the boot migration and read by the move worker; "
            + "it holds no tenant's data");
        notKeyed.put("ChashConstraintValidator.knownTenants", "a supplier of the known tenant names (TokenStore#"
            + "listKnownTenants), read on the failure path of a VALIDATE; it caches nothing");
        notKeyed.put("ChashConstraintValidator.TENANT_COUNT_BUDGET_PROPERTY", CONSTANT);
        notKeyed.put("ChashConstraintValidator.Config.tenantCountBudgetMs", "a configured time bound (a system property read "
            + "once per run); no tenant's data");
        notKeyed.put("ChashConstraintValidator.planRows", TEST_HOOK);
        notKeyed.put("ChashConstraintValidator.activeController", "the controller session of the run in flight (a connection "
            + "handle), kept so the shutdown can cancel its statement; no tenant's rows, cleared when the run ends");
        notKeyed.put("ChashConstraintValidator.activeListener", "the listener of the run in flight, kept so a cancel can "
            + "check that it names this run; no tenant's rows, cleared when the run ends");
        notKeyed.put("ChashConstraintValidator.activeStep", "the step session in flight (a connection handle), kept so the "
            + "shutdown can cancel its statement; it holds no tenant's rows and is cleared when the step ends");
        notKeyed.put("AdminDdlSession.resendAdminForTests", TEST_HOOK);
        notKeyed.put("AdminDdlSession.running", "the DDL statement in flight on one admin session, kept so another thread can "
            + "cancel it; a JDBC handle, cleared when the statement ends, no tenant state");
        // the per-reason account of a refused step (its counts, listed chunks with their owners, documents and the tenants in it)
        // is held in the job registry with the step, up to twelve hours: the ValidateConstraintsJobs evictor drops it on a reset
        keyed.put("ChashConstraintValidator.Detail.reasonCounts", VALIDATE_JOBS);
        keyed.put("ChashConstraintValidator.Detail.chunks", VALIDATE_JOBS);
        keyed.put("ChashConstraintValidator.Detail.documents", VALIDATE_JOBS);
        keyed.put("ChashConstraintValidator.Detail.tenants", VALIDATE_JOBS);
        keyed.put("LegacyChunkSurvey.Bad.tenant", VALIDATE_JOBS);
        keyed.put("LegacyChunkSurvey.Owners.owners", VALIDATE_JOBS);
        keyed.put("LegacyChunkSurvey.DocList.docs", VALIDATE_JOBS);
        keyed.put("LegacyChunkSurvey.DocRef.tenant", VALIDATE_JOBS);
        // built for one doctor request and returned in its response
        notKeyed.put("LegacyChunkSurvey.Counts.byReason", RESULT);
        notKeyed.put("LegacyChunkSurvey.TenantDetail.chunks", RESULT);
        notKeyed.put("LegacyChunkSurvey.TenantDetail.manifestDocs", RESULT);
        notKeyed.put("ChashConformanceHandler.detailInFlight", "the tenants whose doctor detail is being read right now, one scan at a "
            + "time per tenant; an entry lives only for the duration of that call and is removed in its finally, so no tenant's state is kept");
        notKeyed.put("NexusService.chashConstraintValidator", "the admin-connection VALIDATE object bound once at boot; "
            + "table-wide, it opens its connections per run and holds no per-tenant state");
        notKeyed.put("CatalogRepository.Rewritten.ledger", "a component of one stale-note repair batch's result, "
            + "the ledger of the rows that call changed, built for that request and returned in its response "
            + "(nexus-bivz3.24); nothing keeps it between requests");
        notKeyed.put("CatalogRepository.ImportCollectionsResult.stateCorrected", "a component of one ETL collection "
            + "import's result, the names of the new rows that call imported as quarantine although they carried live "
            + "(RDR-229 P2.5), built for that request and returned in its response; nothing keeps it between requests");
        notKeyed.put("CatalogRepository.ImportCollectionsResult.refused", "a component of one ETL collection "
            + "import's result, the labels that call refused (RDR-229 item 10), built for that request and returned in "
            + "its response; nothing keeps it between requests");
        notKeyed.put("ImportLabelRefusals.byName", "the labels one import call refused, by label, built inside that "
            + "call and dropped with it; nothing keeps it between requests");
        notKeyed.put("ChunkConstraintAdmin.TenantProbe.tenant", "a component of one table-wide probe row (the "
            + "tenant name beside its counts), built for that request and dropped with its response");
        for (String f : List.of("ChunkKeyPlan.allowed", "ChunkKeyPlan.rowKeys", "ChunkKeyPlan.bypassPrefixes",
            "ChunkKeyPlan.quarantineImplies")) {
            notKeyed.put(f, "a component of one request's immutable plan (List.copyOf), built per request from "
                + "the key table the client rendered; the same for every tenant");
        }
        notKeyed.put("ChunkKeysAdminHandler.tenants", "a supplier of the known tenant names (TokenStore#"
            + "listKnownTenants), read on each table-wide request; it caches nothing");
        notKeyed.put("NexusService.chunkConstraintAdmin", "the admin-connection DDL object bound once at boot; "
            + "table-wide, it opens a connection per call and holds no per-tenant state");
        notKeyed.put("NexusService.chunkKeysAudit", "the sink behind a root credential's retire-keys sweep audit "
            + "(CatalogRepository#recordGcAudit), bound once at boot, replaced only by a test; it is a function "
            + "that writes a row in the tenant it is given and holds no per-tenant state");
        for (String f : List.of("ChunkKeysAdminHandler.Trail.callerTenant", "ChunkKeysAdminHandler.Trail.accepted")) {
            notKeyed.put(f, "a field of the audit trail of ONE sweep request (built per request, dropped with "
                + "its response); the handler holds no reference to it afterwards");
        }
        // nexus-6pfgb.44 and .42: the catalog TTL and manifest chunk-index CHECK routes hold nothing between requests.
        for (String f : List.of("CatalogTtlCheckAdmin.TenantCensus.tenant", "CatalogTtlCheckHandler.tenants",
                "CatalogTtlCheckHandler.Survey.checked", "CatalogTtlCheckHandler.Survey.census",
                "ManifestCheckAdmin.TenantCensus.tenant", "ManifestCheckHandler.tenants",
                "ManifestCheckHandler.Survey.checked", "ManifestCheckHandler.Survey.census")) {
            notKeyed.put(f, "a component of one request's census or survey, or the supplier of known tenant names "
                + "(TokenStore#listKnownTenants) read on each request; built for that request, dropped with its response");
        }
        for (String f : List.of("NexusService.catalogTtlCheckAdmin", "ManifestCheckWiring.admins")) {
            notKeyed.put(f, "the admin-connection DDL object(s) bound once at boot (the wiring holds one per manifest "
                + "CHECK); table-wide, each opens a connection per call and holds no per-tenant state");
        }
        notKeyed.put("NexusService.tokenCache", TOKEN_CACHE_REF);
        notKeyed.put("AuthFilter.tokenCache", TOKEN_CACHE_REF);
        notKeyed.put("TokenAdminHandler.cache", TOKEN_CACHE_REF);
        notKeyed.put("TemplateRegistry.byName", "template name to schema, loaded once at boot and immutable "
            + "after; tenant-independent");
        notKeyed.put("EmbedderRouter.modelEmbedders", "embedding model to embedder, a Map.copyOf built at boot; "
            + "tenant-independent (the per-tenant profile cache is CollectionRegistry)");
        notKeyed.put("LocalTokenizers.warned", "model names already warned about, once per process; "
            + "tenant-independent");
        notKeyed.put("OrtInitGate.active", "the live ONNX init scopes of this process; tenant-independent");
        notKeyed.put("CollectionRegistry.afterMissReadForTests", "a test-only hook run between a cache "
            + "miss's read and its fill (nexus-hkh1y); null in production, holds no state");
        notKeyed.put("ModelPartitions.KNOWN", "parent-and-model partition pairs already created; keyed by "
            + "model, not by tenant");
        notKeyed.put("TenantScope.ADMISSION", "admission semaphore per DataSource identity; tenant-independent");
        notKeyed.put("TenantScope.FANOUT_ARMS", "fan-out arm semaphore per DataSource identity; "
            + "tenant-independent");
        notKeyed.put("HttpUtil.BEFORE_SEND", "send hook per in-flight HttpExchange, removed when the response "
            + "is sent; request-scoped");
        FanoutSearchKeyedStateTable.fill(notKeyed);   // nexus-xd2rc, nexus-bbtc2: one fan-out search's state
        notKeyed.put("TupleWaitRegistry.MultiWaiter.pendingSubspaces", "wake bookkeeping of one parked wait; "
            + "dies when that request ends");
        notKeyed.put("TupleWaitRegistry.MultiWaiter.tenant", "the tenant of one parked wait, immutable; "
            + "dies when that request ends");
        notKeyed.put("TenantCreationBusyException.tenant", "payload of an exception thrown to one caller");
        notKeyed.put("UnregisteredCollectionException.tenant", "payload of an exception thrown to one caller");
        notKeyed.put("CollectionSupersededException.tenant", "payload of an exception thrown to one caller (nexus-mp8ys)");
        notKeyed.put("UnknownCollectionIdException.tenant", "payload of an exception thrown to one caller (RDR-229 P3.5a)");
        notKeyed.put("CollectionSelectorFilter.Resolver.tenant", "the tenant of one request: the resolver is built per request and dies with it (RDR-229 P3.5a)");
        notKeyed.put("CollectionSelectorFilter.Resolver.expected", "the ids one request named, by label: the resolver is built per request and dies with it (RDR-229 P3.5a)");
        notKeyed.put("CollectionSelectorFilter.afterResolve", TEST_HOOK);
        notKeyed.put("CollectionSelectorFilter.beforeTransactionRead", TEST_HOOK);
        notKeyed.put("CollectionSelectorFilter.afterTransactionRead", TEST_HOOK);
        notKeyed.put("CollectionSelectorFilter.unconsumedSink", TEST_HOOK);
        notKeyed.put("ExpectedCollectionIds.Carrier.ids", "the ids the request on this thread named, by label: set by the route boundary just before the handler and removed in a finally, so no request outlives it and nothing is keyed by tenant (RDR-229 P3.5a)");
        notKeyed.put("ExpectedCollectionIds.Carrier.consumed", "the labels the request on this thread has read by id in its own transaction; dies with the request's carrier (RDR-229 P3.5a)");
        notKeyed.put("ExpectedCollectionIds.Carrier.readByTransaction", "census only (a test turns it on): the labels each transaction of the request read, by the transaction's jOOQ configuration; dies with the request's carrier (RDR-229 P3.5a)");
        notKeyed.put("ExpectedCollectionIds.Carrier.historyTransactions", "census only: the transactions of the request that declared a history write; dies with the request's carrier (RDR-229 P3.5a)");
        notKeyed.put("StrictWriterCensus.FOUND", "census only (off unless -Dnexus.writer.census is set): the guarded-table writes that ran in a transaction with no registry read, as 'table @ frame' strings; a test reads it and nothing is keyed by tenant (RDR-229 P3.5a)");
        notKeyed.put("ExpectedCollectionIds.Carrier.findings", "census only: the write transactions of the request that read none of its named ids; dies with the request's carrier (RDR-229 P3.5a)");
        notKeyed.put("CollectionRegistry.writeReadHookForTests", TEST_HOOK);
        notKeyed.put("DeadlockRetry.retryListenerForTests", TEST_HOOK);
        notKeyed.put("CollectionSelectorFilter.censusSink", TEST_HOOK);
        notKeyed.put("CollectionRetiredIdException.tenant", "payload of an exception thrown to one caller (RDR-229 P3.5a)");
        notKeyed.put("ReembedRunner.tenantSource", "a supplier of the tenant list, re-read from service_tokens at "
            + "the start of every runner pass; the runner keeps nothing per tenant between passes");
        notKeyed.put("ReembedHandler.servedModels", "a supplier of the models the embedder router serves, an "
            + "install-wide fact read on each plan and start (the list GET /version reports); it keeps no copy");
        for (String f : List.of(
                "Reference.columns", "Reference.parentColumns", "ExportTable.primaryKey", "ExportTable.references",
                "ExportTable.triggers", "ExportTable.loadsAfter", "ExportTable.triggerMaintained",
                "ExportTable.notReferences", "Entry.pk", "Entry.refs", "Entry.triggers", "Entry.loadsAfter",
                "Entry.maintained", "Entry.notRefs", "BY_NAME")) {
            notKeyed.put("TenantExport." + f, EXPORT_REGISTRY);
        }
        TenantExportKeyedStateTable.fill(keyed, notKeyed, RESULT);   // nexus-csayy.2 (RDR-248)
        notKeyed.put("ChunkReaper.tenants", "a supplier of the tenant list, re-read from service_tokens on "
            + "every pass; the reaper keeps no copy");
        notKeyed.put("Rdr192BackfillGate.emptyTenant", "a predicate that reads PG on each call; it caches nothing");
        notKeyed.put("NexusService.aspectQueueDrainResult", "the one-shot result of the engine-start drain of the retired "
            + "aspect_extraction_queue table (nexus-hbnml): install-wide totals and a reason tally, no per-tenant entry");
        notKeyed.put("NexusService.aspectQueueDrain", "the drain object bound once at boot; it keeps its state in a "
            + "per-run tally that dies with the run");
        notKeyed.put("StatusHandler.aspectQueue", "a supplier of the install-wide drain result; not keyed by tenant");
        notKeyed.put("StatusHandler.catalogEpoch", "a supplier of whether the install's role can read the cluster facts the catalog epoch hashes (RDR-229 P3.11); one install-wide boolean, not keyed by tenant");
        notKeyed.put("CompletionBootWork.DbReader.latchWriteFailingSince", "when the gate 3 latch write first failed to take its lock in the current run of failures (RDR-229 P3.5a), for the elapsed figure in the WARN; one install-wide instant, not keyed by tenant");
        notKeyed.put("RenameNoOpActivity.NOOPS", "a process-wide count of per-store renames answered as the one-row no-op (RDR-229 P3.5a), read only by tests; one counter, not keyed by tenant");
        notKeyed.put("CatalogEpoch.NULL_LOGGED", "a once-per-process latch for the WARN that a pg_control member read NULL (RDR-229 P3.11); one install-wide flag, not keyed by tenant");
        notKeyed.put("StatusHandler.collectionTupleIndex", "a supplier of whether the install-wide live-tuple index exists (RDR-229 P3.8); not keyed by tenant");
        notKeyed.put("StatusHandler.liveLabelIndex", "a supplier of whether the install-wide live-label index exists and is valid (RDR-229 P3.12); not keyed by tenant");
        notKeyed.put("AspectQueueDrain.TENANT", "the tenant_id column of the retired queue table, a jOOQ field "
            + "constant; no state");
        notKeyed.put("AspectQueueDrain.Snapshot.rowsByTenant", "one census read of the retired queue table, "
            + "returned to the run that asked and dropped with it; the drain keeps no copy");
        notKeyed.put("AspectQueueDrain.Result.reasons", "reason to row count across the whole run; no tenant key");
        notKeyed.put("AspectQueueDrain.fallbackTenants", "a supplier of the engine's known tenants (the reaper's), "
            + "re-read when the census cannot run; the drain keeps no copy");
        notKeyed.put("AspectQueueDrain.Tally.perTenant", "per-tenant counts for the log lines of ONE run, built "
            + "and dropped inside run(); the drain runs once per boot and keeps nothing between runs");
        notKeyed.put("AspectQueueDrain.Tally.reasons", "reason to row count for ONE run; no tenant key");
        notKeyed.put("StatusHandler.chunksTenantIsolationIntact", "a supplier of one process-wide policy check; "
            + "not keyed by tenant");
        notKeyed.put("TokenStore.tenantCreationBound", "attempt and timeout bounds for a token insert; "
            + "configuration, immutable");
        notKeyed.put("SchemaMigrator.CHASH_LEN_CONSTRAINTS", "constraint names the schema migrator repairs; "
            + "filled once, tenant-independent");
        notKeyed.put("NexusService.tenantKeyedState", "the evictor list itself: an immutable List.copyOf built "
            + "once at construction from stateless lambdas");
        notKeyed.put("NexusService.TENANT_RESET_ROUTE_ENV", CONSTANT);
        notKeyed.put("NexusService.TENANT_RESET_TUPLE_BOUND", CONSTANT);
        // nexus-4lnn1.1.3 (RDR-230): the recall canary's query ring. Its rows are in nexus.canary_query_ring (FORCE RLS,
        // cascading foreign key to the collection registry); what the engine holds in memory is a short queue.
        notKeyed.put("PciQueryRing.queue", "a bounded in-memory queue (1,024 entries) drained every 30 seconds; an "
            + "entry for a reset tenant is skipped by the function's foreign key, so nothing outlives the reset");
        notKeyed.put("PciQueryRing.Entry.tenant", "the tenant of one queued ring entry, immutable; dies when the "
            + "queue is drained at the next flush");
        notKeyed.put("PciQueryRing.validIndexes", "the router's valid-index set, shared with the repository; "
            + "owned and evicted there");
        notKeyed.put("PciQueryRing.dropped", "a single process-wide count of entries the full queue dropped, "
            + "reset at each flush; not keyed by tenant");
        notKeyed.put("PciQueryRing.seen", "a single process-wide count of admit calls, for tests; "
            + "not keyed by tenant");
        notKeyed.put("PciQueryRing.writer", "the writer thread handle, set once at start; tenant-independent");
        notKeyed.put("PciQueryRingExpiry.tenants", "a supplier of the tenant list (the reaper's tenants and "
            + "request_ledger_tenants), re-read on every pass; the expiry keeps no copy");
        notKeyed.put("VectorHandler.queryRing", "the handle of the one query ring, bound once at boot; "
            + "the ring keeps no per-tenant state of its own beyond its short queue");
        // CONFIG (16)
        for (String f : List.of(
            "Bge768Embedder.admissionGate",
            "ChunkReaper.Settings.floorExemptCollections",
            "HttpUtil.gzipper",
            "NexusService.chunksLeafBloat",
            "NexusService.chunksLeavesCanary",
            "NexusService.chunksLeavesSweepSeconds",
            "NexusService.chunksLeavesView",
            "NexusService.embedPosture",
            "NexusService.perCollectionIndexes",
            "OwnerlessWriteMode.envReader",
            "OwnerlessWritePolicy.mode",
            "PgSession.scanBudget",
            "StatusHandler.embedPosture",
            "StatusHandler.testTenantResetRoute",
            "VersionHandler.embedPosture",
            "VersionHandler.schemaIdentity"
        )) {
            notKeyed.put(f, CONFIG);
        }
        // COUNTER (47)
        for (String f : List.of(
            "Bge768Embedder.onnxInvocationCount",
            "CatalogRepository.vacuumInProgress",
            "CceEmbedder.callEwmaNanos",
            "CceEmbedder.waitingBatches",
            "NexusService.consecutiveDataSweepFailures",
            "ChunkKeyAllowlistActivity.WOULD_REFUSE",
            "ChunkReaper.censusBackoffTotal",
            "ChunkReaper.censusTimedOutTotal",
            "ChunkReaper.failedPassesTotal",
            "ChunkReaper.gateBusyTotal",
            "ChunkReaper.lockTimeoutTotal",
            "ChunkReaper.passNumber",
            "ChunkReaper.refusedTotal",
            "ChunkReaper.statementBackoffTotal",
            "ChunkReaper.statementTimedOutTotal",
            "DeadlockRetry.RETRY_ATTEMPTS",
            "EmbedActivityTracker.admissionRefusalsTotal",
            "EmbedActivityTracker.chunksDoneTotal",
            "EmbedActivityTracker.deadlineAbortsTotal",
            "EmbedActivityTracker.lastActivityNanos",
            "EmbedActivityTracker.lastChunksPerSecBits",
            "EmbedActivityTracker.subBatchesTotal",
            "EmbedProgressGate.lastLogNanos",
            "HttpUtil.GZIP_FAILURE_LOGGED",
            "NexusService.SweepCounts.tenants",
            "NexusService.TupleSweepRunResult.tenantsVisited",
            "OwnerlessWriteActivity.REFUSED",
            "OwnerlessWriteActivity.WOULD_REFUSE",
            "PgVectorRepository.EXACT_FALLBACKS",
            "PgVectorRepository.QUALIFYING_WIDENS",
            "PgVectorRepository.ROUTED_EXACT",
            "PgVectorRepository.ROUTED_HNSW",
            "PgVectorRepository.SHORT_PAGE_COUNT_DEADLINES",
            "PgVectorRepository.SHORT_PAGE_RERUNS",
            "PgVectorRepository.SHORT_PAGE_RERUN_DEADLINES",
            "PgVectorRepository.SHORT_PAGE_SKIPS",
            "PgVectorRepository.existenceSelectCalls",
            "PgVectorRepository.ownerLookupCalls",
            "RacedEmbedActivity.RACED_EMBEDS_TOTAL",
            "RequestLedgerActivity.IN_FLIGHT",
            "RequestLedgerActivity.KEY_REUSED",
            "RequestLedgerActivity.REPLAYS",
            "RequestLedgerActivity.TAKEOVERS",
            "ShortPageCountBudget.SPENT",
            "SuppliedVectorMismatchActivity.MISMATCHES_TOTAL",
            "TupleWaitRegistry.claimantRefused",
            "TupleWaitRegistry.globalParked",
            "TupleWaitRegistry.globalRefused",
            "VoyageEmbedder.voyageRequestCount"
        )) {
            notKeyed.put(f, COUNTER);
        }
        // COUNT_VIEW (1): the bounded by-route-and-class view of the allow-list's would-refuse count
        for (String f : List.of(
            "ChunkKeyAllowlistActivity.BY_ROUTE_CLASS"
        )) {
            notKeyed.put(f, COUNT_VIEW);
        }
        // TEST_HOOK (23)
        for (String f : List.of(
            "CatalogRepository.afterChunkUpsertHookForTests",
            "CatalogRepository.afterMoveCompletedHookForTests",
            "CatalogRepository.afterMovePreconditionHookForTests",
            "CatalogRepository.beforeReadHookForTests",
            "CatalogRepository.noteUriRepairHookForTests",
            "CatalogRepository.noteUriCensusedHookForTests",
            "ReembedJobRepository.lockRetryHookForTests",
            "ReembedRunner.phaseHookForTests",
            "NexusService.reembedServedModelsForTests",
            "ChunkReaper.testOnlyAfterNoteTimeout",
            "CombinedWriteService.afterExistencePartitionHookForTests",
            "CombinedWriteService.afterNeedEmbedResolvedHookForTests",
            "HttpUtil.sendObserverForTests",
            "PgVectorRepository.afterExistencePartitionHookForTests",
            "PgVectorRepository.afterNeedEmbedResolvedHookForTests",
            "PgVectorRepository.afterSweepSqlHookForTests",
            "PgVectorRepository.beforeSensitiveLookupHookForTests",
            "PgVectorRepository.keyedRestoreSiblingHookForTests",
            "RequestLedger.claimPhaseHookForTests",
            "TaxonomyRepository.betweenOwnAndCrossPassHookForTests",
            "TaxonomyRepository.duringOwnPassHookForTests",
            "TokenCache.testOnlyAfterStoreRead",
            "TupleRepository.TEST_ONLY_ANNOUNCE_TXN_HOLD",
            "TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY",
            "TupleRepository.TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY",
            "TupleRepository.TEST_ONLY_SUBSPACE_LIST_PRE_QUERY_HOOK",
            "TupleRepository.testOnlyAfterInterrupt",
            "TupleRepository.testOnlyAfterRegister",
            "TupleRepository.testOnlyAtParkLoopTop",
            "TupleWaitRegistry.TEST_ONLY_SIGNAL_HOOK"
        )) {
            notKeyed.put(f, TEST_HOOK);
        }
        // RESULT (75)
        for (String f : List.of(
            "CatalogRepository.DocumentMove.expectedChashes",
            "ChunksLeafAutovacuumCheck.LeafRel.options",
            "LeafBloatCatalog.Leaf.pciIndexes",
            "LeafBloatCatalog.Snapshot.leaves",
            "PgVectorRepository.SiblingResolution.names",
            "LeafBloatSweep.Status.leaves",
            "CatalogRepository.AppendOutcome.sweep",
            "CatalogRepository.DanglingEndpointException.missing",
            "CatalogRepository.DeleteOutcome.counts",
            "CatalogRepository.DeleteOutcome.retiredRemoved",
            "CatalogRepository.DeleteOutcome.unregisteredSiblings",
            "CatalogRepository.GhostSweepResult.dormantNames",
            "CatalogRepository.GhostSweepResult.ghostNames",
            "CatalogRepository.RacedEmbeds.sample",
            "CatalogRepository.RehomeResult.leftBehindByTable",
            "CatalogRepository.RehomeResult.movedByTable",
            "CatalogRepository.RehomeStatus.remainingByTable",
            "CatalogRepository.ResolvedChunk.mergeDeleteKeys",
            "CceEmbedder.BatchOutcome.vectors",
            "ChunksIsolationCheck.Probe.found",
            "ChunksIsolationCheck.Probe.gaps",
            "ChunksIsolationCheck.StructureReport.gaps",
            "ChunksLeafAutovacuumCheck.Report.autovacuumDisabled",
            "ChunksLeafAutovacuumCheck.Report.policyUnset",
            "CombinedWriteService.ChunkSplit.relevant",
            "CombinedWriteService.CombinedWriteResult.response",
            "CombinedWriteService.MetadataMode.deleteKeys",
            "CombinedWriteService.ResolvedBatch.mismatchedChashes",
            "CombinedWriteService.ResolvedBatch.resolved",
            "EmbedResult.embeddings",
            "LocalModelProbe.FileState.missing",
            "LocalModelProbe.Presence.bgeMissing",
            "LocalModelProbe.Presence.crossEncoderMissing",
            "MemoryRepository.ExpireResult.deletedIds",
            "MemoryRepository.ExpireResult.quarantinedIds",
            "MemoryRepository.ResolveResult.candidates",
            "OwnerlessChunkWriteException.unownedSample",
            "PgSession.GucBatch.calls",
            "PgVectorRepository.Candidate.row",
            "ManifestLabelStage.Split.rest",
            "ManifestLabelStage.Split.labels",
            "ManifestLabelStage.Occurrence.docValues",
            "ManifestLabelStage.DocFields.keys",
            "PgVectorRepository.ExistencePartition.haveVectorIdx",
            "PgVectorRepository.ExistencePartition.needEmbedIdx",
            "PgVectorRepository.ManifestLessCensusResult.chashes",
            "PgVectorRepository.ManifestLessCensusResult.owners",
            "PgVectorRepository.ManifestLessCensusResult.totals",
            "PgVectorRepository.MetadataUpdateOutcome.missing",
            "PgVectorRepository.NeedEmbedResolution.needEmbedIdx",
            "PgVectorRepository.NeedEmbedResolution.originalAbsentIdx",
            "PgVectorRepository.LexicalStat.ids",
            "PgVectorRepository.PerCollectionResult.lexical",
            "PgVectorRepository.PerCollectionResult.lexicalRows",
            "PgVectorRepository.PerCollectionResult.perCollection",
            "PgVectorRepository.PerCollectionResult.rows",
            "PgVectorRepository.PerCollectionResult.skippedCollections",
            "PgVectorRepository.QuarantineBoundedOutcome.sample",
            "PgVectorRepository.QuarantineFlooredOutcome.sample",
            "PgVectorRepository.QuarantineOutcome.sample",
            "PgVectorRepository.QuarantineRestoreBusyException.auditIds",
            "PgVectorRepository.QuarantineRestoreBusyException.movedChashes",
            "PgVectorRepository.QuarantineRestoreOutcome.auditIds",
            "PgVectorRepository.QuarantineRestoreOutcome.quarantineCollections",
            "PgVectorRepository.QuarantineRestoreOutcome.rows",
            "PgVectorRepository.Tokened.skippedCollections",
            "ReembedJobRepository.BatchResult.skips",
            "SchemaMigrator.WalkChangesetCounts.duplicateChangesetIds",
            "TaxonomyRepository.OwnPassOutcome.unmatched",
            "TelemetryRepository.StepInput.bundledSteps",
            "TokenStore.MixedScopeRotationRefused.scopes",
            "TokenStore.RotationResult.expiredHashes",
            "TupleRepository.ParkStats.perClaimant",
            "TupleRepository.PreparedOut.dims",
            "TupleRepository.PreparedOut.keys",
            "TupleRepository.ReplySpec.dims",
            "TupleRepository.ReplySpec.keys",
            "TupleRepository.SubspacePage.items",
            "TupleRepository.TupleRow.dims",
            "TupleRepository.TupleRow.keys",
            "TupleRepository.WaitResult.tuples",
            "TupleRepository.WaitSpec.pattern",
            "VoyageEmbedder.SubBatchResponse.texts"
        )) {
            notKeyed.put(f, RESULT);
        }
        // REAPER_COUNTS (10)
        for (String f : List.of(
            "ChunkReaper.LastPass.tenantsEmpty",
            "ChunkReaper.LastPass.tenantsErrored",
            "ChunkReaper.LastPass.tenantsRefused",
            "ChunkReaper.LastPass.tenantsVisited",
            "ChunkReaper.lastCompletedPassAt",
            "ChunkReaper.lastPass",
            "StatusHandler.ReaperStatus.LastPass.tenantsEmpty",
            "StatusHandler.ReaperStatus.LastPass.tenantsErrored",
            "StatusHandler.ReaperStatus.LastPass.tenantsRefused",
            "StatusHandler.ReaperStatus.LastPass.tenantsVisited"
        )) {
            notKeyed.put(f, REAPER_COUNTS);
        }
        // REAPER_LAST_RUN (7)
        for (String f : List.of(
            "ChunkReaper.RunResult.tenants",
            "ChunkReaper.TenantResult.clientExpiries",
            "ChunkReaper.TenantResult.collections",
            "ChunkReaper.TenantResult.expiries",
            "ChunkReaper.TenantResult.tenant",
            "ChunkReaper.TenantResult.tenantRefusal",
            "ChunkReaper.lastRun"
        )) {
            notKeyed.put(f, REAPER_LAST_RUN);
        }
        // JOOQ_COLUMN (2)
        for (String f : List.of(
            "DimTables.CentroidTable.tenantId",
            "DimTables.ChunkTable.tenantId"
        )) {
            notKeyed.put(f, JOOQ_COLUMN);
        }
        // PRINCIPAL (9)
        for (String f : List.of(
            "NexusService.TupleTenantCursor.tenantId",
            "IdentityTokenStore.Principal.tenantId",
            "ReembedRunner.Turn.tenant",
            "RequestContext.Principal.tenant",
            "RequestLedger.RetentionPass.tenant",
            "SessionPrincipal.tenantId",
            "TokenCache.Resolved.tenantId",
            "TokenStore.ServiceToken.tenantId",
            "TokenStore.TokenInfo.tenantId"
        )) {
            notKeyed.put(f, PRINCIPAL);
        }
        // ORT (5)
        for (String f : List.of(
            "OrtInitGate.boundExpiredCleanup",
            "OrtInitGate.exitSignals",
            "OrtInitGate.halting",
            "OrtInitGate.installed",
            "OrtNativeDir.UID_WARNED"
        )) {
            notKeyed.put(f, ORT);
        }
        // LEAF_BLOAT (2)
        for (String f : List.of(
            "LeafBloatSweep.current",
            "LeafBloatSweep.loggedProblems"
        )) {
            notKeyed.put(f, LEAF_BLOAT);
        }
        // PCI_OFF (17)
        for (String f : List.of(
            "PciBuilderSession.state",
            "PciCatalog.Key.tenant",
            "PciCatalog.Leaf.indexes",
            "PciCatalog.Leaf.tenant",
            "PciCatalog.LeafBuilder.indexes",
            "PciCatalog.Snapshot.leaves",
            "PciIndexSweep.State.entries",
            "PciIndexSweep.State.expiryLogged",
            "PciIndexSweep.State.set",
            "PciIndexSweep.State.since",
            "PciIndexSweep.State.idSince",
            "PciIndexSweep.state",
            "PciPlanCheck.armsSeen",
            "PciReconciler.Planned.actions",
            "PciReconciler.building",
            "PciReconciler.lastDdlPassAt",
            "PciReconciler.passStartedAt",
            "PciReconciler.passes"
        )) {
            notKeyed.put(f, PCI_OFF);
        }
        // REQUEST (1)
        for (String f : List.of(
            "PgVectorRepository.ArmRun.limiter"
        )) {
            notKeyed.put(f, REQUEST);
        }
        // TEMPLATE (19)
        for (String f : List.of(
            "TemplateDoc.Example.dims",
            "TemplateDoc.Example.keys",
            "TemplateDoc.fields",
            "TemplateDoc.readers",
            "TemplateDoc.seeAlso",
            "TemplateDoc.writers",
            "TemplateRegistry.Snapshot.sources",
            "TemplateRegistry.Snapshot.templates",
            "TemplateRegistry.SourceGroup.files",
            "TemplateRegistry.sources",
            "TemplateRegistry.templates",
            "TemplateSchema.Dimension.values",
            "TemplateSchema.dimensions",
            "TemplateSchema.idDims",
            "TemplateSchema.keyValues",
            "TemplateSchema.keys",
            "TemplateSchema.nameSegments",
            "TemplateSchema.retentionBySuffix",
            "TemplateSchemaParser.ParsedKeys.names",
            "TemplateSchemaParser.ParsedKeys.values"
        )) {
            notKeyed.put(f, TEMPLATE);
        }
        // EVICTOR_LIST (1)
        for (String f : List.of(
            "TenantResetHandler.evictors"
        )) {
            notKeyed.put(f, EVICTOR_LIST);
        }
        // TOKEN_EPOCH (1)
        for (String f : List.of(
            "TokenCache.tenantInvalidations"
        )) {
            notKeyed.put(f, TOKEN_EPOCH);
        }
        // MULTI_GROUPS (1)
        for (String f : List.of(
            "TupleWaitRegistry.MultiWaiter.registeredGroups"
        )) {
            notKeyed.put(f, MULTI_GROUPS);
        }
        // WAITER_LOCAL (1)
        for (String f : List.of(
            "TupleWaitRegistry.MultiWaiter.subspaces"
        )) {
            notKeyed.put(f, WAITER_LOCAL);
        }
        notKeyed.put("PciReconciler.failures", PCI_OFF);
        // nexus-p2m1x.22 (RDR-229 P3.4): the id-predicate half's memory. The planned keys are replaced whole by each pass that saw
        // every leaf (and dropped to unknown by one that did not); the gate and the per-pass plan values are not kept.
        notKeyed.put("PciIdBuilds.readers", "whether this engine's id readers are present (IdReaders.PRESENT, which P3.5c flips) as a supplier, bound once at boot; a callback, no data");
        notKeyed.put("PciIdBuilds.pciOffLatch", "whether the gate 3 latch row says it latched while PCI was off, as a supplier bound once at boot; a callback, no data");
        // RDR-229 P3.5a: W2's route classification and gc seam, W1's client-label carrier.
        notKeyed.put("CollectionSelectors.EXEMPT", "the route table's exempt routes and the reason for each, a constant built once; no tenant");
        notKeyed.put("GcCollectionReads.beforeMoveForTests", "a test seam run before a gc move, null in production; a callback, no data");
        notKeyed.put("ExpectedCollectionIds.Carrier.clientLabels", "the labels one request itself sent beside its ids, held for that "
            + "request's thread only and cleared at its end; no tenant state");
        notKeyed.put("CompletionBootWork.DbReader.coverage", TenantKeyedStateTablesRdr229.COMPLETION_ENGINE_WIDE + "the last PCI coverage read, reused for at most ten minutes once gate 3 has latched "
            + "(it holds id keys: model, tenant and collection id), in memory only and replaced by the next read");
        notKeyed.put("CompletionBootWork.DbReader.coverageAt", "when that coverage was read (a time); no tenant state");
        notKeyed.put("PciIdBuilds.gate3", "the completion check's gate 3 as a supplier, bound once at boot; a callback, no data");
        notKeyed.put("PciGate3Coverage.unindexed", PCI_OFF + "; the last near-latch scan's keys (model, tenant, collection id) of collections at the build threshold with no index, replaced whole by each scan and dropped after ten minutes");
        notKeyed.put("PciGate3Coverage.Found.keys", PCI_OFF);
        notKeyed.put("PciIdBuilds.gate2", "the completion check's gate 2 as a supplier, bound once at boot; a callback, no data");
        notKeyed.put("PciReconcilePlanner.Plan.actions", RESULT);
        notKeyed.put("PciCatalog.Snapshot.validIds", PCI_OFF);
        notKeyed.put("PciCatalog.Snapshot.invalidIds", PCI_OFF);
        notKeyed.put("CompletionBootWork.DbReader.gate3Extras", "the further gate 3 conditions, fixed at construction "
            + "(IdUniqueIndexPass.allValid, wired in Main); callbacks, no tenant state");
        // nexus-4lnn1.1.1 (RDR-230): the DDL half's last look at each index (its anchor, size and counts) and at
        // each leaf's statistics, kept per engine in memory and lost on restart like the failures map above. The
        // The Reads fields are one counting transaction's result, applied once it commits. They live in
        // PciAnchorLedger since nexus-4lnn1.1.10 (moved out of PciReconciler to keep it under its size cap).
        notKeyed.put("PciAnchorLedger.lastStats", PCI_OFF);
        notKeyed.put("PciAnchorLedger.readings", PCI_OFF);
        notKeyed.put("PciAnchorLedger.Reads.readings", PCI_OFF);
        notKeyed.put("PciAnchorLedger.Reads.writes", PCI_OFF);
        notKeyed.put("PciReconciler.tenantBinder", PCI_OFF);
        // nexus-4lnn1.1.10 (RDR-230 Phase 2): the REINDEX phase's in-memory fields; the list and why.
        PciReindexNotKeyedFields.FIELDS.forEach(f -> notKeyed.put(f, PCI_OFF));
        // nexus-f3yqd.3: GET /v1/status's boot_work. The registry lists the heavy work this engine is doing
        // (an index build, by id), names no tenant, and a process-wide piece of work is not carried across a reset.
        notKeyed.put("BootWorkRegistry.entries", "the engine's heavy work in flight, by work id (pci-reconcile); "
            + "names no tenant and is emptied by each piece of work when it ends, so a reset has nothing to clear");
        notKeyed.put("NexusService.bootWork", "the handle of the one boot-work registry, bound once at boot; "
            + "the registry keeps no per-tenant state");
        notKeyed.put("StatusHandler.bootWork", "a supplier of the registry's snapshot, bound once at boot; "
            + "not keyed by tenant");
        notKeyed.put("PciReconciler.bootWork", "the reconciler's boot-work reporter, bound once at boot; its "
            + "counters are this pass's builds planned and finished, reset when the pass ends");
        notKeyed.put("EngineSingletonLock.connection", SINGLETON_LOCK);
        notKeyed.put("EngineSingletonLock.onRegained", SINGLETON_LOCK);
        notKeyed.put("EngineSingletonLock.onFatal", SINGLETON_LOCK);
        notKeyed.put("EngineSingletonLock.staleTerminationWait", SINGLETON_LOCK);
        notKeyed.put("PciPlanCheck.lastSampled", PCI_OFF);
        notKeyed.put("PciCatalog.Snapshot.valid", PCI_OFF);
        notKeyed.put("PciCatalog.Snapshot.anchors", PCI_OFF);
        notKeyed.put("PciCatalog.Snapshot.idAnchors", PCI_OFF);   // the same, for the id-predicate indexes, keyed by (model, tenant, id)
        notKeyed.put("PciCatalog.LeafBuilder.tenant", PCI_OFF);
        // nexus-4lnn1.1.4 (RDR-230): the recall canary's schedule and results, per engine in memory, lost on restart;
        // keyed by index name (which carries the tenant's hash) and written only by the builder-lock holder's pass.
        for (String f : List.of(
            "PciCanarySchedule.IndexState.lastRun",
            "PciCanarySchedule.IndexState.lastVacuumTrigger",
            "PciCanarySchedule.IndexState.retryNotBefore",
            "PciCanarySchedule.IndexState.latest",
            "PciCanarySchedule.states",
            "PciCanarySchedule.baselines",
            "PciCanary.Target.tenant",
            "PciCanary.Leg.hits",
            "PciCanary.Result.redSample",
            "PciCanary.Result.redQueries",
            "PciReconciler.Tally.droppedIndexes",
            "PciReconciler.Tally.builtTargets",
            // the id canary's ring keys: one pass's (model, tenant, id) to label, from the leaf counting transactions, dropped with the pass
            "PciReconciler.Tally.idLabels",
            "PciReconciler.Planned.idLabels",
            // the latch the id canary's statements read: a supplier over the reconciler's own gate 3 latch, no tenant state
            "PciCanaryLatch.source",
            "PciReconciler.canaryLatch",
            "PciQueryRing.gate3",
            "PciReindexPhase.idCanary")) {
            notKeyed.put(f, PCI_OFF);
        }
        // nexus-4lnn1.1.5 (RDR-230 Technical Design 7): the maintenance snapshot (counts on /v1/status, entries on
        // the operator-only /v1/admin/chunks-leaves). A per-read list of at
        // most 64 entries keyed by the opaque pci_ index name, built from the reconciler's memory for one response
        // and dropped with it; no field of an entry names a tenant or a collection.
        notKeyed.put("PciMaintenanceStatus.Canary.indexes", PCI_OFF);
        // nexus-4lnn1.1.7: the reconciler's hook for the ring's 30-day delete. A stateless function handed the pass's
        // leaf tenants for one call; the reconciler keeps no copy of them.
        notKeyed.put("PciReconciler.ringExpiry", PCI_OFF);
        notKeyed.put("PciIndexSet.NONE", PCI_OFF);
        notKeyed.put("PgVectorRepository.pciIndexes", PCI_OFF);
        // RDR-252 Step 3 (nexus-igea9): the owner stamper. Everything below is a value read or built for one evaluation
        // (a record's components, a holder of a function or of the timer); none outlives the call that built it
        // except the timer's armed and failures maps, which are keyed above and cleared by the AckDeadlineTimer evictor.
        String evaluation = "a component of a value record built for one owner evaluation and dropped with it; "
            + "it is not stored";
        for (String f : List.of(
            "GroupView.members", "GroupView.deadlineByTopic", "OwnerDecision.Input.pushes", "OwnerDecision.Input.dones",
            "OwnerDecision.Input.owners", "OwnerDecision.Post.body", "BoardRow.dims", "Identities.identityOfStamp",
            "Identities.presence", "OwnerDecision.Ctx.chain", "OwnerDecision.Ctx.redTips", "OwnerDecision.Ctx.redAt")) {
            notKeyed.put(f, evaluation);
        }
        notKeyed.put("OwnerStamper.timer", "the stamper's reference to the one AckDeadlineTimer, bound once at boot; "
            + "the per-tenant state is the timer's armed map, which has its own evictor");
        notKeyed.put("AckDeadlineTimer.Key.tenant", "the tenant component of the key of the armed map, a record "
            + "that exists only as that map's key; the map itself is listed as keyed");
        notKeyed.put("BoardOwnerTenants.tokenTenants", "a supplier of the token tenants, read once per rebuild attempt; "
            + "it holds no tenant's state");
        notKeyed.put("BoardOwnerTenants.tupleTenants", "a supplier of the tuple_tenants rows, read once per rebuild "
            + "attempt; it holds no tenant's state");
        notKeyed.put("BoardOwnerWiring.Pending.tenant", "a component of a census item (a tenant and a topic whose re-arm "
            + "failed), held in the list one scheduled rebuild attempt hands to the next; the list ends with the census "
            + "and keeps no tenant's state: a re-arm of an evicted tenant finds no rows and succeeds");
    }
}
