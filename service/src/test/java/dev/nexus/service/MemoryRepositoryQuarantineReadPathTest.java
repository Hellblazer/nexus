package dev.nexus.service;

import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.nexus.tables.records.MemoryRecord;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.ParameterizedType;
import java.lang.reflect.Type;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-207 bead nexus-l3yuc.3: every {@link MemoryRepository} read path carries
 * {@code quarantined_at IS NULL}, pinned by a REFLECTION-derived walk.
 *
 * <p>The read-path set is derived from the repository's public instance methods by
 * return type, never typed by hand (RDR-207 § Test Plan). Every public instance
 * method lands in exactly one of three buckets, and an unclassified one fails the
 * test, so a read path added later cannot forget the predicate silently:
 * <ul>
 *   <li><b>read</b>: returns {@code Optional<MemoryRecord>}, {@code List<MemoryRecord>},
 *       {@link MemoryRepository.ResolveResult} or {@code List<String[]>}
 *       ({@code getProjectsWithPrefix});</li>
 *   <li><b>named exception</b>: {@code listQuarantined}, the ONE read that sees
 *       quarantined rows (positive leg);</li>
 *   <li><b>not a read</b>: everything in {@link #NOT_READ_PATHS}, by name; each is
 *       a write, a lifecycle verb, or reads another table.</li>
 * </ul>
 *
 * <p>Non-vacuity: the derived set must hold at least the twelve methods research
 * finding 4 measured; each derived method needs an argument-table entry (keyed on
 * name plus parameter types, because the compiler is not run with
 * {@code -parameters}); and the walk runs TWICE, first proving every read returns
 * the live row, then proving none returns it once quarantined.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemoryRepositoryQuarantineReadPathTest {

    private static final String SVC_ROLE = "svc_readpath_test";
    private static final String SVC_PASS = "svc_readpath_test_pass";

    /** Measured by RDR-207 research finding 4 (both search overloads count). */
    private static final int MEASURED_READ_PATHS = 12;

    /** Public instance methods that are NOT read paths over nexus.memory rows. */
    private static final Set<String> NOT_READ_PATHS = Set.of(
        "upsert", "delete", "deleteById", "expire", "mergeMemories", "putOrMerge",
        "importRow", "importBatch", "reap", "restore", "insertSummary",
        "listSummaries");

    private static final String T = "readpath-tenant";
    private static final String QP = "readpath-proj";
    private static final String QT = "quarantineprobe title";
    private static final String Q = "quarantineprobe";
    private static final String TAG = "quarantineprobe";

    PostgreSQLContainer<?> pg;
    MemoryRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;
    long qid;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new MemoryRepository(new TenantScope(svcDs));

        // The probe row: ttl 1, created 30 days ago, so the access tracking the
        // first walk performs (effective ttl = 1 * (1 + ln(n + 1))) cannot lift it
        // back under its TTL before expire runs.
        qid = repo.importRow(T, QP, QT, "quarantineprobe content body", TAG, null, null,
            1, OffsetDateTime.now(ZoneOffset.UTC).minusDays(30), 0, null);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── Derivation ────────────────────────────────────────────────────────────

    private static boolean isRead(Method m) {
        Type rt = m.getGenericReturnType();
        if (rt == MemoryRepository.ResolveResult.class) return true;
        if (rt instanceof ParameterizedType pt) {
            Type raw = pt.getRawType();
            Type arg = pt.getActualTypeArguments()[0];
            if (raw == Optional.class && arg == MemoryRecord.class) return true;
            if (raw == List.class && arg == MemoryRecord.class) return true;
            if (raw == List.class && arg == String[].class) return true;
        }
        return false;
    }

    private static String key(Method m) {
        return m.getName() + Arrays.toString(m.getParameterTypes());
    }

    /** Every public instance method, classified; unclassified ones fail here. */
    private List<Method> derivedReadPaths() {
        List<Method> reads = new ArrayList<>();
        List<String> unclassified = new ArrayList<>();
        for (Method m : MemoryRepository.class.getDeclaredMethods()) {
            int mod = m.getModifiers();
            if (!Modifier.isPublic(mod) || Modifier.isStatic(mod) || m.isSynthetic()) continue;
            if (m.getName().equals("listQuarantined")) continue;   // the named exception
            if (isRead(m)) {
                reads.add(m);
            } else if (!NOT_READ_PATHS.contains(m.getName())) {
                unclassified.add(key(m) + " -> " + m.getGenericReturnType().getTypeName());
            }
        }
        assertThat(unclassified)
            .as("every public instance method of MemoryRepository must be a read path by "
                + "return type, listQuarantined, or a name in NOT_READ_PATHS; classify the "
                + "new method (a read path added without the predicate would otherwise "
                + "resurrect cold rows silently)")
            .isEmpty();
        assertThat(reads.size())
            .as("derived read-path set must hold at least the %d methods research finding 4 "
                + "measured (a reflection filter that matches nothing cannot pass)",
                MEASURED_READ_PATHS)
            .isGreaterThanOrEqualTo(MEASURED_READ_PATHS);
        return reads;
    }

    /** {@code List.of(Object[])} would spread the array as varargs; this keeps it as one call. */
    private static List<Object[]> calls(Object[]... c) { return List.of(c); }

    /** Invocation arguments per derived method, keyed on name + parameter types. */
    private Map<String, List<Object[]>> argumentTable() {
        return Map.ofEntries(
            Map.entry("findByProject[class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, QP})),
            Map.entry("findByTitle[class java.lang.String, class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, QP, QT})),
            Map.entry("findById[class java.lang.String, long]",
                calls(new Object[] {T, qid})),
            Map.entry("resolveTitle[class java.lang.String, class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, QP, QT})),
            // Both SQL branches of the four-argument search: project set, project null.
            Map.entry("search[class java.lang.String, class java.lang.String, class java.lang.String, boolean]",
                calls(new Object[] {T, Q, QP, true}, new Object[] {T, Q, null, true})),
            Map.entry("search[class java.lang.String, class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, Q, QP})),
            Map.entry("listEntries[class java.lang.String, class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, QP, null})),
            Map.entry("getProjectsWithPrefix[class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, QP})),
            Map.entry("searchGlob[class java.lang.String, class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, Q, QP})),
            Map.entry("searchByTag[class java.lang.String, class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, Q, TAG})),
            Map.entry("getAll[class java.lang.String, class java.lang.String]",
                calls(new Object[] {T, QP})),
            // idleDays -1: cutoff is tomorrow, so every row counts as stale.
            Map.entry("flagStaleMemories[class java.lang.String, class java.lang.String, int]",
                calls(new Object[] {T, QP, -1}))
        );
    }

    /** Does this read result contain the probe row? */
    @SuppressWarnings("unchecked")
    private boolean returnsProbe(Object result) {
        if (result instanceof Optional<?> opt) {
            return opt.isPresent() && ((MemoryRecord) opt.get()).getId() == qid;
        }
        if (result instanceof MemoryRepository.ResolveResult rr) {
            if (rr.entry() != null && rr.entry().getId() == qid) return true;
            return rr.candidates().stream().anyMatch(r -> r.getId() == qid);
        }
        if (result instanceof List<?> list) {
            for (Object o : list) {
                if (o instanceof MemoryRecord r && r.getId() == qid) return true;
                if (o instanceof String[] pair && QP.equals(pair[0])) return true;
            }
            return false;
        }
        throw new AssertionError("unhandled read result type " + result.getClass());
    }

    private List<String> walk(boolean expectVisible) throws Exception {
        Map<String, List<Object[]>> table = argumentTable();
        List<String> wrong = new ArrayList<>();
        for (Method m : derivedReadPaths()) {
            List<Object[]> calls = table.get(key(m));
            assertThat(calls)
                .as("derived read path %s has no argument-table entry; add one so the walk "
                    + "covers it", key(m))
                .isNotNull();
            for (Object[] args : calls) {
                Object result = m.invoke(repo, args);
                if (returnsProbe(result) != expectVisible) {
                    wrong.add(key(m) + " with " + Arrays.toString(args));
                }
            }
        }
        return wrong;
    }

    // ── The test ──────────────────────────────────────────────────────────────

    @Test
    void everyDerivedReadPath_hidesAQuarantinedRow_andListQuarantinedShowsIt() throws Exception {
        // Walk 1 (non-vacuity): the live probe row is returned by EVERY read path,
        // so walk 2's empties are the predicate, not a mis-seeded fixture.
        assertThat(walk(true))
            .as("before quarantine every derived read path must return the probe row")
            .isEmpty();
        assertThat(repo.listQuarantined(T, QP)).isEmpty();

        // Quarantine it through the real path.
        assertThat(repo.expire(T).quarantinedIds()).containsExactly(qid);

        // Walk 2: no read path returns it; the named exception does.
        assertThat(walk(false))
            .as("after quarantine these read paths still return the probe row: a "
                + "missing quarantined_at IS NULL predicate resurrects cold rows")
            .isEmpty();
        assertThat(repo.listQuarantined(T, QP)).extracting(MemoryRecord::getId)
            .as("listQuarantined is the one read that sees quarantined rows")
            .containsExactly(qid);
    }

    @Test
    void putOrMerge_scanIgnoresAQuarantinedNearDuplicate() {
        // Tested by name (a read-then-write, not a read by return type): a
        // quarantined row is never a merge target, so an identical body under a new
        // title inserts a NEW row and leaves the hidden one untouched.
        String tenant = "readpath-merge-tenant";
        String project = "readpath-merge-proj";
        String body = "identical merge probe body with several distinctive words here";
        long hidden = repo.importRow(tenant, project, "hidden title", body, "t", null, null,
            1, OffsetDateTime.now(ZoneOffset.UTC).minusDays(30), 0, null);
        assertThat(repo.expire(tenant).quarantinedIds()).containsExactly(hidden);

        long[] r = repo.putOrMerge(tenant, project, "other title", body, "t", null, null, 30, 0.1);

        assertThat(r[1]).as("inserted, not merged").isZero();
        assertThat(r[0]).as("a NEW row").isNotEqualTo(hidden);
        MemoryRecord q = repo.listQuarantined(tenant, project).get(0);
        assertThat(q.getId()).isEqualTo(hidden);
        assertThat(q.getContent()).as("the quarantined row is untouched").isEqualTo(body);
        assertThat(repo.findById(tenant, hidden)).as("and still hidden").isEmpty();
    }
}
