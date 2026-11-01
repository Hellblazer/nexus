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
 * {@code service/src/main/java} that holds a collection (a {@code Map}, {@code Set}, {@code Queue}, {@code Deque},
 * {@code Cache}, {@code List} or {@code Collection}, or is built with {@code newKeySet}), is an {@code Atomic*}
 * holder, is a {@code volatile} reference (a hot-swapped or last-result slot), or whose NAME contains {@code tenant},
 * must be listed in one of two CHECKED tables below, with an evictor name (keyed) or a reason (not keyed). A field in
 * neither table fails; a table entry whose field no longer exists fails (a stale entry would keep claiming a cache is
 * evicted). A table says what was ACKNOWLEDGED, not what was cleared: the evictor behind each keyed entry has its own
 * test in {@code TenantKeyedEvictorsTest}.
 *
 * <p>The scan is lexical (comments and string literals blanked, braces matched), in the style of
 * {@link RawSqlGateTest}. It reads declared fields AND record components (a record holds its components as surely as
 * a class holds fields, and a {@code Map} component is as much a store as a {@code Map} field). A collection built by
 * {@code Map.of}/{@code Set.of} (and the other immutable factories) as a {@code static final} constant is exempt
 * because it cannot hold per-tenant state; those are counted, not silently skipped. {@code TypeReference} constants
 * and the {@code jooq} directory are excluded. Non-vacuity: the walk must reach 100 files and examine at least 15
 * fields, and the synthetic-source tests below prove the scanner finds what it claims to. Its blind spots, stated:
 * a holder typed as an interface or class with none of these words in its name, an array, and anything declared in
 * an anonymous class body.
 *
 * <p>Keys are {@code TopLevelClass[.Nested].field}. The live check that the route's response names exactly the
 * {@link #TENANT_KEYED} evictors is {@code TenantResetRouteTest}; this class also checks each evictor name is wired as
 * a literal in {@code NexusService.java}.
 *
 * <p>This gate proves acknowledgment, not clearance. The behavioural counterpart, which drives a workload, resets and
 * walks the live object graph for the tenant's name, is {@code TenantResetLeakCanaryTest} (nexus-vr8wk).
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

    /**
     * The classes that declare a field either table lists, as {@code TopLevel[.Nested]} names (the key minus its last
     * segment). Every field the census examined has an owner here, so a walk that reads these classes' statics
     * reads every static holder the census knows of (nexus-vr8wk: the behavioural leak canary).
     */
    public static Set<String> catalogedOwnerNames() {
        var owners = new TreeSet<String>();
        for (String key : TENANT_KEYED.keySet()) {
            owners.add(key.substring(0, key.lastIndexOf('.')));
        }
        for (String key : NOT_TENANT_KEYED.keySet()) {
            owners.add(key.substring(0, key.lastIndexOf('.')));
        }
        return owners;
    }

    // ── the scanner ──────────────────────────────────────────────────────────────────────────────

    record FieldDecl(String key, String type, String name, boolean isStatic, boolean immutableConstant,
                     boolean isVolatile) { }

    private static final Pattern MODIFIER = Pattern.compile(
        "\\b(public|private|protected|static|final|volatile|transient)\\b");
    private static final Pattern ANNOTATION = Pattern.compile("@\\w+(?:\\.\\w+)*(?:\\s*\\([^)]*\\))?");
    private static final Pattern TYPE_DECL = Pattern.compile("(?:^|\\s)(class|interface|enum|record)\\s+(\\w+)");
    /**
     * Types that can hold more than one tenant's state: a Map, Set, Queue, Deque, Cache, List or Collection (any
     * implementation: the prefix is free), and the Atomic holders ({@code AtomicReference<RunResult>} and
     * {@code AtomicReference<Map<..>>} both hide a payload from a collection-only match; a counter such as
     * {@code AtomicLong} is examined too and listed with its reason).
     */
    private static final Pattern COLLECTION_TYPE = Pattern.compile(
        "\\b[A-Za-z]*(Map|Set|Queue|Deque|Cache|List|Collection)\\b|\\bAtomic\\w+\\b");
    private static final Pattern PRIMITIVE_OR_STRING = Pattern.compile(
        "(boolean|byte|short|int|long|float|double|char|String|Boolean|Integer|Long|Double)");
    private static final Pattern RECORD_HEADER = Pattern.compile(
        "(?:^|\\s)record\\s+\\w+\\s*(?:<[^>]*>)?\\s*\\((.*)\\)[^()]*$", Pattern.DOTALL);
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
                    if (t.group(1).equals("record")) {
                        recordComponents(stripAnnotations(head), nested, examined, exempt, seen);
                    }
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

    /**
     * A record's components are its (private final) fields: {@code record Key(String tenant, Set<String> parts)}
     * holds a tenant id and a collection as surely as a class with two fields does. Each component is examined as a
     * field of the record, keyed {@code Outer.Rec.component}.
     */
    private static void recordComponents(String head, List<String> typePath, List<FieldDecl> examined,
                                         List<FieldDecl> exempt, int[] seen) {
        Matcher m = RECORD_HEADER.matcher(head);
        if (!m.find()) return;
        String list = m.group(1);
        int depth = 0;
        StringBuilder part = new StringBuilder();
        List<String> parts = new ArrayList<>();
        for (int i = 0; i < list.length(); i++) {
            char c = list.charAt(i);
            if (c == '<' || c == '(' || c == '[') depth++;
            if (c == '>' || c == ')' || c == ']') depth--;
            if (c == ',' && depth == 0) {
                parts.add(part.toString());
                part.setLength(0);
            } else {
                part.append(c);
            }
        }
        if (!part.toString().isBlank()) parts.add(part.toString());
        for (String component : parts) {
            field(component, typePath, false, examined, exempt, seen);
        }
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
        boolean isVolatile = Pattern.compile("\\bvolatile\\b").matcher(typeAndMods).find();
        // A volatile reference is a holder written by one thread and read by another, which is how a hot-swapped
        // cache or a last-result slot is kept; only a volatile primitive or String flag is not a holder.
        boolean volatileHolder = isVolatile && !PRIMITIVE_OR_STRING.matcher(type).matches();
        boolean collection = COLLECTION_TYPE.matcher(type).find() || init.contains("newKeySet") || volatileHolder;
        boolean tenantNamed = name.toLowerCase(java.util.Locale.ROOT).contains("tenant");
        if (!collection && !tenantNamed) return;
        String key = String.join(".", typePath) + "." + name;
        boolean immutable = isStatic && isFinal && IMMUTABLE_INIT.matcher(init).matches();
        FieldDecl decl = new FieldDecl(key, type, name, isStatic, immutable, isVolatile);
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
        assertThat(files).as("non-vacuity: the walk reached the main sources").isGreaterThanOrEqualTo(150);
        assertThat(fieldsSeen).as("non-vacuity: the scanner parsed field declarations").isGreaterThanOrEqualTo(1500);
        assertThat(examined.size()).as("non-vacuity: collection or tenant-named fields examined (%d immutable "
            + "constants exempt)", exempt.size()).isGreaterThanOrEqualTo(200);

        Set<String> found = new LinkedHashSet<>();
        Map<String, FieldDecl> declByKey = new TreeMap<>();
        List<String> duplicates = new ArrayList<>();
        for (FieldDecl d : examined) {
            if (!found.add(d.key())) duplicates.add(d.key());
            declByKey.put(d.key(), d);
        }
        assertThat(duplicates).as("two fields share a key: disambiguate the scanner's key").isEmpty();
        // The first-live-run numbers of the bead's review record, and a standing line in every run's output.
        System.out.printf("TenantKeyedStateGateTest: files=%d fieldsSeen=%d examined=%d exemptConstants=%d "
            + "keyed=%d notKeyed=%d%n", files, fieldsSeen, examined.size(), exempt.size(), TENANT_KEYED.size(),
            NOT_TENANT_KEYED.size());

        Verdict v = judge(found, TENANT_KEYED, NOT_TENANT_KEYED);
        assertThat(v.unlisted().stream().map(k -> k + "  ::  "
                + (declByKey.get(k).isVolatile() ? "volatile " : "") + declByKey.get(k).type()).toList())
            .as("UNLISTED collection, holder or tenant-named fields in src/main: add each to "
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
            private volatile Thread owner;
            private volatile boolean flag;
            private volatile long stamp;
            private final List<String> names = new ArrayList<>();
            private final java.util.concurrent.atomic.AtomicLong counter = new java.util.concurrent.atomic.AtomicLong();
            private volatile Runnable hook;
            record Key(String tenant, Set<String> components) { }
            record Plain(String name, int n) { }
            record Pair<A>(List<A> items, Map<String, List<A>> byName, String label) { }
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
            "Sample.Inner.queue", "Sample.Mode.perMode",
            // widened: Lists, Atomic* holders, volatile reference holders, and record components
            "Sample.owner", "Sample.names", "Sample.counter", "Sample.hook",
            "Sample.Key.tenant", "Sample.Key.components", "Sample.Pair.items", "Sample.Pair.byName");
        assertThat(keys).as("a volatile primitive flag and a plain record component are not holders")
            .doesNotContain("Sample.flag", "Sample.stamp", "Sample.Plain.name", "Sample.Plain.n",
                "Sample.Pair.label");
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
        private static final String LEAF_BLOAT = "leaf bloat view (nexus-dxsjc): the sweep's last read of "
            + "pg_stat_user_tables, replaced as a whole on every read, so a leaf that is gone (its name carries a hash "
            + "of the tenant), as after a tenant reset, drops out at the next read; the status route serves counts "
            + "only";
        private static final String RESULT = "a field of an immutable per-call value (result, parameter, outcome or "
            + "exception) built for one request or one pass and dropped with it; no long-lived structure holds it";
        private static final String COUNTER = "a process-wide counter or flag since boot, or a pass counter; not keyed "
            + "by tenant, and a data reset does not reset counters";
        private static final String TEST_HOOK = "a test-only hook, null in production";
        private static final String CONFIG = "process configuration set at boot or swapped as a whole; not keyed by "
            + "tenant";
        private static final String TEMPLATE = "tuple template schema, parsed at boot and immutable; "
            + "tenant-independent";
        private static final String ORT = "ONNX runtime init state, one per process; tenant-independent";
        private static final String JOOQ_COLUMN = "a jOOQ column reference to tenant_id, a constant naming the "
            + "column; it holds no state";
        private static final String PRINCIPAL = "an immutable value naming a tenant, carried on one request's or one "
            + "sweep step's stack; the long-lived holders of tenant principals are the keyed entries (TokenCache.map)";
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
            keyed.put("CatalogRepository.ghostSweepCheckedTenants", GHOST_SWEEP);
            keyed.put("MintRateLimiter.credentials", MINT_RATE);
            keyed.put("MintRateLimiter.CredentialState.tenants", MINT_RATE);
            keyed.put("OwnerlessLogLimiter.states", OWNERLESS_LOG);
            keyed.put("ChunkReaper.timeoutStreaks", REAPER);
            keyed.put("ChunkReaper.resumeTenant", REAPER);
            keyed.put("ChunkReaper.resumeCollection", REAPER);
            keyed.put("ChunkReaper.resumeExpiry", REAPER);
            keyed.put("NexusService.dataSweepFailuresByTenant", DATA_SWEEP);
            keyed.put("TupleWaitRegistry.WaitKey.tenant", TUPLE_WAITS);   // the key of groups
            keyed.put("TokenCache.Entry.tenantId", TOKEN_CACHE);   // the value of map

            // Not keyed: each with the reason it holds nothing a reset must clear.
            for (String f : List.of("NexusService.tenantScope", "TaxonomyCentroidRepository.tenantScope",
                "PgVectorRepository.tenantScope", "ReaperRepository.tenantScope", "WhoamiHandler.tenantScope",
                "RemapRepository.tenantScope", "CombinedWriteService.tenantScope", "TaxonomyRepository.tenantScope",
                "TupleRepository.tenantScope", "TelemetryRepository.tenantScope", "LadderRepository.tenantScope",
                "PlanRepository.tenantScope", "MemoryRepository.tenantScope", "PipelineRepository.tenantScope",
                "CatalogRepository.tenantScope", "ChashRepository.tenantScope", "AspectRepository.tenantScope",
                "ScratchRepository.tenantScope", "ManifestLabelStage.tenantScope")) {
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
            notKeyed.put("CollectionRegistry.afterMissReadForTests", "a test-only hook run between a cache "
                + "miss's read and its fill (nexus-hkh1y); null in production, holds no state");
            notKeyed.put("ModelPartitions.KNOWN", "parent-and-model partition pairs already created; keyed by "
                + "model, not by tenant");
            notKeyed.put("TenantScope.ADMISSION", "admission semaphore per DataSource identity; tenant-independent");
            notKeyed.put("TenantScope.FANOUT_ARMS", "fan-out arm semaphore per DataSource identity; "
                + "tenant-independent");
            notKeyed.put("HttpUtil.BEFORE_SEND", "send hook per in-flight HttpExchange, removed when the response "
                + "is sent; request-scoped");
            notKeyed.put("PgVectorRepository.FanoutMerger.worstOnTop", "the merge heap of one fan-out search; "
                + "dies with the request");
            notKeyed.put("PgVectorRepository.LexicalLeg.rows", "the lexical arms' rows of one fan-out search "
                + "(nexus-xd2rc); dies with the request");
            notKeyed.put("PgVectorRepository.LexicalLeg.Union.extras", "the lexical rows one fan-out search "
                + "returns beside its vector rows; dies with the request");
            notKeyed.put("PgVectorRepository.LexicalLeg.Union.stats", "per-collection lexical stats of one "
                + "fan-out search; dies with the request");
            notKeyed.put("TupleWaitRegistry.MultiWaiter.pendingSubspaces", "wake bookkeeping of one parked wait; "
                + "dies when that request ends");
            notKeyed.put("TupleWaitRegistry.MultiWaiter.tenant", "the tenant of one parked wait, immutable; "
                + "dies when that request ends");
            notKeyed.put("TenantCreationBusyException.tenant", "payload of an exception thrown to one caller");
            notKeyed.put("UnregisteredCollectionException.tenant", "payload of an exception thrown to one caller");
            notKeyed.put("CollectionSupersededException.tenant", "payload of an exception thrown to one caller (nexus-mp8ys)");
            notKeyed.put("ReembedRunner.tenantSource", "a supplier of the tenant list, re-read from service_tokens at "
                + "the start of every runner pass; the runner keeps nothing per tenant between passes");
            notKeyed.put("ReembedHandler.servedModels", "a supplier of the models the embedder router serves, an "
                + "install-wide fact read on each plan and start (the list GET /version reports); it keeps no copy");
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
            notKeyed.put("NexusService.TENANT_RESET_ROUTE_ENV", CONSTANT);
            notKeyed.put("NexusService.TENANT_RESET_TUPLE_BOUND", CONSTANT);
            // CONFIG (15)
            for (String f : List.of(
                "Bge768Embedder.admissionGate",
                "ChunkReaper.Settings.floorExemptCollections",
                "HttpUtil.gzipper",
                "NexusService.chunksLeafBloat",
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
            // COUNTER (42)
            for (String f : List.of(
                "Bge768Embedder.onnxInvocationCount",
                "CatalogRepository.vacuumInProgress",
                "CceEmbedder.callEwmaNanos",
                "CceEmbedder.waitingBatches",
                "NexusService.consecutiveDataSweepFailures",
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
                "PgVectorRepository.ROUTED_EXACT",
                "PgVectorRepository.ROUTED_HNSW",
                "PgVectorRepository.SHORT_PAGE_RERUNS",
                "PgVectorRepository.existenceSelectCalls",
                "PgVectorRepository.ownerLookupCalls",
                "RacedEmbedActivity.RACED_EMBEDS_TOTAL",
                "RequestLedgerActivity.IN_FLIGHT",
                "RequestLedgerActivity.KEY_REUSED",
                "RequestLedgerActivity.REPLAYS",
                "RequestLedgerActivity.TAKEOVERS",
                "SuppliedVectorMismatchActivity.MISMATCHES_TOTAL",
                "TupleWaitRegistry.claimantRefused",
                "TupleWaitRegistry.globalParked",
                "TupleWaitRegistry.globalRefused",
                "VoyageEmbedder.voyageRequestCount"
            )) {
                notKeyed.put(f, COUNTER);
            }
            // TEST_HOOK (21)
            for (String f : List.of(
                "CatalogRepository.afterChunkUpsertHookForTests",
                "CatalogRepository.afterMoveCompletedHookForTests",
                "CatalogRepository.afterMovePreconditionHookForTests",
                "CatalogRepository.beforeReadHookForTests",
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
            // PRINCIPAL (7)
            for (String f : List.of(
                "NexusService.TupleTenantCursor.tenantId",
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
            // TEMPLATE (13)
            for (String f : List.of(
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
            notKeyed.put("PciReconciler.tenantBinder", PCI_OFF);
            notKeyed.put("EngineSingletonLock.connection", SINGLETON_LOCK);
            notKeyed.put("EngineSingletonLock.onRegained", SINGLETON_LOCK);
            notKeyed.put("EngineSingletonLock.onFatal", SINGLETON_LOCK);
            notKeyed.put("EngineSingletonLock.staleTerminationWait", SINGLETON_LOCK);
            notKeyed.put("PciPlanCheck.lastSampled", PCI_OFF);
            notKeyed.put("PciCatalog.Snapshot.valid", PCI_OFF);
            notKeyed.put("PciCatalog.LeafBuilder.tenant", PCI_OFF);
            notKeyed.put("PciIndexSet.NONE", PCI_OFF);
            notKeyed.put("PgVectorRepository.pciIndexes", PCI_OFF);
        }
    }
}
