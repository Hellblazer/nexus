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
        TenantKeyedStateTables.fill(TENANT_KEYED, NOT_TENANT_KEYED);
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
}
