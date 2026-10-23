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
 * <p>The local engine connects only to the Voyage API ({@code api.voyageai.com}) from the three Voyage
 * classes, and only when keyed, and to its database at {@code NX_DB_URL} over JDBC. This scan holds that
 * by text over the main sources: comments are stripped (string literals are kept, because the Voyage
 * hosts live in them), then every outbound construct in {@link #CONSTRUCTS} must sit in a file on
 * {@link #ALLOWLIST}, with that construct named on the entry and a reason beside it.
 *
 * <p>This is a text scan of {@code service/src/main/java} for the constructs in {@link #CONSTRUCTS}, and
 * nothing more. It does not see any dependency's code: {@code java.net.http} used inside a library, raw
 * sockets, DNS or JNI inside a library (DJL's Rust tokenizers library does network I/O that no Java-level
 * check can see), or the behavior of the GraalVM native image the engine ships as. It also misses an
 * outbound call spelled through reflection, or reached by a type this list does not name; a static import
 * of a listed member is caught, and so are datagram sockets, DNS lookups, URL objects, socket factories, a
 * Runtime held in a variable, a ProxySelector and an http(s) string literal (nexus-3va7p). JDBC beyond
 * {@code DriverManager} (the Hikari pools, {@code ds.getConnection()}) is not checked at all. No test in this repository covers those gaps; {@code EgressRuntimeWitnessTest} is a
 * separate, narrower check and does not close them. Each allowlist entry pins an exact site count for its
 * file, so a second site in an allowlisted file fails; a file whose count falls short is stale and fails.
 * Non-vacuity: {@link #scan} reports the sites it found, and the caller asserts the count is at least the
 * number the allowlist names.
 *
 * <p>The scan takes its roots as arguments so a test can run it against a planted tree.
 */
final class EgressGuardCensus {

    /** One allowlist entry: the constructs the file may use, the exact number of sites it shows, and why. */
    record Allow(Set<String> constructs, int sites, String reason) {
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

    /** What each Voyage class shows: its one client builder and its one URL constant. */
    private static final Set<String> VOYAGE_CONSTRUCTS = Set.of("HttpClient.newBuilder", "http(s) URL literal");

    /** Path under the scanned root to its entry. The reason is the review record: say why, not what. */
    static final Map<String, Allow> ALLOWLIST = allowlist();

    private static Map<String, Allow> allowlist() {
        Map<String, Allow> m = new TreeMap<>();
        m.put("dev/nexus/service/vectors/VoyageEmbedder.java", new Allow(
            VOYAGE_CONSTRUCTS, 2, "Voyage embed call to api.voyageai.com." + VOYAGE_REASON_TAIL));
        m.put("dev/nexus/service/vectors/CceEmbedder.java", new Allow(
            VOYAGE_CONSTRUCTS, 2,
            "Voyage contextualized-embed call to api.voyageai.com." + VOYAGE_REASON_TAIL));
        m.put("dev/nexus/service/vectors/VoyageReranker.java", new Allow(
            VOYAGE_CONSTRUCTS, 2, "Voyage rerank call to api.voyageai.com." + VOYAGE_REASON_TAIL));
        m.put("dev/nexus/service/vectors/PciBuilderSession.java", new Allow(
            Set.of("DriverManager.getConnection"), 1,
            "JDBC to the engine's own database (NX_DB_URL): the index builder's dedicated session."));
        m.put("dev/nexus/service/db/PoolerModeCheck.java", new Allow(
            Set.of("DriverManager.getConnection"), 1,
            "JDBC to the engine's own database (NX_DB_URL): the pooler-mode probe."));
        m.put("dev/nexus/service/db/EngineSingletonLock.java", new Allow(
            Set.of("DriverManager.getConnection"), 1,
            "JDBC to the engine's own database (NX_DB_URL): the one-engine-per-database lock connection, outside the pool."));
        m.put("dev/nexus/service/db/BackendReaper.java", new Allow(
            Set.of("DriverManager.getConnection"), 1,
            "JDBC to the engine's own database (NX_DB_URL): the stale-backend reaper."));
        m.put("dev/nexus/service/vectors/EgressProxy.java", new Allow(
            Set.of("ProxySelector.of/setDefault/getDefault", "http(s) URL literal"), 2,
            "The operator's egress proxy selector for the Voyage clients, read from NX_HTTPS_PROXY / HTTPS_PROXY. "
            + "The literal is the scheme prefixed to a bare proxy host, not a destination."));
        m.put("dev/nexus/service/db/SchemaMigrator.java", new Allow(
            Set.of("http(s) URL literal"), 1,
            "A documentation link inside an error message (the migration runbook); nothing connects to it."));
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
