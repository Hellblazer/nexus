// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * Completeness gate for the test-only tenant reset route (bead nexus-0bl77, rtg20 phase A; design T2
 * {@code nexus/design-nexus-rtg20-data-reset-pool-2026-10-18}). {@code POST /v1/_test/tenant-reset} must evict every
 * JVM structure that is keyed by tenant. A new cache added later without an evictor would survive a data reset and
 * serve the next test a dead tenant's state, so this gate makes the question unavoidable: every field in
 * {@code service/src/main/java} that holds a collection (a {@code Map}, {@code Set}, {@code Queue}, {@code Deque} or
 * {@code Cache}, or is built with {@code newKeySet}) or whose NAME contains {@code tenant}, must be listed in one of
 * two CHECKED tables below, with an evictor name (keyed) or a reason (not keyed). A field in neither table fails; a
 * table entry whose field no longer exists fails (a stale entry would keep claiming a cache is evicted).
 *
 * <p>The scan is lexical (comments and string literals blanked, braces matched), in the style of
 * {@link RawSqlGateTest}. It reads declared fields only: record components are not scanned (a record's
 * {@code tenant} component is data, not a store), and a collection built by {@code Map.of}/{@code Set.of} (and the
 * other immutable factories) as a {@code static final} constant is exempt because it cannot hold per-tenant state;
 * those are counted, not silently skipped. {@code TypeReference} constants and the {@code jooq} directory are
 * excluded. Non-vacuity: the walk must reach 100 files and examine at least 15 fields, and the synthetic-source tests
 * below prove the scanner finds what it claims to.
 *
 * <p>Keys are {@code TopLevelClass[.Nested].field}. The live check that the route's response names exactly the
 * {@link #TENANT_KEYED} evictors is {@code TenantResetRouteTest}; this class also checks each evictor name is wired as
 * a literal in {@code NexusService.java}.
 */
public class TenantKeyedStateGateTest {

    private static final Path MAIN = Path.of("src", "main", "java");

    // ── the checked tables ───────────────────────────────────────────────────────────────────────

    /** Field to the name of the evictor that clears it for a tenant ({@code NexusService.tenantKeyedState()}). */
    static final Map<String, String> TENANT_KEYED = new TreeMap<>();
    /** Field to the reason it holds no per-tenant state a reset must clear. */
    static final Map<String, String> NOT_TENANT_KEYED = new TreeMap<>();

    static {
        Tables.fill(TENANT_KEYED, NOT_TENANT_KEYED);
    }

    /** The distinct evictor names the table lists: what the route's response must name, no more and no fewer. */
    public static Set<String> evictorNames() {
        return new TreeSet<>(TENANT_KEYED.values());
    }

    // ── the scanner ──────────────────────────────────────────────────────────────────────────────

    record FieldDecl(String key, String type, String name, boolean isStatic, boolean immutableConstant) { }

    private static final Pattern MODIFIER = Pattern.compile(
        "\\b(public|private|protected|static|final|volatile|transient)\\b");
    private static final Pattern ANNOTATION = Pattern.compile("@\\w+(?:\\.\\w+)*(?:\\s*\\([^)]*\\))?");
    private static final Pattern TYPE_DECL = Pattern.compile("(?:^|\\s)(class|interface|enum|record)\\s+(\\w+)");
    private static final Pattern COLLECTION_TYPE = Pattern.compile("\\b[A-Za-z]*(Map|Set|Queue|Deque|Cache)\\b");
    private static final Pattern IMMUTABLE_INIT = Pattern.compile(
        "^\\s*(?:java\\.util\\.)?(?:Map\\.(?:of|ofEntries|copyOf)|Set\\.(?:of|copyOf)|List\\.(?:of|copyOf)"
            + "|Collections\\.(?:unmodifiable\\w+|empty\\w+|singleton\\w*))\\s*\\(.*", Pattern.DOTALL);

    /** Every declared field of {@code source} that this gate examines, plus the count of fields seen at all. */
    record Scan(List<FieldDecl> examined, List<FieldDecl> exemptConstants, int fieldsSeen) { }

    static Scan scan(String fileName, String source) {
        String s = RawSqlGateTest.blank(source);
        List<FieldDecl> examined = new ArrayList<>();
        List<FieldDecl> exempt = new ArrayList<>();
        int[] seen = {0};
        members(s, 0, new ArrayList<>(), true, examined, exempt, seen);
        return new Scan(examined, exempt, seen[0]);
    }

    /**
     * Walks the member statements of one type body (or, at {@code topLevel}, the file) starting at {@code pos}; returns
     * the index just past the closing brace of the body (or the end of the file).
     */
    private static int members(String s, int pos, List<String> typePath, boolean topLevel,
                               List<FieldDecl> examined, List<FieldDecl> exempt, int[] seen) {
        int n = s.length();
        int i = pos;
        StringBuilder stmt = new StringBuilder();
        int paren = 0;
        while (i < n) {
            char c = s.charAt(i);
            if (c == '(' || c == '[') paren++;
            if (c == ')' || c == ']') paren--;
            if (paren == 0 && c == ';') {
                field(stmt.toString(), typePath, topLevel, examined, exempt, seen);
                stmt.setLength(0);
                i++;
                continue;
            }
            if (paren == 0 && c == '{') {
                String head = stmt.toString();
                Matcher t = TYPE_DECL.matcher(stripAnnotations(head));
                boolean isType = t.find() && !stripAnnotations(head).contains("=");
                if (isType) {
                    List<String> nested = new ArrayList<>(typePath);
                    nested.add(t.group(2));
                    i = members(s, i + 1, nested, false, examined, exempt, seen);
                    stmt.setLength(0);
                    continue;
                }
                int end = matchingBrace(s, i);
                if (stripAnnotations(head).contains("=")) {
                    // A field initializer (lambda, array, anonymous class): the statement runs on to its ';'.
                    stmt.append("{}");
                    i = end + 1;
                    continue;
                }
                // A method, constructor or initializer block: no field in it is a member.
                stmt.setLength(0);
                i = end + 1;
                continue;
            }
            if (paren == 0 && c == '}') {
                return i + 1;
            }
            stmt.append(c);
            i++;
        }
        return n;
    }

    private static int matchingBrace(String s, int open) {
        int depth = 0;
        for (int i = open; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '{') depth++;
            if (c == '}' && --depth == 0) return i;
        }
        return s.length() - 1;
    }

    private static String stripAnnotations(String head) {
        return ANNOTATION.matcher(head).replaceAll(" ");
    }

    private static void field(String statement, List<String> typePath, boolean topLevel,
                              List<FieldDecl> examined, List<FieldDecl> exempt, int[] seen) {
        if (topLevel) return;   // package, import and annotation statements; fields live in type bodies
        String stmt = stripAnnotations(statement).strip();
        if (stmt.isEmpty()) return;
        int eq = topLevelEquals(stmt);
        String head = (eq < 0 ? stmt : stmt.substring(0, eq)).strip();
        String init = eq < 0 ? "" : stmt.substring(eq + 1);
        if (head.contains("(") || head.contains(",") && !head.contains("<")) return;   // a method, or enum constants
        Matcher nameMatch = Pattern.compile("(\\w+)\\s*(?:\\[\\s*\\])*$").matcher(head);
        if (!nameMatch.find()) return;
        String name = nameMatch.group(1);
        String typeAndMods = head.substring(0, nameMatch.start()).strip();
        if (typeAndMods.isEmpty()) return;   // an enum constant list or a bare identifier
        boolean isStatic = Pattern.compile("\\bstatic\\b").matcher(typeAndMods).find();
        boolean isFinal = Pattern.compile("\\bfinal\\b").matcher(typeAndMods).find();
        String type = MODIFIER.matcher(typeAndMods).replaceAll(" ").replaceAll("\\s+", " ").strip();
        if (type.isEmpty()) return;
        seen[0]++;
        if (type.startsWith("TypeReference") || type.contains(".TypeReference")) return;
        boolean collection = COLLECTION_TYPE.matcher(type).find() || init.contains("newKeySet");
        boolean tenantNamed = name.toLowerCase(java.util.Locale.ROOT).contains("tenant");
        if (!collection && !tenantNamed) return;
        String key = String.join(".", typePath) + "." + name;
        boolean immutable = isStatic && isFinal && IMMUTABLE_INIT.matcher(init).matches();
        FieldDecl decl = new FieldDecl(key, type, name, isStatic, immutable);
        (immutable ? exempt : examined).add(decl);
    }

    private static int topLevelEquals(String stmt) {
        int depth = 0;
        for (int i = 0; i < stmt.length(); i++) {
            char c = stmt.charAt(i);
            if (c == '(' || c == '<' || c == '[' || c == '{') depth++;
            if (c == ')' || c == '>' || c == ']' || c == '}') depth--;
            if (c == '=' && depth <= 0) {
                char prev = i > 0 ? stmt.charAt(i - 1) : ' ';
                char next = i + 1 < stmt.length() ? stmt.charAt(i + 1) : ' ';
                if (prev != '=' && prev != '!' && prev != '<' && prev != '>' && next != '=') return i;
            }
        }
        return -1;
    }

    /** Result of comparing the scan with the two tables. */
    record Verdict(List<String> unlisted, List<String> stale, List<String> both, List<String> blankReasons) {
        boolean clean() {
            return unlisted.isEmpty() && stale.isEmpty() && both.isEmpty() && blankReasons.isEmpty();
        }
    }

    static Verdict judge(Set<String> found, Map<String, String> keyed, Map<String, String> notKeyed) {
        List<String> unlisted = new ArrayList<>();
        List<String> stale = new ArrayList<>();
        List<String> both = new ArrayList<>();
        List<String> blank = new ArrayList<>();
        for (String k : found) {
            if (!keyed.containsKey(k) && !notKeyed.containsKey(k)) unlisted.add(k);
        }
        Set<String> listed = new TreeSet<>(keyed.keySet());
        listed.addAll(notKeyed.keySet());
        for (String k : listed) {
            if (!found.contains(k)) stale.add(k);
            if (keyed.containsKey(k) && notKeyed.containsKey(k)) both.add(k);
        }
        keyed.forEach((k, v) -> { if (v == null || v.isBlank()) blank.add(k + " (no evictor name)"); });
        notKeyed.forEach((k, v) -> { if (v == null || v.strip().length() < 15) blank.add(k + " (reason too short)"); });
        return new Verdict(unlisted, stale, both, blank);
    }

    // ── the gate ─────────────────────────────────────────────────────────────────────────────────

    @Test
    void everyCollectionOrTenantNamedFieldInMainSourcesIsListedAndEveryEntryStillExists() throws IOException {
        assertThat(MAIN).as("the gate runs from the service module directory").exists();
        List<FieldDecl> examined = new ArrayList<>();
        List<FieldDecl> exempt = new ArrayList<>();
        int files = 0;
        int fieldsSeen = 0;
        try (Stream<Path> walk = Files.walk(MAIN)) {
            for (Path p : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                if (p.toString().contains("/jooq/")) continue;
                Scan scan = scan(p.getFileName().toString(), Files.readString(p));
                examined.addAll(scan.examined());
                exempt.addAll(scan.exemptConstants());
                fieldsSeen += scan.fieldsSeen();
                files++;
            }
        }
        assertThat(files).as("non-vacuity: the walk reached the main sources").isGreaterThanOrEqualTo(100);
        assertThat(fieldsSeen).as("non-vacuity: the scanner parsed field declarations").isGreaterThanOrEqualTo(500);
        assertThat(examined.size()).as("non-vacuity: collection or tenant-named fields examined (%d immutable "
            + "constants exempt)", exempt.size()).isGreaterThanOrEqualTo(15);

        Set<String> found = new LinkedHashSet<>();
        List<String> duplicates = new ArrayList<>();
        for (FieldDecl d : examined) {
            if (!found.add(d.key())) duplicates.add(d.key());
        }
        assertThat(duplicates).as("two fields share a key: disambiguate the scanner's key").isEmpty();

        Verdict v = judge(found, TENANT_KEYED, NOT_TENANT_KEYED);
        assertThat(v.unlisted()).as("UNLISTED collection or tenant-named fields in src/main: add each to "
            + "TenantKeyedStateGateTest.Tables as keyed (with the NexusService evictor that clears it for a "
            + "tenant) or not keyed (with the reason it holds no per-tenant state)").isEmpty();
        assertThat(v.stale()).as("STALE entries: the field no longer exists or was renamed; remove or fix the entry")
            .isEmpty();
        assertThat(v.both()).as("a field is in both tables").isEmpty();
        assertThat(v.blankReasons()).as("every keyed entry names an evictor and every not-keyed entry a reason")
            .isEmpty();
    }

    @Test
    void everyEvictorNamedInTheTableIsWiredAsALiteralInNexusService() throws IOException {
        String service = Files.readString(MAIN.resolve("dev/nexus/service/NexusService.java"));
        Set<String> evictors = new TreeSet<>(TENANT_KEYED.values());
        assertThat(evictors).as("non-vacuity: the table names evictors").hasSizeGreaterThanOrEqualTo(8);
        List<String> missing = new ArrayList<>();
        for (String name : evictors) {
            if (!service.contains("\"" + name + "\"")) missing.add(name);
        }
        assertThat(missing).as("evictors named in the table but not registered in NexusService.tenantKeyedState()")
            .isEmpty();
    }

    // ── the scanner's own tests: it finds what it claims to, and the gate fails when it should ───────

    private static final String SAMPLE = """
        package p;
        import java.util.*;
        final class Sample {
            private static final Map<String, String> CONSTANT = Map.of("a", "b");
            private static final Set<String> MUTABLE_CONSTANT = new HashSet<>(Set.of("a"));
            static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};
            private final Map<String, Integer> byTenant = new ConcurrentHashMap<>();
            private final Set<String> checked =
                java.util.concurrent.ConcurrentHashMap.newKeySet();
            private volatile String resumeTenant;
            private final String unrelated = "tenant in a string";
            private final java.util.concurrent.atomic.AtomicReference<Map<String, String>> held = null;
            private final Runnable task = () -> { Map<String, String> inLambda = new HashMap<>(); };
            Sample(Map<String, String> ctorArg) { Set<String> local = new HashSet<>(); }
            void method() { Map<String, String> localMap = new HashMap<>(); }
            record Key(String tenant, Set<String> components) { }
            static final class Inner {
                final Queue<String> queue = new ArrayDeque<>();
                // private Map<String,String> commented = null;
            }
            enum Mode { A, B; private final Map<String, String> perMode = new HashMap<>(); }
        }
        """;

    @Test
    void theScannerFindsCollectionsAndTenantNamedFieldsAndNothingInsideMethods() {
        Scan scan = scan("Sample.java", SAMPLE);
        Set<String> keys = new TreeSet<>();
        scan.examined().forEach(d -> keys.add(d.key()));
        assertThat(keys).containsExactlyInAnyOrder(
            "Sample.MUTABLE_CONSTANT", "Sample.byTenant", "Sample.checked", "Sample.resumeTenant", "Sample.held",
            "Sample.Inner.queue", "Sample.Mode.perMode");
        assertThat(scan.exemptConstants()).extracting(FieldDecl::key).containsExactly("Sample.CONSTANT");
        assertThat(scan.fieldsSeen()).as("fields seen include the ones filtered out").isGreaterThanOrEqualTo(10);
    }

    @Test
    void aFieldInNeitherTableFailsAndAStaleEntryFails() {
        Set<String> found = new TreeSet<>(Set.of("A.cache", "A.other"));
        Verdict unlisted = judge(found, new TreeMap<>(Map.of("A.cache", "Evictor")),
            new TreeMap<>());
        assertThat(unlisted.unlisted()).containsExactly("A.other");
        assertThat(unlisted.clean()).isFalse();

        Verdict stale = judge(found, new TreeMap<>(Map.of("A.cache", "Evictor", "A.gone", "Evictor")),
            new TreeMap<>(Map.of("A.other", "holds no per-tenant state at all")));
        assertThat(stale.stale()).containsExactly("A.gone");
        assertThat(stale.unlisted()).isEmpty();

        Verdict clean = judge(found, new TreeMap<>(Map.of("A.cache", "Evictor")),
            new TreeMap<>(Map.of("A.other", "holds no per-tenant state at all")));
        assertThat(clean.clean()).isTrue();

        Verdict shortReason = judge(found, new TreeMap<>(Map.of("A.cache", "Evictor")),
            new TreeMap<>(Map.of("A.other", "n/a")));
        assertThat(shortReason.blankReasons()).hasSize(1);
        Verdict dup = judge(found, new TreeMap<>(Map.of("A.cache", "Evictor", "A.other", "Evictor")),
            new TreeMap<>(Map.of("A.other", "holds no per-tenant state at all")));
        assertThat(dup.both()).containsExactly("A.other");
    }

    /** The two tables, as data. Evictor names are the ones {@code NexusService.tenantKeyedState()} registers. */
    static final class Tables {
        private Tables() { }

        private static final String TUPLE_WAITS = "TupleWaitRegistry";
        private static final String TOKEN_CACHE = "TokenCache";
        private static final String COLLECTIONS = "CollectionRegistry";
        private static final String GHOST_SWEEP = "CatalogRepository.ghostSweepCheckedTenants";
        private static final String MINT_RATE = "MintRateLimiter";
        private static final String OWNERLESS_LOG = "OwnerlessLogLimiter";
        private static final String REAPER = "ChunkReaper";
        private static final String DATA_SWEEP = "NexusService.dataSweepFailuresByTenant";

        private static final String HANDLE =
            "a TenantScope handle: it holds the DataSource wrapper only, the tenant is a per-call argument";
        private static final String CONSTANT = "a constant naming something (a GUC, header, env variable, scope or "
            + "default tenant); it holds no state";
        private static final String PCI_OFF = "PCI index state: populated only when NX_SEARCH_PCI=1, and the tenant "
            + "reset route answers 503 in that mode, so it is never carried across a reset";
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
            keyed.put("CatalogRepository.ghostSweepCheckedTenants", GHOST_SWEEP);
            keyed.put("MintRateLimiter.credentials", MINT_RATE);
            keyed.put("MintRateLimiter.CredentialState.tenants", MINT_RATE);
            keyed.put("OwnerlessLogLimiter.states", OWNERLESS_LOG);
            keyed.put("ChunkReaper.timeoutStreaks", REAPER);
            keyed.put("ChunkReaper.resumeTenant", REAPER);
            keyed.put("ChunkReaper.resumeCollection", REAPER);
            keyed.put("ChunkReaper.resumeExpiry", REAPER);
            keyed.put("NexusService.dataSweepFailuresByTenant", DATA_SWEEP);

            // Not keyed: each with the reason it holds nothing a reset must clear.
            for (String f : List.of("NexusService.tenantScope", "TaxonomyCentroidRepository.tenantScope",
                "PgVectorRepository.tenantScope", "ReaperRepository.tenantScope", "WhoamiHandler.tenantScope",
                "RemapRepository.tenantScope", "CombinedWriteService.tenantScope", "TaxonomyRepository.tenantScope",
                "TupleRepository.tenantScope", "TelemetryRepository.tenantScope", "LadderRepository.tenantScope",
                "PlanRepository.tenantScope", "MemoryRepository.tenantScope", "PipelineRepository.tenantScope",
                "CatalogRepository.tenantScope", "ChashRepository.tenantScope", "AspectRepository.tenantScope",
                "ScratchRepository.tenantScope")) {
                notKeyed.put(f, HANDLE);
            }
            for (String f : List.of("NexusService.TUPLE_SWEEP_MAX_BATCHES_PER_TENANT_ENV",
                "NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT", "NexusService.DEFAULT_TENANT",
                "HttpUtil.TENANT_PARTITION_MISSING_REASON", "HttpUtil.TENANT_CREATION_BUSY_REASON",
                "HttpUtil.TENANT_CREATION_RETRY_AFTER_SECONDS", "AuthFilter.BOOTSTRAP_ANY_TENANT",
                "AuthFilter.TENANT_HEADER", "TenantConstants.DEFAULT_TENANT", "TenantConstants.BOOTSTRAP_ANY_TENANT",
                "ChunksIsolationCheck.TENANT_POLICY", "TokenStore.SCOPE_TENANT", "TenantScope.DEFAULT_TENANT_GUC",
                "TenantScope.T1_TENANT_GUC", "ScratchRepository.T1_TENANT_GUC", "PciReconciler.SET_LOCAL_TENANT",
                "MintRateLimiter.MAX_TENANT_BUCKETS_PER_CREDENTIAL", "MintRateLimiter.tenantBurst",
                "MintRateLimiter.tenantSustainedPerMinute")) {
                notKeyed.put(f, CONSTANT);
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
            notKeyed.put("ModelPartitions.KNOWN", "parent-and-model partition pairs already created; keyed by "
                + "model, not by tenant");
            notKeyed.put("TenantScope.ADMISSION", "admission semaphore per DataSource identity; tenant-independent");
            notKeyed.put("TenantScope.FANOUT_ARMS", "fan-out arm semaphore per DataSource identity; "
                + "tenant-independent");
            notKeyed.put("HttpUtil.BEFORE_SEND", "send hook per in-flight HttpExchange, removed when the response "
                + "is sent; request-scoped");
            notKeyed.put("PgVectorRepository.FanoutMerger.worstOnTop", "the merge heap of one fan-out search; "
                + "dies with the request");
            notKeyed.put("TupleWaitRegistry.MultiWaiter.pendingSubspaces", "wake bookkeeping of one parked wait; "
                + "dies when that request ends");
            notKeyed.put("TupleWaitRegistry.MultiWaiter.tenant", "the tenant of one parked wait, immutable; "
                + "dies when that request ends");
            notKeyed.put("TenantCreationBusyException.tenant", "payload of an exception thrown to one caller");
            notKeyed.put("UnregisteredCollectionException.tenant", "payload of an exception thrown to one caller");
            notKeyed.put("ChunkReaper.tenants", "a supplier of the tenant list, re-read from service_tokens on "
                + "every pass; the reaper keeps no copy");
            notKeyed.put("Rdr192BackfillGate.emptyTenant", "a predicate that reads PG on each call; it caches nothing");
            notKeyed.put("StatusHandler.chunksTenantIsolationIntact", "a supplier of one process-wide policy check; "
                + "not keyed by tenant");
            notKeyed.put("TokenStore.tenantCreationBound", "attempt and timeout bounds for a token insert; "
                + "configuration, immutable");
            notKeyed.put("SchemaMigrator.CHASH_LEN_CONSTRAINTS", "constraint names the schema migrator repairs; "
                + "filled once, tenant-independent");
            notKeyed.put("NexusService.tenantKeyedState", "the evictor list itself: an immutable List.copyOf built "
                + "once at construction from stateless lambdas");
            notKeyed.put("VersionHandler.testTenantResetRoute", "a boolean reported on /version: whether the "
                + "reset route is registered; set once at construction");
            notKeyed.put("NexusService.TENANT_RESET_ROUTE_ENV", CONSTANT);
            notKeyed.put("NexusService.TENANT_RESET_TUPLE_BOUND", CONSTANT);
            notKeyed.put("PciReconciler.failures", PCI_OFF);
            notKeyed.put("PciReconciler.tenantBinder", PCI_OFF);
            notKeyed.put("PciPlanCheck.lastSampled", PCI_OFF);
            notKeyed.put("PciCatalog.Snapshot.valid", PCI_OFF);
            notKeyed.put("PciCatalog.LeafBuilder.tenant", PCI_OFF);
            notKeyed.put("PciIndexSet.NONE", PCI_OFF);
            notKeyed.put("PgVectorRepository.pciIndexes", PCI_OFF);
        }
    }
}
