// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * RDR-210 Technical Design 7 (nexus-qfy01): the source half of the egress guard.
 *
 * <p>The intent: the local engine connects only to the Voyage API ({@code api.voyageai.com}) from the three
 * Voyage classes, and only when keyed, and to its database at {@code NX_DB_URL} over JDBC. This scan checks
 * part of that by text over the main sources. Comments are stripped (string literals are kept, because the
 * Voyage hosts live in them). Then every outbound construct in {@link #CONSTRUCTS} must sit in a file on
 * {@link #ALLOWLIST}, with that construct named on the entry and a reason beside it. It reads where the two
 * Hikari pools' URLs come from (see "JDBC pools"), and nothing else about the database URL; see "Not checked".
 *
 * <p>What it checks: the constructs in {@link #CONSTRUCTS}, including static imports of a listed member,
 * datagram sockets, DNS lookups, URL objects, socket factories, a Runtime held in a variable, a
 * ProxySelector and an http(s) string literal (nexus-3va7p). Each allowlist entry pins an exact site count
 * for its file and, for the live entries, an exact count per construct, so a second site fails, a construct
 * swapped for another at the same total fails, and a file whose count falls short is stale and fails.
 * Non-vacuity: {@link #scan} reports the sites it found, and the caller asserts the count is at least the
 * number the allowlist names.
 *
 * <p>JDBC pools (nexus-4lqi7): {@code new HikariConfig}, {@code setJdbcUrl} and {@code new HikariDataSource} are
 * constructs like the rest, pinned to exact counts (Main builds two pools). {@link #jdbcUrlProblems} then reads
 * each {@code setJdbcUrl} argument inside the method that makes the call: a plain variable, declared once there as
 * a local {@code String} and never reassigned, whose value is the {@code NX_DB_URL} lookup
 * ({@code requireEnv} or {@code System.getenv}) or {@code url()} on an {@code AdminConnection} that is a
 * method parameter or a local built by {@code AdminConnection.resolve(}, itself never reassigned. This catches a new
 * pool, a pool handed a literal or another source, and a URL variable that is reassigned, shadowed or passed in
 * under the same name. The rule is textual and name-based: it reads one method, not data flow.
 *
 * <p>Not checked, by this class or by any other test in the repository:
 * <ul>
 *   <li>the VALUE of {@code NX_DB_URL} or {@code NX_DB_ADMIN_URL}: an operator who puts a remote host in the
 *       environment gets a pool to it, and nothing here objects;</li>
 *   <li>the inputs to {@code AdminConnection}: the {@code resolve} call and its {@code defaultUrl} argument, and
 *       who calls a method that takes an {@code AdminConnection} parameter (the rule trusts the parameter);</li>
 *   <li>the URL sources at the {@code DriverManager.getConnection} sites (PciBuilderSession, AspectQueueDrain's
 *       {@code admin.url()}, EngineSingletonLock's {@code jdbcUrl} parameter, BackendReaper, AdminDdlSession):
 *       they are counted and pinned, and their arguments are not read;</li>
 *   <li>the Hikari forms the URL rule does not read: {@code new HikariConfig(Properties)} or
 *       {@code (String)}, {@code addDataSourceProperty}, {@code setDataSourceClassName}, {@code setDataSource},
 *       a {@code cfg::setJdbcUrl} method reference (the first is counted as a pool site; none is read for its URL);</li>
 *   <li>the 15 {@code .getConnection()} call sites (11 files) on those pools and any DataSource built another
 *       way (one handed in, or a driver-specific class);</li>
 *   <li>any dependency's code, such as {@code java.net.http}, raw sockets or DNS used inside a library;</li>
 *   <li>the GraalVM native image the engine ships as (the witnesses run on a JVM);</li>
 *   <li>DJL's Rust tokenizers library, whose network I/O is JNI that no Java-level check sees;</li>
 *   <li>an outbound call spelled through reflection or reached by a type this list does not name.</li>
 * </ul>
 * {@code EgressRuntimeWitnessTest} and {@code EgressVoyageClientWitnessTest} are separate, narrower runtime
 * checks and do not close these.
 *
 * <p>The scan takes its roots as arguments so a test can run it against a planted tree.
 */
final class EgressGuardCensus {

    /**
     * One allowlist entry: the constructs the file may use, the exact number of sites it shows, and why.
     * {@code perConstruct}, when not empty, pins the exact count of each construct too, so swapping one
     * construct for another at the same total fails (nexus-3va7p).
     */
    record Allow(Set<String> constructs, int sites, String reason, Map<String, Integer> perConstruct) {

        Allow(Set<String> constructs, int sites, String reason) {
            this(constructs, sites, reason, Map.of());
        }

        /** An entry that pins each construct's exact count; the construct set and the total follow from it. */
        static Allow pinned(Map<String, Integer> perConstruct, String reason) {
            return new Allow(Set.copyOf(perConstruct.keySet()),
                perConstruct.values().stream().mapToInt(Integer::intValue).sum(), reason, perConstruct);
        }
    }

    /** The outbound constructs, by name, with the pattern that finds each one in comment-stripped source. */
    static final Map<String, Pattern> CONSTRUCTS = construct();

    private static Map<String, Pattern> construct() {
        Map<String, Pattern> m = new LinkedHashMap<>();
        m.put("HttpClient.newBuilder", Pattern.compile("HttpClient\\s*\\.\\s*newBuilder\\b"));
        m.put("HttpClient.newHttpClient", Pattern.compile("HttpClient\\s*\\.\\s*newHttpClient\\b"));
        m.put("openConnection(", Pattern.compile("\\bopenConnection\\s*\\("));
        m.put("openStream(", Pattern.compile("\\bopenStream\\s*\\("));
        m.put("new Socket(", Pattern.compile("\\bnew\\s+(?:java\\s*\\.\\s*net\\s*\\.\\s*)?Socket\\s*\\("));
        m.put("ServerSocket", Pattern.compile("ServerSocket\\b"));
        m.put("SocketChannel.open", Pattern.compile("SocketChannel\\s*\\.\\s*open\\b"));
        m.put("DriverManager.getConnection", Pattern.compile("DriverManager\\s*\\.\\s*getConnection\\b"));
        m.put("ProcessBuilder", Pattern.compile("\\bProcessBuilder\\b"));
        m.put("Runtime.getRuntime().exec",
            Pattern.compile("Runtime\\s*\\.\\s*getRuntime\\s*\\(\\s*\\)\\s*\\.\\s*exec\\b"));
        // nexus-3va7p: the gaps the first review named (RDR-210 research 10). Each has a positive control in
        // EgressGuardCensusTest, and that test fails if a construct is added here without one.
        m.put("DatagramSocket/DatagramChannel",
            Pattern.compile("\\b(?:DatagramSocket|DatagramChannel|MulticastSocket|DatagramSocketImpl)\\b"));
        m.put("InetAddress.getBy*",
            Pattern.compile("\\bInetAddress\\s*\\.\\s*(?:getBy\\w+|getAllByName)\\b"));
        m.put("new URL(", Pattern.compile("\\bnew\\s+(?:java\\s*\\.\\s*net\\s*\\.\\s*)?URL\\s*\\("));
        m.put("toURL(", Pattern.compile("\\.\\s*toURL\\s*\\(\\s*\\)"));
        m.put("URL.of(", Pattern.compile("\\bURL\\s*\\.\\s*of\\s*\\("));
        // A statement that names URL and ends in .getContent(: the chained and the cast spellings.
        m.put("URL.getContent", Pattern.compile("\\bURL\\b[^;{}]*\\.\\s*getContent\\s*\\("));
        m.put("SocketFactory/createSocket",
            Pattern.compile("\\b(?:SSL|ServerSocket)?SocketFactory\\b|\\bcreateSocket\\s*\\("));
        // A Runtime held in a variable, and any x.exec( call: Runtime.getRuntime().exec( is caught above, but
        // `Runtime rt = Runtime.getRuntime(); rt.exec(...)` is not. getRuntime() followed by exec is excluded
        // from the second pattern (the char before the dot is ')'), so no site is counted twice.
        m.put("Runtime assigned to a variable",
            Pattern.compile("=\\s*(?:java\\s*\\.\\s*lang\\s*\\.\\s*)?Runtime\\s*\\.\\s*getRuntime\\s*\\(\\s*\\)\\s*;"));
        m.put("x.exec(", Pattern.compile("\\b\\w+\\s*\\.\\s*exec\\s*\\("));
        // A static import of an outbound member lets the call site drop the qualifier the patterns above read.
        m.put("static import of an outbound member", Pattern.compile(
            "\\bimport\\s+static\\s+(?:java\\.net\\.http\\.HttpClient|java\\.sql\\.DriverManager|java\\.net\\.InetAddress"
            + "|java\\.net\\.URL|java\\.net\\.ProxySelector|java\\.lang\\.Runtime|java\\.lang\\.ProcessBuilder"
            + "|java\\.nio\\.channels\\.(?:SocketChannel|DatagramChannel)|javax\\.net\\.(?:ssl\\.SSL)?SocketFactory)\\s*\\."));
        // A selector reroutes where a client connects; the engine's own is EgressProxy's.
        m.put("ProxySelector.of/setDefault/getDefault",
            Pattern.compile("\\bProxySelector\\s*\\.\\s*(?:of|setDefault|getDefault)\\b|\\bnew\\s+ProxySelector\\b|\\bextends\\s+ProxySelector\\b"));
        // A string literal that begins with http:// or https://. The Voyage host pin below reads the three Voyage
        // classes' literals; this puts every other one on the allowlist too.
        m.put("http(s) URL literal", Pattern.compile("\"\\s*https?://"));
        // nexus-4lqi7: the JDBC pools. A pool opens connections to whatever URL it is given, so each construction
        // site is pinned like any other outbound construct, and jdbcUrlProblems reads where the URL comes from.
        m.put("new HikariConfig(", Pattern.compile(
            "\\bnew\\s+(?:com\\s*\\.\\s*zaxxer\\s*\\.\\s*hikari\\s*\\.\\s*)?HikariConfig\\s*\\("));
        m.put("setJdbcUrl(", Pattern.compile("\\bsetJdbcUrl\\s*\\("));
        m.put("new HikariDataSource(", Pattern.compile(
            "\\bnew\\s+(?:com\\s*\\.\\s*zaxxer\\s*\\.\\s*hikari\\s*\\.\\s*)?HikariDataSource\\s*\\("));
        return java.util.Collections.unmodifiableMap(m);
    }

    private static final String VOYAGE_REASON_TAIL =
        " Built only when an API key is configured; routed through the operator's proxy by EgressProxy. "
        + "The host is pinned to api.voyageai.com by the URL-constant check in this scan.";

    /** The Voyage classes: path under the scanned root (forward slashes). The host pin applies to these. */
    static final List<String> VOYAGE_CLASSES = List.of(
        "dev/nexus/service/vectors/VoyageEmbedder.java",
        "dev/nexus/service/vectors/CceEmbedder.java",
        "dev/nexus/service/vectors/VoyageReranker.java");

    static final String VOYAGE_HOST = "api.voyageai.com";

    /** What each Voyage class shows: exactly one client builder and exactly one URL constant. */
    private static final Map<String, Integer> VOYAGE_SITES =
        Map.of("HttpClient.newBuilder", 1, "http(s) URL literal", 1);

    private static final Map<String, Integer> POOL_SITES =
        Map.of("new HikariConfig(", 2, "setJdbcUrl(", 2, "new HikariDataSource(", 2);

    private static final Map<String, Integer> JDBC_SITE = Map.of("DriverManager.getConnection", 1);

    /** Path under the scanned root to its entry. The reason is the review record: say why, not what. */
    static final Map<String, Allow> ALLOWLIST = allowlist();

    private static Map<String, Allow> allowlist() {
        Map<String, Allow> m = new TreeMap<>();
        m.put("dev/nexus/service/vectors/VoyageEmbedder.java", Allow.pinned(VOYAGE_SITES,
            "Voyage embed call to api.voyageai.com." + VOYAGE_REASON_TAIL));
        m.put("dev/nexus/service/vectors/CceEmbedder.java", Allow.pinned(VOYAGE_SITES,
            "Voyage contextualized-embed call to api.voyageai.com." + VOYAGE_REASON_TAIL));
        m.put("dev/nexus/service/vectors/VoyageReranker.java", Allow.pinned(VOYAGE_SITES,
            "Voyage rerank call to api.voyageai.com." + VOYAGE_REASON_TAIL));
        m.put("dev/nexus/service/vectors/PciBuilderSession.java", Allow.pinned(JDBC_SITE,
            "JDBC to the engine's own database (NX_DB_URL): the index builder's dedicated session."));
        m.put("dev/nexus/service/db/AdminDdlSession.java", Allow.pinned(JDBC_SITE,
            "JDBC to the engine's own database (NX_DB_ADMIN_*): the admin session of the operator constraint routes"
                + " (nexus-6pfgb.43 chunk CHECKs, nexus-6pfgb.44 catalog TTL CHECK), which adds and validates them."));
        m.put("dev/nexus/service/db/AspectQueueDrain.java", Allow.pinned(JDBC_SITE,
            "JDBC to the engine's own database (NX_DB_ADMIN_*): the owner-side census of the retired aspect queue table"
                + " (nexus-hbnml), which counts every tenant's rows with FORCE ROW LEVEL SECURITY lifted in a"
                + " transaction it rolls back."));
        m.put("dev/nexus/service/db/PoolerModeCheck.java", Allow.pinned(JDBC_SITE,
            "JDBC to the engine's own database (NX_DB_URL): the pooler-mode probe."));
        m.put("dev/nexus/service/db/EngineSingletonLock.java", Allow.pinned(JDBC_SITE,
            "JDBC to the engine's own database (NX_DB_URL): the one-engine-per-database lock connection, outside the pool."));
        m.put("dev/nexus/service/db/BackendReaper.java", Allow.pinned(JDBC_SITE,
            "JDBC to the engine's own database (NX_DB_URL): the stale-backend reaper."));
        m.put("dev/nexus/service/Main.java", Allow.pinned(POOL_SITES,
            "The engine's two Hikari pools (nexus-4lqi7): the application pool, whose URL is NX_DB_URL, and the"
                + " migration pool, whose URL is AdminConnection.url() (NX_DB_ADMIN_URL, else NX_DB_URL)."
                + " The URL-source rule in this scan reads both setJdbcUrl arguments."));
        m.put("dev/nexus/service/vectors/EgressProxy.java", Allow.pinned(
            Map.of("ProxySelector.of/setDefault/getDefault", 1, "http(s) URL literal", 1),
            "The operator's egress proxy selector for the Voyage clients, read from NX_HTTPS_PROXY / HTTPS_PROXY. "
            + "The literal is the scheme prefixed to a bare proxy host, not a destination."));
        m.put("dev/nexus/service/db/SchemaMigrator.java", Allow.pinned(Map.of("http(s) URL literal", 1),
            "A documentation link inside an error message (the migration runbook); nothing connects to it."));
        m.put("dev/nexus/service/db/PgVersionFloor.java", Allow.pinned(Map.of("http(s) URL literal", 1),
            "A documentation link inside the boot refusal (docs/configuration.md on GitHub); nothing connects to it."));
        m.put("dev/nexus/service/db/ServiceRoleBypassRlsCheck.java", Allow.pinned(Map.of("http(s) URL literal", 1),
            "A documentation link inside the boot refusal (docs/configuration.md on GitHub); nothing connects to it."));
        return java.util.Collections.unmodifiableMap(m);
    }

    private EgressGuardCensus() {
    }

    /** What a scan saw, and what is wrong with it. */
    record Report(int filesScanned, int sitesFound, int allowlistSites, Map<String, Integer> sitesByFile,
                  Map<String, Map<String, Integer>> sitesByConstruct, List<String> problems) {
    }

    // ---- comment stripping -------------------------------------------------------------------------

    /**
     * Removes {@code //} and block comments and keeps string, text-block and char literals whole, so a
     * {@code "https://host"} literal is not read as a line comment. Newlines survive.
     */
    static String stripComments(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int n = src.length();
        int i = 0;
        while (i < n) {
            char c = src.charAt(i);
            if (src.startsWith("\"\"\"", i)) {
                int j = i + 3;
                while (j < n && !src.startsWith("\"\"\"", j)) {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                j = Math.min(n, j + 3);
                out.append(src, i, j);
                i = j;
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c && src.charAt(j) != '\n') {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                j = Math.min(n, j + 1);
                out.append(src, i, j);
                i = j;
            } else if (src.startsWith("//", i)) {
                while (i < n && src.charAt(i) != '\n') {
                    i++;
                }
            } else if (src.startsWith("/*", i)) {
                int end = src.indexOf("*/", i + 2);
                int stop = end < 0 ? n : end + 2;
                for (int k = i; k < stop; k++) {
                    if (src.charAt(k) == '\n') {
                        out.append('\n');
                    }
                }
                out.append(' ');
                i = stop;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    // ---- the scan ----------------------------------------------------------------------------------

    /** Scans every {@code .java} file under {@code root} against {@code allowlist} and the Voyage pin. */
    static Report scan(Path root, Map<String, Allow> allowlist, List<String> voyageClasses) throws IOException {
        List<String> problems = new ArrayList<>();
        Map<String, Integer> sitesByFile = new TreeMap<>();
        Map<String, Map<String, Integer>> byConstruct = new TreeMap<>();
        int files = 0;
        int sites = 0;
        try (Stream<Path> walk = Files.walk(root)) {
            for (Path file : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                files++;
                String rel = root.relativize(file).toString().replace('\\', '/');
                String src = stripComments(Files.readString(file));
                problems.addAll(jdbcUrlProblems(rel, src));
                for (Map.Entry<String, Pattern> c : CONSTRUCTS.entrySet()) {
                    Matcher m = c.getValue().matcher(src);
                    while (m.find()) {
                        sites++;
                        sitesByFile.merge(rel, 1, Integer::sum);
                        byConstruct.computeIfAbsent(rel, k -> new TreeMap<>()).merge(c.getKey(), 1, Integer::sum);
                    }
                }
            }
        }

        for (Map.Entry<String, Map<String, Integer>> e : byConstruct.entrySet()) {
            Allow allow = allowlist.get(e.getKey());
            if (allow == null) {
                problems.add(e.getKey() + " has outbound construct(s) " + e.getValue()
                    + " and is not on the egress allowlist: add it with a reason, or remove the construct");
                continue;
            }
            for (String construct : e.getValue().keySet()) {
                if (!allow.constructs().contains(construct)) {
                    problems.add(e.getKey() + " uses " + construct + ", which its allowlist entry ("
                        + allow.constructs() + ") does not name");
                }
            }
        }

        int allowlistSites = 0;
        for (Map.Entry<String, Allow> e : allowlist.entrySet()) {
            allowlistSites += e.getValue().sites();
            if (e.getValue().reason() == null || e.getValue().reason().isBlank()) {
                problems.add(e.getKey() + " is on the allowlist without a reason");
            }
            Map<String, Integer> seenByConstruct = byConstruct.getOrDefault(e.getKey(), Map.of());
            if (!e.getValue().perConstruct().isEmpty()
                && !new TreeMap<>(e.getValue().perConstruct()).equals(new TreeMap<>(seenByConstruct))) {
                problems.add(e.getKey() + " is allowlisted per construct as " + new TreeMap<>(e.getValue().perConstruct())
                    + " but shows " + new TreeMap<>(seenByConstruct)
                    + ": a construct swapped for another at the same total needs a reviewed entry");
            }
            int seen = sitesByFile.getOrDefault(e.getKey(), 0);
            if (seen < e.getValue().sites()) {
                problems.add(e.getKey() + " is allowlisted for " + e.getValue().sites() + " site(s) but "
                    + "shows " + seen + ": the entry is stale, or the scan stopped seeing the construct");
            } else if (seen > e.getValue().sites()) {
                problems.add(e.getKey() + " is allowlisted for exactly " + e.getValue().sites() + " site(s) but "
                    + "shows " + seen + ": a new outbound site in an allowlisted file needs a reviewed entry");
            }
        }
        if (sites < allowlistSites) {
            problems.add("non-vacuity: found " + sites + " outbound site(s) but the allowlist names "
                + allowlistSites);
        }

        for (String voyage : voyageClasses) {
            Path file = root.resolve(voyage);
            if (!Files.isRegularFile(file)) {
                problems.add("the Voyage class " + voyage + " is not under " + root);
                continue;
            }
            problems.addAll(voyageHostProblems(voyage, stripComments(Files.readString(file))));
        }
        return new Report(files, sites, allowlistSites, sitesByFile, byConstruct, problems);
    }

    // ---- the JDBC pool URL source (nexus-4lqi7) ----------------------------------------------------

    private static final Pattern SET_JDBC_URL_CALL = Pattern.compile("\\bsetJdbcUrl\\s*\\(");
    private static final Pattern SET_JDBC_URL_VARIABLE = Pattern.compile("\\bsetJdbcUrl\\s*\\(\\s*(\\w+)\\s*\\)");
    private static final Pattern NX_DB_URL_LOOKUP = Pattern.compile(
        "(?:\\brequireEnv|\\bSystem\\s*\\.\\s*getenv)\\s*\\(\\s*\"NX_DB_URL\"\\s*\\)");
    private static final Pattern ADMIN_URL_CALL = Pattern.compile("(\\w+)\\s*\\.\\s*url\\s*\\(\\s*\\)");
    private static final Set<String> NOT_A_TYPE =
        Set.of("return", "throw", "new", "else", "case", "yield", "assert", "instanceof", "do");
    private static final Set<String> CONTROL_HEADS =
        Set.of("if", "for", "while", "switch", "catch", "synchronized", "try", "else", "do");
    private static final Pattern METHOD_HEADER = Pattern.compile(
        "(?s)^[^()]*?\\b\\w+\\s*\\([^{};]*\\)\\s*(?:throws\\s+[\\w.,\\s]+)?$");

    /** How many {@code setJdbcUrl} calls {@link #jdbcUrlProblems} examines under {@code root}: its non-vacuity count. */
    static int jdbcUrlSites(Path root) throws IOException {
        int n = 0;
        try (Stream<Path> walk = Files.walk(root)) {
            for (Path file : (Iterable<Path>) walk.filter(f -> f.toString().endsWith(".java"))::iterator) {
                Matcher m = SET_JDBC_URL_CALL.matcher(stripComments(Files.readString(file)));
                while (m.find()) {
                    n++;
                }
            }
        }
        return n;
    }

    /** A method: its header (from the previous statement boundary) and its body braces, as offsets into the source. */
    private record MethodSpan(int headerStart, int open, int close) {
    }

    /**
     * The innermost method whose body holds {@code pos}, or null. Walks the braces from the top (string, text-block
     * and char literals skipped), then takes the innermost open brace whose header looks like a method header: a
     * name and a parenthesised list, not led by a control keyword.
     */
    private static MethodSpan enclosingMethod(String src, int pos) {
        java.util.ArrayDeque<Integer> open = new java.util.ArrayDeque<>();
        List<int[]> spans = new ArrayList<>();   // {open, close} of every brace pair that holds pos
        int n = src.length();
        int i = 0;
        while (i < n) {
            char c = src.charAt(i);
            if (src.startsWith("\"\"\"", i)) {
                int j = i + 3;
                while (j < n && !src.startsWith("\"\"\"", j)) {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                i = Math.min(n, j + 3);
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c && src.charAt(j) != '\n') {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                i = Math.min(n, j + 1);
            } else {
                if (c == '{') {
                    open.push(i);
                } else if (c == '}' && !open.isEmpty()) {
                    int o = open.pop();
                    if (o < pos && pos < i) {
                        spans.add(new int[] {o, i});
                    }
                }
                i++;
            }
        }
        MethodSpan best = null;
        for (int[] sp : spans) {   // inner pairs close first, so the first method-shaped one is the innermost
            int start = Math.max(Math.max(src.lastIndexOf(';', sp[0] - 1), src.lastIndexOf('{', sp[0] - 1)),
                src.lastIndexOf('}', sp[0] - 1)) + 1;
            String header = src.substring(start, sp[0]).strip();
            String head = header.split("[\\s(]", 2)[0];
            if (!CONTROL_HEADS.contains(head) && !header.contains("->") && METHOD_HEADER.matcher(header).matches()) {
                best = new MethodSpan(start, sp[0], sp[1]);
                break;
            }
        }
        return best;
    }

    /** One declaration-shaped use of a name: a typed local, field or parameter, or a lambda parameter. */
    private record Declaration(String type, int at, int end) {
    }

    private static List<Declaration> declarationsOf(String name, String region, int base) {
        List<Declaration> found = new ArrayList<>();
        Matcher typed = Pattern.compile("(?<![\\w.$])([A-Za-z_][\\w.]*(?:<[^;(){}=]*>)?(?:\\[\\])*)\\s+"
            + Pattern.quote(name) + "\\b\\s*(?==|;|,|\\)|:)").matcher(region);
        while (typed.find()) {
            if (!NOT_A_TYPE.contains(typed.group(1))) {
                found.add(new Declaration(typed.group(1), base + typed.start(), base + typed.end()));
            }
        }
        Matcher lambda = Pattern.compile("(?<![\\w.$])" + Pattern.quote(name) + "\\s*->|\\(\\s*(?:\\w+\\s*,\\s*)*"
            + Pattern.quote(name) + "\\s*(?:,\\s*\\w+\\s*)*\\)\\s*->").matcher(region);
        while (lambda.find()) {
            found.add(new Declaration("lambda", base + lambda.start(), base + lambda.end()));
        }
        return found;
    }

    private static int assignmentsTo(String name, String region) {
        Matcher m = Pattern.compile("(?<![\\w$])" + Pattern.quote(name) + "\\s*(?:<<|>>>?|[-+*/%&|^])?=(?!=)")
            .matcher(region);
        int n = 0;
        while (m.find()) {
            n++;
        }
        return n;
    }

    /**
     * Where each {@code setJdbcUrl} argument in one file comes from, read inside the method that makes the call
     * (nexus-4lqi7). The argument must be a plain variable, and within that method (its parameter list and body):
     * the name is declared exactly once, as {@code String <name> = <rhs>;} (so no parameter, lambda parameter,
     * {@code var}, field or second declaration of the name); it is assigned nowhere else (no reassignment, no
     * {@code +=}); and the right-hand side is the {@code NX_DB_URL} lookup, or {@code url()} on a receiver that is
     * an {@code AdminConnection} parameter never reassigned, or a local built by {@code AdminConnection.resolve(}
     * and never reassigned. Textual, like the rest of this class: it is name-based and reads one method.
     */
    static List<String> jdbcUrlProblems(String rel, String source) {
        List<String> problems = new ArrayList<>();
        String src = blankLiterals(source);   // same offsets; a name inside a string is not a use of the name
        Matcher call = SET_JDBC_URL_CALL.matcher(src);
        while (call.find()) {
            Matcher arg = SET_JDBC_URL_VARIABLE.matcher(src);
            if (!arg.find(call.start()) || arg.start() != call.start()) {
                problems.add(rel + " calls setJdbcUrl with an argument that is not a plain variable: a pool URL must "
                    + "come from the NX_DB_URL lookup or an AdminConnection, named by a variable");
                continue;
            }
            String name = arg.group(1);
            MethodSpan m = enclosingMethod(src, call.start());
            if (m == null) {
                problems.add(rel + " calls setJdbcUrl(" + name + ") outside a method the rule can find: "
                    + "there is no enclosing method to read the source of " + name + " in");
                continue;
            }
            String region = src.substring(m.headerStart(), m.close());
            List<Declaration> declarations = declarationsOf(name, region, m.headerStart());
            Declaration only = declarations.size() == 1 ? declarations.get(0) : null;
            if (declarations.isEmpty()) {
                problems.add(rel + " gives setJdbcUrl the variable " + name + ", which has no declaration "
                    + "`String " + name + " = ...;` in its method: a field or other source cannot be read");
                continue;
            }
            if (only == null || !"String".equals(only.type()) || only.at() < m.open()
                || !src.startsWith("=", skipBlanks(src, only.end()))) {
                problems.add(rel + " gives setJdbcUrl the variable " + name + ", which its method declares "
                    + declarations.size() + " time(s) (types " + declarations.stream().map(Declaration::type).toList()
                    + "): it must be declared once, as a local `String " + name + " = ...;`, not a parameter, lambda "
                    + "parameter, var or a second declaration");
                continue;
            }
            if (assignmentsTo(name, region) != 1) {
                problems.add(rel + " assigns " + name + " again after its declaration in the method that gives it to "
                    + "setJdbcUrl: a pool URL variable is assigned once");
                continue;
            }
            int eq = skipBlanks(src, only.end());
            String rhs = source.substring(eq + 1, src.indexOf(';', eq)).strip();
            Matcher url = ADMIN_URL_CALL.matcher(rhs);
            boolean ok = NX_DB_URL_LOOKUP.matcher(rhs).matches()
                || (url.matches() && adminReceiverIsTrusted(url.group(1), src, m));
            if (!ok) {
                problems.add(rel + " gives setJdbcUrl the variable " + name + " = " + rhs
                    + ", which is neither the NX_DB_URL lookup nor url() on an AdminConnection parameter or one built "
                    + "by AdminConnection.resolve(");
            }
        }
        return problems;
    }

    /** The source with the inside of every string, text-block and char literal replaced by spaces, offsets kept. */
    private static String blankLiterals(String src) {
        StringBuilder out = new StringBuilder(src);
        int n = src.length();
        int i = 0;
        while (i < n) {
            char c = src.charAt(i);
            int end;
            if (src.startsWith("\"\"\"", i)) {
                int j = i + 3;
                while (j < n && !src.startsWith("\"\"\"", j)) {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                end = Math.min(n, j + 3);
                blank(out, i + 3, Math.max(i + 3, end - 3));
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c && src.charAt(j) != '\n') {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                end = Math.min(n, j + 1);
                blank(out, i + 1, Math.max(i + 1, end - 1));
            } else {
                i++;
                continue;
            }
            i = end;
        }
        return out.toString();
    }

    private static void blank(StringBuilder out, int from, int to) {
        for (int k = from; k < to && k < out.length(); k++) {
            if (out.charAt(k) != '\n') {
                out.setCharAt(k, ' ');
            }
        }
    }

    private static int skipBlanks(String src, int from) {
        int i = from;
        while (i < src.length() && Character.isWhitespace(src.charAt(i))) {
            i++;
        }
        return i;
    }

    /** {@code receiver} is declared once in the method, never reassigned, and is a parameter or a resolve() local. */
    private static boolean adminReceiverIsTrusted(String receiver, String src, MethodSpan m) {
        String region = src.substring(m.headerStart(), m.close());
        List<Declaration> declarations = declarationsOf(receiver, region, m.headerStart());
        if (declarations.size() != 1 || assignmentsTo(receiver, region) > 1) {
            return false;
        }
        Declaration d = declarations.get(0);
        boolean adminType = d.type().equals("AdminConnection") || d.type().endsWith(".AdminConnection");
        if (adminType && d.at() < m.open()) {
            return assignmentsTo(receiver, region) == 0;   // a parameter, never reassigned
        }
        if (adminType || d.type().equals("var")) {
            return resolveLocal(receiver).matcher(src.substring(d.at(), m.close())).find();
        }
        return false;
    }

    private static Pattern resolveLocal(String receiver) {
        return Pattern.compile("^[\\w.<>]+\\s+" + Pattern.quote(receiver)
            + "\\s*=\\s*(?:[\\w.]+\\s*\\.\\s*)?AdminConnection\\s*\\.\\s*resolve\\s*\\(");
    }

    private static final Pattern URL_CONSTANT = Pattern.compile(
        "static\\s+final\\s+String\\s+(\\w*URL\\w*)\\s*=\\s*\"([^\"]*)\"\\s*;");
    private static final Pattern URL_LITERAL = Pattern.compile("\"(https?://[^\"]*)\"");

    /**
     * The Voyage pin on one class: it declares at least one {@code static final String ...URL...}, every such
     * constant is an https URL whose host is {@code api.voyageai.com}, and no other http(s) literal in the
     * file names a different host.
     */
    static List<String> voyageHostProblems(String name, String strippedSource) {
        List<String> problems = new ArrayList<>();
        int constants = 0;
        Matcher m = URL_CONSTANT.matcher(strippedSource);
        while (m.find()) {
            constants++;
            String problem = hostProblem(name + " " + m.group(1), m.group(2));
            if (problem != null) {
                problems.add(problem);
            }
        }
        if (constants == 0) {
            problems.add(name + " declares no static final String URL constant: the host pin has nothing to read");
        }
        Matcher lit = URL_LITERAL.matcher(strippedSource);
        while (lit.find()) {
            String problem = hostProblem(name + " literal", lit.group(1));
            if (problem != null) {
                problems.add(problem);
            }
        }
        return problems;
    }

    private static String hostProblem(String what, String url) {
        String host;
        String scheme;
        try {
            URI uri = URI.create(url);
            host = uri.getHost();
            scheme = uri.getScheme();
        } catch (IllegalArgumentException e) {
            return what + " is not a URL: " + url;
        }
        if (!"https".equals(scheme) || !VOYAGE_HOST.equals(host)) {
            return what + " names " + url + ", not https://" + VOYAGE_HOST + "/...";
        }
        return null;
    }

    // ---- the Liquibase analytics check -------------------------------------------------------------

    private static final Pattern LIQUIBASE_VERSION = Pattern.compile(
        "<liquibase\\.version>\\s*(\\d+)\\.(\\d+)(?:\\.\\d+)?[^<]*</liquibase\\.version>");

    /** {@code liquibase.analytics.enabled} set to false, in a property file, a pom property, or a System.setProperty. */
    private static final Pattern ANALYTICS_OFF = Pattern.compile(
        "liquibase\\.analytics\\.enabled\"?\\s*[=:,>]\\s*\"?\\s*false\\b|LIQUIBASE_ANALYTICS_ENABLED\"?\\s*[=:,>]\\s*\"?\\s*false\\b",
        Pattern.CASE_INSENSITIVE);

    /**
     * Liquibase 4.30 added an anonymous analytics beacon on by default. A version of 4.30 or later is
     * accepted only when the pom or the main sources set {@code liquibase.analytics.enabled} to false.
     * This reads the pom property only; {@link #liquibaseResolvedProblems} reads what the classpath resolved.
     *
     * @param pomText    the text of {@code service/pom.xml}
     * @param configText the pom and the main sources (java and resources) joined, where the switch may live
     */
    static List<String> liquibaseProblems(String pomText, String configText) {
        Matcher m = LIQUIBASE_VERSION.matcher(stripConfigComments("pom.xml", pomText));
        if (!m.find()) {
            return List.of("pom.xml has no <liquibase.version> property: the analytics check has nothing to read");
        }
        return beaconProblems("liquibase.version " + m.group(1) + "." + m.group(2),
            Integer.parseInt(m.group(1)), Integer.parseInt(m.group(2)), configText);
    }

    private static final Pattern VERSION_PREFIX = Pattern.compile("^(\\d+)\\.(\\d+)(?:\\.(\\d+))?");

    /**
     * The same check against the version Maven actually resolved onto the classpath (nexus-3va7p). The pom
     * property is a hardcoded number that a BOM, a second declaration or a transitive upgrade can override;
     * the jar's own {@code LiquibaseUtil.getBuildVersion()} cannot be. A resolved version that is not the
     * pom's is itself a problem: the property no longer says what runs.
     *
     * @param resolvedVersion {@code liquibase.util.LiquibaseUtil.getBuildVersion()} on the test classpath
     */
    static List<String> liquibaseResolvedProblems(String resolvedVersion, String pomText, String configText) {
        Matcher r = resolvedVersion == null ? null : VERSION_PREFIX.matcher(resolvedVersion.trim());
        if (r == null || !r.find()) {
            return List.of("the resolved Liquibase version \"" + resolvedVersion + "\" is not major.minor[.patch]: "
                + "the analytics check has nothing to read");
        }
        List<String> problems = new ArrayList<>();
        Matcher pom = LIQUIBASE_VERSION.matcher(stripConfigComments("pom.xml", pomText));
        if (pom.find() && !(pom.group(1).equals(r.group(1)) && pom.group(2).equals(r.group(2)))) {
            problems.add("the classpath resolved Liquibase " + resolvedVersion + " but <liquibase.version> says "
                + pom.group(1) + "." + pom.group(2) + ": something overrides the property, so the analytics check "
                + "on the property reads the wrong number");
        }
        problems.addAll(beaconProblems("resolved Liquibase " + resolvedVersion,
            Integer.parseInt(r.group(1)), Integer.parseInt(r.group(2)), configText));
        return problems;
    }

    private static List<String> beaconProblems(String what, int major, int minor, String configText) {
        boolean beacon = major > 4 || (major == 4 && minor >= 30);
        if (beacon && !ANALYTICS_OFF.matcher(configText).find()) {
            return List.of(what + " is 4.30 or later, which sends usage analytics unless "
                + "liquibase.analytics.enabled=false is configured; none is");
        }
        return List.of();
    }

    private static final Pattern ANALYTICS_SET_PROPERTY = Pattern.compile(
        "System\\s*\\.\\s*setProperty\\s*\\(\\s*\"liquibase\\.analytics\\.enabled\"");
    private static final Pattern LIQUIBASE_FIRST_USE = Pattern.compile(
        "\\bSchemaMigrator\\s*\\.\\s*migrate\\s*\\(|\\bnew\\s+Liquibase\\s*\\(|\\bScope\\s*\\.\\s*(?:child|getCurrentScope)\\b");

    /**
     * Liquibase reads its analytics switch when its scope first initializes, so a {@code System.setProperty}
     * that comes after the first Liquibase use in the same file is too late. Takes comment-stripped Java.
     * A file that sets no such property, or uses no Liquibase, has no ordering to get wrong.
     *
     * <p>Vacuous on the live tree today, on purpose and in the open: liquibase is 4.29, no main file sets the
     * property, and {@link #analyticsSetPropertySites} counts that zero so a test can assert it. The logic is
     * proven only by the planted fixtures. The order is per file and textual, not a control-flow or
     * cross-file ordering: a switch set in one file and Liquibase used first in another is not seen.
     */
    static List<String> analyticsOrderProblems(String name, String strippedJava) {
        Matcher set = ANALYTICS_SET_PROPERTY.matcher(strippedJava);
        Matcher use = LIQUIBASE_FIRST_USE.matcher(strippedJava);
        if (set.find() && use.find() && use.start() < set.start()) {
            return List.of(name + " sets liquibase.analytics.enabled after its first Liquibase use ("
                + "offset " + use.start() + " before " + set.start() + "): the switch is read too late");
        }
        return List.of();
    }

    /** The number of {@code System.setProperty("liquibase.analytics.enabled"...)} sites in the main sources. */
    static int analyticsSetPropertySites(Path mainJavaRoot) throws IOException {
        int n = 0;
        try (Stream<Path> walk = Files.walk(mainJavaRoot)) {
            for (Path f : (Iterable<Path>) walk.filter(x -> x.toString().endsWith(".java"))::iterator) {
                Matcher m = ANALYTICS_SET_PROPERTY.matcher(stripComments(Files.readString(f)));
                while (m.find()) {
                    n++;
                }
            }
        }
        return n;
    }

    /** {@link #analyticsOrderProblems} over every main source file. */
    static List<String> analyticsOrderProblems(Path mainJavaRoot) throws IOException {
        List<String> problems = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(mainJavaRoot)) {
            for (Path f : (Iterable<Path>) walk.filter(x -> x.toString().endsWith(".java"))::iterator) {
                problems.addAll(analyticsOrderProblems(mainJavaRoot.relativize(f).toString(),
                    stripComments(Files.readString(f))));
            }
        }
        return problems;
    }

    private static final Pattern XML_COMMENT = Pattern.compile("<!--.*?-->", Pattern.DOTALL);
    private static final Pattern HASH_COMMENT = Pattern.compile("(?m)^[ \\t]*#.*$");

    /** Comment removal by file kind; see {@link #configText}. */
    static String stripConfigComments(String fileName, String text) {
        if (fileName.endsWith(".java")) {
            return stripComments(text);
        }
        if (fileName.endsWith(".xml")) {
            return XML_COMMENT.matcher(text).replaceAll(" ");
        }
        return HASH_COMMENT.matcher(text).replaceAll("");
    }

    /**
     * The pom plus every main source and resource, as one string, for {@link #liquibaseProblems}. Comments
     * are removed first (Java and XML comments, and whole-line {@code #} comments in properties and YAML),
     * so a comment that mentions the switch does not satisfy the check.
     */
    static String configText(Path pom, Path mainRoot) throws IOException {
        StringBuilder sb = new StringBuilder(stripConfigComments("pom.xml", Files.readString(pom)));
        try (Stream<Path> walk = Files.walk(mainRoot)) {
            for (Path f : (Iterable<Path>) walk.filter(Files::isRegularFile)::iterator) {
                String name = f.getFileName().toString();
                if (name.endsWith(".java") || name.endsWith(".properties") || name.endsWith(".xml")
                    || name.endsWith(".yaml") || name.endsWith(".yml")) {
                    sb.append('\n').append(stripConfigComments(name, Files.readString(f)));
                }
            }
        }
        return sb.toString();
    }
}
