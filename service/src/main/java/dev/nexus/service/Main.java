package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Bge768Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.LocalTokenizers;
import dev.nexus.service.vectors.OrtInitGate;
import dev.nexus.service.vectors.OrtNativeDir;
import dev.nexus.service.vectors.OrtTempSweep;
import dev.nexus.service.vectors.PgVectorRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Entry point for the nexus-service process.
 *
 * <p>Configuration (v1 bootstrap — all from environment):
 * <ul>
 *   <li>{@code NX_SERVICE_PORT} — listen port (default 8080)</li>
 *   <li>{@code NX_SERVICE_TOKEN} — bearer token for authentication</li>
 *   <li>{@code NX_DB_URL} — JDBC URL (e.g. {@code jdbc:postgresql://localhost/nexus})</li>
 *   <li>{@code NX_DB_USER} — database user (application / DML role)</li>
 *   <li>{@code NX_DB_PASS} — database password</li>
 *   <li>{@code NX_POOL_SIZE} — HikariCP pool size (default 10)</li>
 *   <li>{@code NX_DB_ADMIN_URL} — optional JDBC URL for schema migration (defaults to
 *       {@code NX_DB_URL}). Useful when the application role is {@code nexus_svc}
 *       (NOSUPERUSER NOBYPASSRLS) and a separate schema-owner role runs DDL.</li>
 *   <li>{@code NX_DB_ADMIN_USER} — optional migration user (defaults to {@code NX_DB_USER})</li>
 *   <li>{@code NX_DB_ADMIN_PASS} — optional migration password (defaults to {@code NX_DB_PASS})</li>
 * </ul>
 *
 * <p>Vector backend (RDR-155 P4a.2, bead nexus-1k8s1 — pgvector serves every vector
 * route; the Chroma serving backend and its {@code NX_CHROMA_*} wiring are retired):
 * <ul>
 *   <li>{@code NX_VOYAGE_API_KEY} — optional Voyage AI API key. Present: embedders
 *       route by collection prefix to Voyage (CCE for {@code knowledge__}/{@code docs__}/
 *       {@code rdr__}, voyage-code-3 for {@code code__}), ONNX fallback for
 *       unrecognised prefixes. Absent: ONNX-only (local mode).</li>
 *   <li>{@code NX_EMBED_PROFILE} — optional, {@code voyage} or {@code bge} (RDR-210): the
 *       family new collections get. Seeds {@code nexus.embedding_profile}; unset keeps
 *       keyed-means-Voyage, keyless-means-bge; any other value fails the boot. A choice
 *       the boot cannot serve boots degraded. The profile also picks the reranker
 *       ({@code BootPlan}): {@code voyage} with a key reranks with Voyage, every other
 *       combination with the local cross-encoder, so a {@code bge} profile sends no rerank
 *       text to Voyage even on a keyed box.</li>
 * </ul>
 *
 * <p>Binds to {@code 127.0.0.1} only (loopback). No external TLS — forward proxy
 * or supervisor is responsible for TLS termination in production.
 */
public final class Main {

    private static final Logger log = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) throws Exception {
        // nexus-17jvq: FIRST, before anything can load a DJL class. DJL's tokenizer build calls home to the
        // AWS metadata address and its CUDA path downloads a JNI library; the engine loads only local files.
        LocalTokenizers.goOffline();
        // nexus-rph82: one clock for the whole process, pinned before any
        // datasource negotiates a session zone. See SchemaMigrator.pinJvmTimeZoneToUtc.
        // nexus-9gaj7: the pin asserts itself and fails loud (System.exit(1))
        // rather than let a silently-ignored TimeZone.setDefault leave every
        // downstream UTC assumption (Liquibase's dateexecuted stamp, tsOrNull,
        // the SET TIME ZONE session pin) quietly wrong.
        try {
            SchemaMigrator.pinJvmTimeZoneToUtc();
        } catch (SchemaMigrator.TimeZonePinFailedException e) {
            log.error("event=jvm_timezone_pin_failed_at_boot error=\"{}\"", e.getMessage(), e);
            System.exit(1);
        }
        // nexus-o5xyx.1: FIRST, before anything can start native ONNX init. A SIGTERM
        // during OrtSession creation otherwise lets onnxruntime-java's own shutdown hook
        // free ORT's logging manager under the live init and the JVM SEGVs (exit 134
        // instead of 143). This defers signal-driven exit until in-flight model inits
        // finish (bounded), before any hook runs; see OrtInitGate.
        OrtInitGate.process().installSignalHandlers();
        // nexus-f9bgu.11: onnxruntime-java leaves a %TEMP%\onnxruntime-java<random> directory
        // per start on Windows (its DLLs are locked at exit, so deleteOnExit loses). Remove
        // the dead ones from earlier runs; Windows only, best effort, never throws.
        OrtTempSweep.sweepAtBoot();
        int port   = intEnv("NX_SERVICE_PORT", 8080);
        // RDR-152 bead nexus-gmiaf.32.5: NX_SERVICE_TOKEN is the persistent random
        // root bearer token (minted + persisted by `nx init --service`). Auth resolves
        // bearer→tenant against the service_tokens registry; this token is seeded BOUND
        // to the default tenant (the wildcard bootstrap is retired — no token crosses
        // tenants anymore). Optional: a deployment whose tokens already live in the DB
        // can start without it.
        String token  = System.getenv("NX_SERVICE_TOKEN");
        String dbUrl  = requireEnv("NX_DB_URL");
        String dbUser = requireEnv("NX_DB_USER");
        String dbPass = requireEnv("NX_DB_PASS");
        int poolSize  = intEnv("NX_POOL_SIZE", 10);

        var hikari = new HikariConfig();
        hikari.setJdbcUrl(dbUrl);
        // nexus-u9zkn: OS-level dead-peer backstop on an idle connection. The read bound on a request in
        // flight is per path, set in PgSession#setLocal; there is no pool-wide socketTimeout. PoolKeepAlive,
        // not PgSession, because this runs before the boot catch below and PgSession's static initializers
        // parse env: a bad value must reach that catch, not escape main as ExceptionInInitializerError.
        dev.nexus.service.db.PoolKeepAlive.apply(hikari, dbUrl);
        hikari.setUsername(dbUser);
        hikari.setPassword(dbPass);
        hikari.setMaximumPoolSize(poolSize);
        hikari.setAutoCommit(true);   // pool default; TenantScope toggles to false per borrow
        // nexus-cbo4a batch 9 item 0 (Sam's directive, 2026-09-05, nexus-zrcj7): the
        // session-search_path connectionInitSql this pool used to carry ("SET search_path
        // TO nexus, t1, public") is retired -- every nexus/t1 table and routine reference
        // already goes through jOOQ generated Tables/Routines, which render fully
        // schema-qualified SQL regardless of session search_path (renderSchema defaults
        // true, unoverridden anywhere in this codebase). The real (undocumented) dependency
        // was the bare pgvector/pg_trgm operators and types in 33 search/hybrid/taxonomy-ANN
        // SQL functions, which now explicitly qualify every such reference as nexus.* (both
        // extensions are relocated into the nexus schema -- see
        // search-path-001-relocate-vector-extensions.xml and search-path-002-qualify-
        // vector-operator-functions.xml) instead of pinning a function-level search_path.
        // nexus-g17tf: per-boot unique application_name so the shutdown hook can
        // terminate THIS process's backends (and only these) via pg_stat_activity.
        String applicationName = dev.nexus.service.db.BackendReaper.newApplicationName(
                dev.nexus.service.http.VersionHandler.resolveReleaseVersion());
        hikari.addDataSourceProperty("ApplicationName", applicationName);
        var ds = new HikariDataSource(hikari);

        // Boot validation of every env-resolved PgSession setting. PgSession resolves them in static
        // initializers, so the FIRST PgSession static call anywhere in the process runs the parse, and a malformed
        // value there arrives as ExceptionInInitializerError. This block is that first call on purpose: it needs
        // only the pool, and it runs before anything else that touches PgSession (LocalOnnxAdmission.fromEnv in
        // the local-mode branch below, then the embedding-profile seed through TenantScope), so the one catch
        // below reports the failure as event=pg_session_env_invalid and exits 1 instead of letting a stack trace
        // escape main. MainPciWiringTest pins this order; nexus-43ulx.12 fix round (review I3).
        // nexus-4ktfm review fold: force NX_HNSW_EF_SEARCH validation NOW —
        // PgSession resolves it in a static initializer, and without this
        // boot-time touch a malformed value would surface only at the first
        // query and then poison the class (NoClassDefFoundError) for the
        // process's life, invisible to health checks. Throwable, not Exception:
        // a static-init failure arrives as ExceptionInInitializerError.
        try {
            log.info("event=hnsw_ef_search_floor floor={}",
                     dev.nexus.service.db.PgSession.startupEfSearchFloor());
            // nexus-g17tf: same fail-fast for NX_SEARCH_STATEMENT_TIMEOUT_MS.
            log.info("event=search_statement_timeout timeout_ms={}",
                     dev.nexus.service.db.PgSession.startupSearchStatementTimeoutMs());
            // nexus-wbfpw.47: the search scan budget. Validates NX_HNSW_MAX_SCAN_TUPLES /
            // NX_HNSW_SCAN_MEM_BUDGET_MB, reads the engine role's effective work_mem and
            // derives hnsw.scan_mem_multiplier from the fixed memory budget.
            var scanBudget = dev.nexus.service.db.PgSession.startupScanBudget(
                org.jooq.impl.DSL.using(ds, org.jooq.SQLDialect.POSTGRES));
            log.info("event=hnsw_scan_budget max_scan_tuples={} work_mem_bytes={} "
                     + "mem_budget_bytes={} mem_multiplier={} effective_mem_bytes={}",
                     scanBudget.maxScanTuples(), scanBudget.workMemBytes(),
                     scanBudget.budgetBytes(), scanBudget.memMultiplier(),
                     scanBudget.effectiveMemBytes());
            // nexus-tu8wp.6: same fail-fast for the cardinality router's NX_SEARCH_EXACT_MAX_ROWS.
            log.info("event=search_exact_router max_rows={}",
                     dev.nexus.service.db.PgSession.startupSearchExactMaxRows());
            // nexus-43ulx.10 (RDR-227): same fail-fast for the four NX_SEARCH_PCI* settings; logs
            // event=pci_settings, plus a WARN when B > T (T > 0).
            dev.nexus.service.db.PgSession.logPciBootSettings(
                dev.nexus.service.db.PgSession.startupPciSettings(),
                dev.nexus.service.db.PgSession.startupSearchExactMaxRows());
            // nexus-r0vkh: same fail-fast for the taxonomy assign bounds.
            log.info("event=taxonomy_assign_bounds statement_timeout_ms={} lock_timeout_ms={}",
                     dev.nexus.service.db.PgSession.startupTaxonomyAssignStatementTimeoutMs(),
                     dev.nexus.service.db.PgSession.startupTaxonomyAssignLockTimeoutMs());
            // nexus-u9zkn: same fail-fast for NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS.
            log.info("event=pg_network_bound margin_ms={} tcp_keep_alive=true",
                     dev.nexus.service.db.PgSession.startupNetworkBoundMarginMs());
        } catch (Throwable t) {
            ds.close();
            // One catch for every env-resolved PgSession bound above (ef_search,
            // the search statement timeout, the scan budget, the taxonomy assign bounds, the network
            // bound margin); the
            // parse's own message names the variable that failed.
            // A static-init failure arrives wrapped; its message is null and the cause names the variable.
            Throwable cause = (t instanceof ExceptionInInitializerError && t.getCause() != null) ? t.getCause() : t;
            log.error("event=pg_session_env_invalid error=\"{}\"", cause.getMessage(), t);
            System.exit(1);
        }

        // ── Schema migration (RDR-152 bead nexus-net63) ───────────────────────
        // Run Liquibase BEFORE the HTTP server binds so the service never serves
        // requests against an unmigrated database.  Fail fast on any error so
        // the process exits non-zero and the supervisor knows not to route traffic.
        //
        // When NX_DB_ADMIN_* are set, use a dedicated single-connection migration
        // pool whose credentials have DDL rights (schema-owner or superuser).
        // Falls back to NX_DB_* when NX_DB_ADMIN_* are absent, covering dev/test
        // setups where the application role also owns the schema.
        // RDR-227: the resolved admin values stay in scope for the per-collection index builder.
        var adminConnection = dev.nexus.service.db.AdminConnection.resolve(System::getenv, dbUrl, dbUser, dbPass);
        var migrationDs = buildMigrationDataSource(adminConnection);
        try {
            SchemaMigrator.migrate(migrationDs);
        } catch (SchemaMigrator.MigrationException e) {
            // Close the migration pool BEFORE System.exit: exit does not run finally blocks,
            // so explicit close here avoids leaking the pool on the error path.
            migrationDs.close();
            log.error("event=schema_migration_failed error=\"{}\"", e.getMessage(), e);
            System.exit(1);
        }
        migrationDs.close();

        // nexus-wbfpw.48: identity-based tenant-isolation backstop for nexus.chunks, on the SERVICE pool so
        // current_user is the role that actually serves tenant traffic, whatever NX_DB_USER names. Refuses to
        // boot when a permissive policy other than tenant_isolation applies to it (vectors-029's owner
        // policy bound to the wrong role, or a later membership grant): such a role reads every tenant's
        // chunks with no error anywhere. See ChunksIsolationCheck.
        try {
            dev.nexus.service.db.ChunksIsolationCheck.verifyAtStartup(ds);
        } catch (dev.nexus.service.db.ChunksIsolationCheck.IsolationException e) {
            ds.close();
            log.error("event=chunks_isolation_check_failed error=\"{}\"", e.getMessage());
            System.exit(1);
        }

        // Root-token provisioning (RDR-152 bead nexus-gmiaf.32.5): seed NX_SERVICE_TOKEN
        // as a BOUND default-tenant row. The transitional wildcard (tenant_id="*") is
        // retired — every token, including the root, is strictly tenant-bound and the
        // client X-Nexus-Tenant header never crosses tenants. Runs AFTER migration (table
        // exists) as the app role (nexus_svc has INSERT via grants-nexus-svc). Idempotent
        // (ON CONFLICT DO NOTHING). No-op when NX_SERVICE_TOKEN is unset.
        // nexus-kjjab: this call was NOT in a try, and main declares `throws Exception`, so
        // any failure here left the JVM on a bare stack trace with HTTP never bound — the
        // one startup step of the three whose failure was un-diagnosable. Report it the way
        // the migration and pooler checks above report theirs.
        try {
            new dev.nexus.service.db.TokenStore(ds, java.time.Clock.systemUTC())
                .ensureBootstrapToken(token, dev.nexus.service.db.TenantConstants.DEFAULT_TENANT);
        } catch (dev.nexus.service.db.TokenStore.BootstrapTokenConflict e) {
            // Deliberate refusal, with a remedy in the message. Exit non-zero rather than
            // bind: a service whose root token does not authenticate is not a running
            // service, and booting anyway would hide the cause behind 401s.
            log.error("event=root_token_seed_refused error=\"{}\"", e.getMessage());
            System.exit(1);
        } catch (RuntimeException e) {
            log.error("event=root_token_seed_failed error=\"{}\"", e.getMessage(), e);
            System.exit(1);
        }

        // Vector backend (RDR-155 P4a.2, bead nexus-1k8s1): pgvector serves every
        // vector route against the SAME Postgres the service already requires — no
        // separate vector store process. Embedders route by collection prefix
        // (Voyage when NX_VOYAGE_API_KEY is present, ONNX otherwise/fallback).
        // PRODUCTION WIRING INVARIANT (P2.2 contract, recorded on nexus-1k8s1):
        // PgVectorRepository takes the ROUTER constructor — EmbedderRouter through the
        // plain-Embedder constructor would fall back to ONNX for ALL collections and
        // break cloud-mode routing (caught only at the first upsert's dim check).
        String voyageKey = System.getenv("NX_VOYAGE_API_KEY");
        EmbedderRouter docEmbedRouter;
        EmbedderRouter qryEmbedRouter;
        // Bead nexus-s71lr, deliverable 2: non-null ONLY when bge is built (in
        // either the bge-only or the dual shape) — null on the Voyage-only shape,
        // where GET /v1/status reports local_embed_activity: null rather than
        // fabricating a value for an embedder that does not exist here.
        java.util.function.Supplier<dev.nexus.service.vectors.EmbedActivitySnapshot> localEmbedActivitySupplier = null;
        dev.nexus.service.vectors.Reranker reranker;

        // RDR-210 Phase 2 Step 1 (nexus-uaovy): build whatever embedder family is
        // PRESENT — bge only, Voyage only, or both. The probe only stats files (it
        // never touches ONNX Runtime; building a session on a missing model file
        // segfaults), so it is safe on the cloud shape (key, no model files).
        boolean voyagePresent = voyageKey != null && !voyageKey.isBlank();
        var presence = dev.nexus.service.vectors.LocalModelProbe.probe(voyageKey);
        log.info("event=embedder_presence bge_present={} voyage_key_present={} bge=\"{}\" "
                + "cross_encoder_present={} cross_encoder=\"{}\"",
                presence.bgePresent(), presence.voyageKeyPresent(), presence.bgeDetail(),
                presence.crossEncoderPresent(), presence.crossEncoderDetail());
        // RDR-210 Phase 2 Step 3 (nexus-qhclj): the embedding profile is a CHOICE the
        // supervisor passes in NX_EMBED_PROFILE (voyage | bge). Unset keeps today's rule
        // (keyed means Voyage, keyless means bge). An invalid value fails the boot loudly,
        // before anything is built; the seed below follows the profile, not what was built.
        dev.nexus.service.vectors.EmbeddingProfile embedProfile;
        try {
            embedProfile = dev.nexus.service.vectors.EmbeddingProfile.resolve(
                    System.getenv(dev.nexus.service.vectors.EmbeddingProfile.ENV), voyagePresent);
        } catch (IllegalArgumentException e) {
            ds.close();
            log.error("event=embed_profile_invalid error=\"{}\"", e.getMessage());
            System.exit(1);
            return;
        }
        log.info("event=embed_profile_resolved profile={} source={}", embedProfile.value(),
                dev.nexus.service.vectors.EmbeddingProfile.parse(
                        System.getenv(dev.nexus.service.vectors.EmbeddingProfile.ENV)).isPresent()
                        ? "NX_EMBED_PROFILE" : "unset_default");
        // RDR-210 Phase 2 Step 4 (nexus-un7vj): every decision about what this boot builds (the three-way
        // bge decision, the reranker the profile picks, the ONNX native directory, the admission gate) is
        // made by value in BootPlan, so a test asserts it without running main. What follows only acts on it.
        var plan = dev.nexus.service.vectors.BootPlan.decide(presence, embedProfile);
        if (plan.bge() == dev.nexus.service.vectors.BootPlan.Bge.FAIL_PARTIAL_NO_KEY) {
            // nexus-ngvxb (RDR-210 TD1, Sam 2026-10-18): exactly one of the bge model and its
            // tokenizer is on disk. Never construct Bge768Embedder or touch ORT for it.
            // Keyless: nothing is servable. Fail loud, naming the missing file(s).
            ds.close();
            log.error("event=bge_partial_install_no_voyage_key error=\"the bge install is partial "
                    + "(exactly one of the model and the tokenizer is on disk) and no Voyage key is "
                    + "set, so no embedder can be built. Repair the install with `"
                    + dev.nexus.service.vectors.BootPlan.PROVISION_COMMAND + "`, or set "
                    + "NX_VOYAGE_API_KEY.\" missing={} bge=\"{}\"",
                    presence.bgeMissing(), presence.bgeDetail());
            System.exit(1);
        }
        if (plan.bge() == dev.nexus.service.vectors.BootPlan.Bge.PARTIAL_DEGRADED) {
            // Keyed: boot with the family that can be served (Voyage only), loudly.
            log.warn("event=bge_partial_install_degraded missing={} bge=\"{}\" serving=voyage_only "
                    + "local_collections=REFUSED_422 hint=\"the local bge install is incomplete; "
                    + "run `" + dev.nexus.service.vectors.BootPlan.PROVISION_COMMAND
                    + "` to repair it, then restart the engine\"",
                    presence.bgeMissing(), presence.bgeDetail());
        }
        if (plan.bge() == dev.nexus.service.vectors.BootPlan.Bge.FAIL_NO_EMBEDDER) {
            // Neither family can be built: a boot failure that names BOTH remedies.
            ds.close();
            log.error("event=no_embedder_available error=\"no embedder family is present: "
                    + "the Voyage key is not set and the bge model files are not on disk. "
                    + "Either set NX_VOYAGE_API_KEY (or persist the key with `nx config set "
                    + "voyage_api_key --stdin` so the supervisor passes it), or provision the "
                    + "local bge model with `" + dev.nexus.service.vectors.BootPlan.PROVISION_COMMAND
                    + "`.\" bge=\"{}\"",
                    presence.bgeDetail());
            System.exit(1);
        }
        // A choice this boot cannot serve boots DEGRADED, never refused (Sam, RDR-210 gate
        // round 1): the profile is still seeded with the chosen family's tokens, the other
        // family serves, and only the chosen family's collections get the 422.
        plan.degradation().ifPresent(d -> log.warn(
                "event=embed_profile_degraded profile={} missing=\"{}\" detail=\"{}\" serving={} "
                + "{}_collections=REFUSED_422 hint=\"{}\"",
                d.chosen().value(), d.missingPiece(), d.detail(), d.serving().value(),
                d.refusedCollections(), d.remedy()));

        // ONNX Runtime is touched ONLY when a local ONNX model file is on disk: the cloud shape (key, no
        // model files) never enters this block, so it extracts nothing and creates no ONNX session. The
        // condition is the files that are PRESENT (bge or cross-encoder), not what will be constructed
        // (BootPlan.loadsOrtNativeDir), because the cross-encoder is built lazily, after this point.
        if (plan.loadsOrtNativeDir()) {
            // nexus-ogccs: log the resolved onnx_models root BEFORE the first local model is
            // built — its fail-loud missing-model path should follow a line saying where the
            // engine looked and why.
            var onnxRoot = dev.nexus.service.vectors.OnnxModelPaths.resolved();
            log.info("event=onnx_model_root root={} source={}",
                    onnxRoot.root(), onnxRoot.source());
            // nexus-8ool4: load ONNX Runtime's native libraries from one per-version directory
            // extracted once under the engine's cache tree, instead of unpacking ~16 MB into a fresh
            // /tmp/onnxruntime-java<random> on every start (a killed engine never deletes it). Sets
            // onnxruntime.native.path unless the operator already did; best effort, never throws;
            // must precede any ORT use.
            OrtNativeDir.applyAtBoot();
        }
        // nexus-00wsf residual, review round 2 (T2 [24238]/[24239]): ONE
        // LocalOnnxAdmission per process, shared by BOTH routers and the
        // reranker below — wrapping "bge" separately per router (round 1's
        // defect) gave each router its own semaphore and doubled the
        // effective bound instead of capping the process-wide total.
        // Document embeds block (bulk indexing is not latency-sensitive);
        // query embeds and reranks use the bounded-timeout acquire so a
        // bulk-indexing burst cannot stall interactive search unboundedly.
        // It wraps only the ONNX members, never the Voyage embedders.
        // RDR-210 Step 4 (nexus-un7vj, nexus-e81g7): built whenever bge OR the cross-encoder is, not only
        // when bge is: a bge-profile engine with no bge files still reranks through the cross-encoder, which
        // shares this gate. fromEnv() reads PgSession, so this stays after the boot validation block above
        // (MainPciWiringTest pins that order).
        dev.nexus.service.vectors.LocalOnnxAdmission localOnnxAdmission = plan.buildsAdmissionGate()
                ? dev.nexus.service.vectors.LocalOnnxAdmission.fromEnv() : null;
        // The local (bge) member, present only when its files are.
        Bge768Embedder bge = null;
        if (plan.buildsBge()) {
            // Local model (RDR-160): bge-768. MiniLM is NOT loaded (Decision 5) — a
            // non-bge row is REFUSED (422), never silently embedded at the wrong dim.
            try {
                bge = new Bge768Embedder();
            } catch (OrtInitGate.ShutdownInProgressException e) {
                // A signal arrived before model init began: the gate is closed and its
                // deferred exit is already running on another thread. Do not start ORT;
                // wait for the exit rather than dying on a stack trace.
                log.info("event=boot_aborted_by_shutdown stage=bge768_init");
                Thread.currentThread().join();
                return;
            }
            // Bead nexus-s71lr: composition-root wiring so the progress log line and
            // GET /v1/status can report REAL queue-depth/thread-width (read from this
            // same admission gate) instead of omitting them. bge itself never looks
            // the gate up — it is handed the reference exactly once, here.
            bge.setAdmissionGate(localOnnxAdmission);
            final Bge768Embedder builtBge = bge;
            localEmbedActivitySupplier = builtBge::activitySnapshot;
        }
        var docLocal = bge == null ? null
                : new dev.nexus.service.vectors.AdmissionControlledEmbedder(bge, localOnnxAdmission, false);
        var qryLocal = bge == null ? null
                : new dev.nexus.service.vectors.AdmissionControlledEmbedder(bge, localOnnxAdmission, true);
        String routerKey = voyagePresent ? voyageKey : null;
        docEmbedRouter = new EmbedderRouter(docLocal, routerKey, "document", embedProfile);
        qryEmbedRouter = new EmbedderRouter(qryLocal, routerKey, "query", embedProfile);

        // One reranker per engine, chosen by the PROFILE (RDR-210 Technical Design 3), not by the key: a
        // voyage profile with a key reranks with Voyage (RDR-188: the same key grants the fused rerank
        // stage, the engine owns ALL Voyage traffic, no new credential surface); every other combination
        // reranks with the local ms-marco cross-encoder, so a bge profile sends no rerank text to Voyage
        // even on a keyed box. NX_RERANK_MODEL (RDR-157 config-via-env) is the SERVER-side successor of the
        // client's embeddings.rerankerModel knob (e.g. rerank-2.5-lite for cost/latency): model choice
        // moved server-side with the traffic — there is deliberately NO per-request model field.
        // The cross-encoder is lazy (its constructor touches no I/O): a not-yet-provisioned model degrades
        // the fused stage LOUD per request instead of failing boot, and is picked up on first rerank once
        // `nx init --service` lands it. It shares localOnnxAdmission (finding 4) and needs no bge embedder.
        String rerankModelEnv = System.getenv("NX_RERANK_MODEL");
        String rerankModel = (rerankModelEnv == null || rerankModelEnv.isBlank())
                ? dev.nexus.service.vectors.VoyageReranker.DEFAULT_MODEL
                : rerankModelEnv.trim();
        reranker = plan.buildReranker(
                () -> new dev.nexus.service.vectors.VoyageReranker(voyageKey, rerankModel),
                dev.nexus.service.vectors.CrossEncoderReranker::new,
                localOnnxAdmission);
        if (plan.crossEncoderFilesMissing()) {
            log.warn("event=cross_encoder_files_absent rerank=SKIPPED_PER_REQUEST voyage_rerank_calls=NONE "
                    + "cross_encoder=\"{}\" hint=\"run `{}` to provision the reranker, then restart the engine\"",
                    presence.crossEncoderDetail(), dev.nexus.service.vectors.BootPlan.PROVISION_COMMAND);
        }
        // nexus-pebfx.2: LOUD one-line embedding-mode banner. The 2026-06-10 migration ran for hours against
        // silent ONNX-384 fallback because the mode was invisible. RDR-210 Step 4: it names both families
        // (what was built, what is refused), the profile and the reranker; a keyless box, a degraded choice,
        // a partial bge install or an absent cross-encoder logs at WARN and carries the remedy.
        if (plan.loudBanner()) {
            log.warn("event=embedding_mode_banner mode={} models={} profile={} reranker={} rerank_via={} "
                    + "bge={} voyage={} backend=pgvector refused_422={} hint=\"{}\"",
                    docEmbedRouter.modeName(), docEmbedRouter.availableModels(), embedProfile.value(),
                    reranker.modelToken(), plan.rerankVia(), plan.bgeState(),
                    voyagePresent ? "key" : "no_key", plan.refusedFamilies(), plan.bannerHint());
        } else {
            log.info("event=embedding_mode_banner mode={} models={} profile={} reranker={} rerank_via={} "
                    + "bge={} voyage={} backend=pgvector refused_422={}",
                    docEmbedRouter.modeName(), docEmbedRouter.availableModels(), embedProfile.value(),
                    reranker.modelToken(), plan.rerankVia(), plan.bgeState(),
                    voyagePresent ? "key" : "no_key", plan.refusedFamilies());
        }
        var tenantScope = new TenantScope(ds);

        // Embedding profile (RDR-204 Phase 1, bead nexus-ft04v.6): the engine is
        // the only writer of nexus.embedding_profile, and the profile (NX_EMBED_PROFILE,
        // resolved above; the routers hold it) IS what is seeded. Seed the LOCAL
        // tenant's row for every content type now, after the routers exist —
        // idempotent (a second boot in the same mode changes nothing; a boot
        // after a config-driven mode switch overwrites every row). Fail loud,
        // same shape as the root-token seed above: a service whose profile
        // write fails is not in a state bead nexus-ft04v.8's register_collection
        // can trust.
        try {
            docEmbedRouter.seedEmbeddingProfile(
                    tenantScope, dev.nexus.service.db.TenantConstants.DEFAULT_TENANT);
        } catch (RuntimeException e) {
            ds.close();
            log.error("event=embedding_profile_seed_failed error=\"{}\"", e.getMessage(), e);
            System.exit(1);
        }

        // Pooler-mode backstop (nexus-bhzuv): if a PgBouncer is interposed
        // (NX_PGBOUNCER_ADMIN_URL set), refuse to bind unless it reports
        // pool_mode=transaction. SET LOCAL tenant GUCs leak across borrows under
        // session-mode pooling → cross-tenant read. No-op on the direct-PG path.
        // Runs BEFORE service.start(), mirroring the schema-migration fail-fast ordering.
        try {
            dev.nexus.service.db.PoolerModeCheck.verifyAtStartup();
        } catch (dev.nexus.service.db.PoolerModeCheck.PoolerModeException e) {
            ds.close();
            log.error("event=pooler_mode_check_failed error=\"{}\"", e.getMessage(), e);
            System.exit(1);
        }

        // RDR-205 (bead nexus-em75s.4): load + boot-check the tuple template
        // registry BEFORE the service starts serving — a breach (an
        // unparseable/malformed template, or a claim-log TTL that does not
        // exceed some template's retention by strictly more than one sweep
        // interval) refuses to start the process, same fail-fast-at-boot
        // shape as the pooler-mode and HNSW-floor checks above. Same one
        // clock this process pinned to UTC at the very top of main().
        dev.nexus.service.tuples.TemplateRegistry tupleTemplateRegistry;
        try {
            tupleTemplateRegistry = dev.nexus.service.tuples.TemplateRegistry.loadAtBoot(
                    NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        } catch (dev.nexus.service.tuples.TemplateRegistryException e) {
            ds.close();
            log.error("event=tuple_template_registry_load_failed error=\"{}\"", e.getMessage(), e);
            System.exit(1);
            return;
        }

        // nexus-43ulx.12 (RDR-227): the router's valid-index set, built from the NX_SEARCH_PCI* settings the boot
        // validation block after the pool (above the schema migration) has already parsed, and handed to the
        // repository as its only index set (the empty default belongs to tests, never to production).
        var pciSweep = dev.nexus.service.vectors.PciIndexSweep.create(ds, dev.nexus.service.db.PgSession.startupPciSettings());
        var pgVectorRepo = new PgVectorRepository(tenantScope, docEmbedRouter, qryEmbedRouter, pciSweep);
        // nexus-wym0l: fix and log the fan-out arm cap against the pool this process runs with.
        pgVectorRepo.startupFanoutArmPermits();

        var service = new NexusService(port, token, ds, docEmbedRouter, pgVectorRepo, reranker,
                localEmbedActivitySupplier, tupleTemplateRegistry);
        // RDR-210 Step 4 (nexus-un7vj): /version and /v1/status report the profile, the reranker and any
        // degraded-boot description beside the embedding mode. Bound before the listener starts.
        service.embedPosture(dev.nexus.service.http.EmbedPosture.of(plan, reranker));
        service.start();
        // The read half runs in every engine, whatever NX_SEARCH_PCI says: one read now, then one per
        // NX_SEARCH_PCI_SWEEP_SECONDS. Started after the service so the pool is serving; stopped in the
        // shutdown hook after the backend reaper and before ds.close(). Until the first read lands the set is empty and every single-collection
        // statement above the router threshold walks at hnsw.ef_search 1000, the safe direction.
        pciSweep.start();
        // nexus-43ulx.19: the DDL half, on a task of its own beside the read half. It acts only when NX_SEARCH_PCI=1
        // (start() schedules nothing otherwise) and only on the engine that holds the builder lock. Started after
        // the read half so its first refresh() has a set to replace; stopped in the hook right after the listener and
        // before the builder reaper, so a pass cannot open a new builder connection after the reaper ended the old one.
        String pciBootNonce = dev.nexus.service.db.BackendReaper.bootNonce(applicationName);
        var pciReconciler = dev.nexus.service.vectors.PciReconciler.create(ds, adminConnection, pciBootNonce, pciSweep,
                dev.nexus.service.db.PgSession.startupPciSettings());
        pciReconciler.start();
        // nexus-43ulx.23: GET /v1/status's per_collection_indexes, served from what the two halves already hold (the
        // sweep's last read, the reconciler's cached status): no catalog query per request. Bound here because the
        // route is registered in NexusService's constructor, before either half exists; the key is omitted until now.
        service.perCollectionIndexes(() -> dev.nexus.service.http.StatusHandler.PerCollectionIndexes.of(
                pciSweep.status(), pciReconciler.status()));

        log.info("event=service_ready port={}", service.getPort());

        // Parent-death watchdog (nexus-03bcg): exit if the supervisor (our parent
        // process) dies, so an OOM-killed supervisor leaves no orphaned-but-serving
        // JVM (the orphan would keep /health green while its lease ages out, and a
        // heal-on-next-use re-spawn would then double-spawn a 2nd JVM). Opt-in via
        // NX_SERVICE_PARENT_DEATH_EXIT=1 (set by the supervisor when it spawns us)
        // so standalone/test runs with no supervisor are unaffected. This is the
        // portable (Linux + macOS) complement to the Linux-only PR_SET_PDEATHSIG
        // the supervisor also arms on the child.
        if ("1".equals(System.getenv("NX_SERVICE_PARENT_DEATH_EXIT"))) {
            ProcessHandle.current().parent().ifPresentOrElse(parent -> {
                Thread watchdog = new Thread(() -> {
                    try {
                        parent.onExit().get();   // completes when the supervisor exits
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        return;
                    } catch (Exception e) {       // onExit unsupported for this handle (RuntimeException)
                        log.warn("event=parent_death_watchdog_error error=\"{}\"", e.toString());
                        return;
                    }
                    log.warn("event=supervisor_exited action=self_exit "
                            + "reason=orphan_prevention parent_pid={}", parent.pid());
                    // halt, not exit: orphan-prevention is a hard kill, not a graceful
                    // shutdown. System.exit would run the shutdown hook → HikariCP
                    // ds.close() can stall ~30s on a dead PG (the OOM path took the
                    // supervisor AND likely PG). On Linux PR_SET_PDEATHSIG SIGKILLs us
                    // first anyway; halt makes the macOS path equally prompt (CRE M-1).
                    Runtime.getRuntime().halt(143);
                }, "parent-death-watchdog");
                watchdog.setDaemon(true);
                watchdog.start();
                log.info("event=parent_death_watchdog_armed parent_pid={}", parent.pid());
            }, () -> log.warn("event=parent_death_watchdog_skipped reason=no_parent_handle"));
        }

        // RDR-227 (nexus-43ulx.17): the per-collection index builder's backend is named
        // nexus-pci-builder-<this boot's nonce> and runs as the admin role.
        String pciBuilderApplicationName = dev.nexus.service.vectors.PciBuilderSession.builderApplicationName(
                pciBootNonce);
        // nexus-43ulx.19: the reconciler's scheduler stops here with shutdownNow() and NO await (an in-flight
        // CREATE INDEX CONCURRENTLY is ended by the builder reaper below, not waited for).
        Runnable stopPciReconciler = pciReconciler::stop;

        // Everything the shutdown hook does that does not touch ONNX Runtime. Also the whole
        // cleanup on the gate's expired-bound path (nexus-y6aj0), which ends in libc _exit
        // and so runs no hook: native model work is still live there, so the embedder
        // closes are skipped, and so is ds.close(), which can stall about 30 s on a dead PG
        // (process exit closes the sockets, and the reaper has already ended the backends).
        Runnable stopAllButEmbedders = () -> {
            service.stop();
            stopPciReconciler.run();
            // nexus-g17tf: right after the listener stops and the reconciler's stop
            // (both return at once: neither waits), ahead of the embedder
            // closes (each can wait up to 5s) so the reaper always runs inside a
            // 10s container stop grace. Hikari's close aborts the sockets, and a
            // CPU-bound backend never notices a closed socket; only the postmaster
            // signal pg_terminate_backend sends reaches it. Fresh connection, never
            // a pool borrow (the pool is what the runaways hold). SIGKILL (OOM, an
            // expired grace) runs no hook at all: that path is bounded only by the
            // statement_timeout on the search transactions.
            //
            // nexus-43ulx.17: the pool's reaper (application role) and the builder's
            // (admin role: pg_terminate_backend needs the target's role) run TOGETHER
            // under one 9 s deadline. Worst case: each call is 3 s connect + 6 s socket
            // read = 9 s, so in sequence they could take 18 s and miss the 10 s stop
            // grace; together the pair costs 9 s, what the pool's call alone always did.
            // A call still running at the deadline is abandoned and logged.
            dev.nexus.service.db.BackendReaper.terminateAtShutdown(
                    dbUrl, dbUser, dbPass, applicationName,
                    ds.getHikariPoolMXBean().getActiveConnections(),
                    adminConnection, pciBuilderApplicationName);
            // nexus-43ulx.12: end the catalog reads before the pool they borrow from closes. AFTER the reaper and
            // never waiting: a read on a silent socket ignores the interrupt, and the reaper has a 10 s grace to
            // keep; closing the pool below aborts that socket.
            pciSweep.stop();
        };
        OrtInitGate.process().onBoundExpired(stopAllButEmbedders);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            log.info("event=shutdown_signal");
            stopAllButEmbedders.run();
            // doc and qry routers each hold their OWN AdmissionControlledEmbedder
            // (different acquisition policy — see LocalOnnxAdmission), but both
            // wrap the SAME underlying "bge" delegate, so closing either
            // AdmissionControlledEmbedder.close() reaches the same native ONNX
            // session + tokenizer; a second close is harmless (close() swallows
            // it). But each router owns its OWN CceEmbedder with its OWN bounded
            // virtual-thread executor for the parallel per-chunk fan-out
            // (nexus-9okyk) — both routers must be closed to shut both down.
            docEmbedRouter.close();
            qryEmbedRouter.close();
            ds.close();
        }));

        // Block main thread until shutdown
        Thread.currentThread().join();
    }

    // ── Migration datasource factory ─────────────────────────────────────────

    /**
     * Builds a single-connection HikariCP pool for schema migration.
     *
     * <p>Uses {@code NX_DB_ADMIN_*} when present, falling back to the supplied
     * {@code defaultUrl/defaultUser/defaultPass} (the regular application
     * credentials) for dev/test setups where one role owns both DDL and DML.
     *
     * <p>The partial-config guard (all three {@code NX_DB_ADMIN_*} values or none) lives in
     * {@link dev.nexus.service.db.AdminConnection#resolve}, which {@code main} calls once; this method
     * only turns the resolved values into a pool.
     *
     * <p>Pool size 1: Liquibase uses a single connection sequentially.
     */
    private static HikariDataSource buildMigrationDataSource(dev.nexus.service.db.AdminConnection admin) {
        String url = admin.url();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(url);
        cfg.setUsername(admin.user());
        cfg.setPassword(admin.password());
        cfg.setMaximumPoolSize(1);
        cfg.setMinimumIdle(1);
        cfg.setConnectionTimeout(30_000);
        cfg.setPoolName("nexus-migration");
        // nexus-u9zkn: tcpKeepAlive only. The migration walk has no statement bound (one changeset may
        // rewrite gigabytes in a single transactional statement), so it gets no read bound either.
        dev.nexus.service.db.PoolKeepAlive.apply(cfg, url);
        return new HikariDataSource(cfg);
    }

    private static String requireEnv(String name) {
        String v = System.getenv(name);
        if (v == null) {
            throw new IllegalStateException("Required environment variable not set: " + name);
        }
        return v;
    }

    private static int intEnv(String name, int defaultValue) {
        String v = System.getenv(name);
        if (v == null || v.isBlank()) return defaultValue;
        return Integer.parseInt(v.trim());
    }
}
