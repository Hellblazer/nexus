// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Technical Design 7 (nexus-qfy01): the source census of the egress guard. The live test runs
 * {@link EgressGuardCensus} over {@code src/main/java} and {@code pom.xml}; the others are the negative
 * fixtures, each a planted tree or text the census must reject, so a scan that stopped seeing its target
 * cannot pass for a clean tree.
 */
class EgressGuardCensusTest {

    private static final Path MAIN = Path.of("src", "main", "java");
    private static final String VOYAGE = "dev/nexus/service/vectors/VoyageEmbedder.java";
    private static final String GOOD_VOYAGE_CLASS = """
        package dev.nexus.service.vectors;
        import java.net.http.HttpClient;
        final class VoyageEmbedder {
            private static final String VOYAGE_URL = "https://api.voyageai.com/v1/embeddings";
            HttpClient c() { return HttpClient.newBuilder().build(); }
        }
        """;

    @TempDir Path dir;

    private Path plant(String rel, String src) throws IOException {
        Path f = dir.resolve(rel);
        Files.createDirectories(f.getParent());
        Files.writeString(f, src);
        return f;
    }

    private static final Map<String, EgressGuardCensus.Allow> ONE_VOYAGE = Map.of(
        VOYAGE, new EgressGuardCensus.Allow(Set.of("HttpClient.newBuilder", "http(s) URL literal"), 2, "fixture"));

    // ---- the live census ---------------------------------------------------------------------------

    @Test
    void liveMainSources_everyOutboundConstructIsOnTheAllowlist_andTheVoyageHostIsPinned() throws IOException {
        EgressGuardCensus.Report r =
            EgressGuardCensus.scan(MAIN, EgressGuardCensus.ALLOWLIST, EgressGuardCensus.VOYAGE_CLASSES);
        // The first-run record: files scanned, sites found, allowlist size and the per-file sites.
        System.out.println("EGRESS_CENSUS filesScanned=" + r.filesScanned() + " sitesFound=" + r.sitesFound()
            + " allowlistEntries=" + EgressGuardCensus.ALLOWLIST.size() + " allowlistSites=" + r.allowlistSites()
            + " sitesByFile=" + r.sitesByFile());
        assertThat(r.problems()).as("egress census").isEmpty();
        assertThat(r.filesScanned()).as("non-vacuity: the main sources were walked").isGreaterThan(50);
        assertThat(r.sitesFound()).as("non-vacuity: sites found vs the allowlist")
            .isGreaterThanOrEqualTo(r.allowlistSites()).isGreaterThan(0);
        assertThat(EgressGuardCensus.ALLOWLIST).as("every entry carries a reason")
            .allSatisfy((file, allow) -> assertThat(allow.reason()).isNotBlank());
    }

    @Test
    void livePom_liquibaseIsOlderThanItsAnalyticsBeacon_orHasAnalyticsOff() throws IOException {
        String pom = Files.readString(Path.of("pom.xml"));
        String config = EgressGuardCensus.configText(Path.of("pom.xml"), Path.of("src", "main"));
        assertThat(pom).as("non-vacuity: the liquibase.version property is in the pom")
            .contains("<liquibase.version>");
        assertThat(EgressGuardCensus.liquibaseProblems(pom, config)).isEmpty();
    }

    @Test
    void liveClasspath_theResolvedLiquibaseIsOlderThanItsAnalyticsBeacon_orHasAnalyticsOff() throws IOException {
        String resolved = liquibase.util.LiquibaseUtil.getBuildVersion();
        String pom = Files.readString(Path.of("pom.xml"));
        String config = EgressGuardCensus.configText(Path.of("pom.xml"), Path.of("src", "main"));
        System.out.println("EGRESS_LIQUIBASE resolved=" + resolved);
        assertThat(resolved).as("non-vacuity: the jar reports a version").isNotBlank().matches("\\d+\\.\\d+.*");
        assertThat(EgressGuardCensus.liquibaseResolvedProblems(resolved, pom, config)).isEmpty();
    }

    @Test
    void liveMain_noFileSetsTheAnalyticsSwitchAfterItsFirstLiquibaseUse() throws IOException {
        int sites = EgressGuardCensus.analyticsSetPropertySites(MAIN);
        System.out.println("EGRESS_ANALYTICS_ORDER setPropertySitesExamined=" + sites);
        assertThat(EgressGuardCensus.analyticsOrderProblems(MAIN)).isEmpty();
        // Vacuous on purpose and stated: liquibase is 4.29 and nothing sets the switch, so there is no order
        // to check on the live tree. The planted fixtures below prove the logic. When a setProperty site
        // appears (a 4.30+ upgrade), this assertion fails and forces the live ordering to be looked at.
        assertThat(sites).as("non-vacuity, inverted: the live scan found no setProperty site, so the ordering "
            + "check examined nothing; the fixture proves the logic").isZero();
    }

    @Test
    void fixture_aConstructSwappedForAnotherAtTheSameTotalFailsThePerConstructPin() throws IOException {
        Map<String, EgressGuardCensus.Allow> pinned = Map.of(VOYAGE, EgressGuardCensus.Allow.pinned(
            Map.of("HttpClient.newBuilder", 1, "http(s) URL literal", 1), "fixture"));
        plant(VOYAGE, GOOD_VOYAGE_CLASS);
        assertThat(EgressGuardCensus.scan(dir, pinned, List.of()).problems()).isEmpty();

        // The URL literal swapped for a second builder: still two sites, still only constructs the entry names.
        plant(VOYAGE, GOOD_VOYAGE_CLASS.replace("\"https://api.voyageai.com/v1/embeddings\"", "null")
            .replace("HttpClient c()", "HttpClient d() { return HttpClient.newBuilder().build(); }\n    HttpClient c()"));
        EgressGuardCensus.Report r = EgressGuardCensus.scan(dir, pinned, List.of());
        assertThat(r.sitesFound()).as("same total as the clean class").isEqualTo(2);
        assertThat(r.problems()).as("the total-only pin would have passed this").anyMatch(p -> p.contains("swapped"));
    }

    @Test
    void liveAllowlist_everyEntryPinsItsConstructsOneByOne() {
        assertThat(EgressGuardCensus.ALLOWLIST).allSatisfy((file, allow) -> {
            assertThat(allow.perConstruct()).as(file).isNotEmpty();
            assertThat(allow.perConstruct().keySet()).as(file).isEqualTo(allow.constructs());
            assertThat(allow.perConstruct().values().stream().mapToInt(Integer::intValue).sum()).as(file)
                .isEqualTo(allow.sites());
        });
    }

    // ---- negative fixtures: the census itself is not vacuous --------------------------------------

    @Test
    void fixture_aCleanPlantedTreePasses() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS);
        EgressGuardCensus.Report r = EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE));
        assertThat(r.problems()).isEmpty();
        assertThat(r.sitesFound()).as("the builder and the URL constant").isEqualTo(2);
    }

    @Test
    void fixture_anHttpClientBuiltOutsideTheAllowlistFails() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS);
        plant("dev/nexus/service/http/Rogue.java", """
            package dev.nexus.service.http;
            import java.net.http.HttpClient;
            final class Rogue { Object c = HttpClient . newHttpClient(); }
            """);
        EgressGuardCensus.Report r = EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE));
        assertThat(r.problems()).anyMatch(p -> p.contains("Rogue.java") && p.contains("not on the egress allowlist"));

        plant("dev/nexus/service/http/Rogue.java", """
            package dev.nexus.service.http;
            final class Rogue { Object c = java.net.http.HttpClient
                .newBuilder().build(); }
            """);
        assertThat(EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE)).problems())
            .as("a line break inside the call does not hide it")
            .anyMatch(p -> p.contains("Rogue.java"));
    }

    /**
     * One planted spelling per construct, keyed by the construct's name. The set of keys must equal
     * {@link EgressGuardCensus#CONSTRUCTS}, so a construct added to the census without a positive control
     * fails {@link #fixture_everyListedConstructHasAPositiveControl_andTheDetectorFiresOnIt}.
     */
    private static final Map<String, String> SPELLINGS = spellings();

    private static Map<String, String> spellings() {
        Map<String, String> m = new java.util.LinkedHashMap<>();
        m.put("HttpClient.newBuilder", "Object o = HttpClient.newBuilder();");
        m.put("HttpClient.newHttpClient", "Object o = HttpClient.newHttpClient" + "();");
        m.put("openConnection(", "Object o = u.openConnection();");
        m.put("openStream(", "Object o = u.openStream();");
        m.put("new Socket(", "Object o = new Socket(h, 1);");
        m.put("ServerSocket", "Object o = new ServerSocket(0);");
        m.put("SocketChannel.open", "Object o = SocketChannel.open();");
        m.put("DriverManager.getConnection", "Object o = DriverManager.getConnection(u);");
        m.put("ProcessBuilder", "Object o = new ProcessBuilder(a);");
        m.put("Runtime.getRuntime().exec", "Object o = Runtime.getRuntime().exec(a);");
        m.put("DatagramSocket/DatagramChannel", "Object o = new DatagramSocket();");
        m.put("InetAddress.getBy*", "Object o = InetAddress.getByName(h);");
        m.put("new URL(", "Object o = new URL(u);");
        m.put("toURL(", "Object o = uri.toURL();");
        m.put("URL.of(", "Object o = URL.of(uri, null);");
        m.put("URL.getContent", "Object o = new URL(u).getContent();");
        m.put("SocketFactory/createSocket", "Object o = SocketFactory.getDefault();");
        m.put("Runtime assigned to a variable", "Object o = null; o = Runtime.getRuntime();");
        m.put("x.exec(", "Object o = rt.exec(a);");
        m.put("static import of an outbound member", "import static java.net.http.HttpClient.newBuilder; class Q {}");
        m.put("ProxySelector.of/setDefault/getDefault", "Object o = ProxySelector.of(a);");
        m.put("http(s) URL literal", "Object o = \"https://rogue.example/x\";");
        return java.util.Collections.unmodifiableMap(m);
    }

    /** The planted file for a spelling: a class body, or the spelling alone when it is a whole declaration. */
    private static String planted(String spelling) {
        return spelling.startsWith("import ") ? spelling : "class Rogue { void m() { " + spelling + " } }";
    }

    @Test
    void fixture_everyListedConstructHasAPositiveControl_andTheDetectorFiresOnIt() throws IOException {
        assertThat(SPELLINGS.keySet()).as("every construct in the census has a planted spelling, and no spelling is orphaned")
            .containsExactlyInAnyOrderElementsOf(EgressGuardCensus.CONSTRUCTS.keySet());
        int i = 0;
        for (Map.Entry<String, String> e : SPELLINGS.entrySet()) {
            Path sub = dir.resolve("case" + i++);
            Files.createDirectories(sub);
            Files.writeString(sub.resolve("Rogue.java"), planted(e.getValue()));
            EgressGuardCensus.Report r = EgressGuardCensus.scan(sub, Map.of(), List.of());
            assertThat(r.sitesByConstruct().getOrDefault("Rogue.java", Map.of()))
                .as("the census sees %s in %s, under that name", e.getKey(), e.getValue())
                .containsKey(e.getKey());
            assertThat(r.problems()).as("and rejects %s", e.getKey()).isNotEmpty();
        }
    }

    /** Each control spelled the way the first review said the old list missed it. */
    @Test
    void fixture_theSpellingsTheFirstReviewNamedAreSeen() throws IOException {
        String[][] cases = {
            {"DatagramChannel.open()", "DatagramSocket/DatagramChannel"},
            {"new MulticastSocket(1)", "DatagramSocket/DatagramChannel"},
            {"java.net.InetAddress.getAllByName(h)", "InetAddress.getBy*"},
            {"InetAddress\n .getByAddress(b)", "InetAddress.getBy*"},
            {"((URL) o).getContent()", "URL.getContent"},
            {"javax.net.ssl.SSLSocketFactory.getDefault().createSocket(h, 1)", "SocketFactory/createSocket"},
            {"sf.createSocket(h, 1)", "SocketFactory/createSocket"},
            {"this.rt.exec(a)", "x.exec("},
            {"p . exec ( a )", "x.exec("},
            {"java.net.ProxySelector.setDefault(null)", "ProxySelector.of/setDefault/getDefault"},
            {"\"http://rogue.example\" + x", "http(s) URL literal"},
            {"\" https://rogue.example\"", "http(s) URL literal"},
        };
        int i = 0;
        for (String[] c : cases) {
            Path sub = dir.resolve("named" + i++);
            Files.createDirectories(sub);
            Files.writeString(sub.resolve("Rogue.java"), "class Rogue { void m() { Object o = " + c[0] + "; } }");
            assertThat(EgressGuardCensus.scan(sub, Map.of(), List.of()).sitesByConstruct()
                .getOrDefault("Rogue.java", Map.of())).as(c[0]).containsKey(c[1]);
        }
        // A Runtime held in a variable, then exec'd: both halves are seen.
        Path sub = dir.resolve("runtimeVar");
        Files.createDirectories(sub);
        Files.writeString(sub.resolve("Rogue.java"),
            "class Rogue { void m() throws Exception { Runtime rt = Runtime.getRuntime(); rt.exec(\"x\"); } }");
        assertThat(EgressGuardCensus.scan(sub, Map.of(), List.of()).sitesByConstruct().get("Rogue.java"))
            .containsKeys("Runtime assigned to a variable", "x.exec(")
            .doesNotContainKey("Runtime.getRuntime().exec");
    }

    @Test
    void fixture_theNewPatternsDoNotFireOnTheEnginesOrdinaryRuntimeAndPathUses() throws IOException {
        plant("A.java", """
            class A {
                int n = Runtime.getRuntime().availableProcessors();
                void halt() { Runtime.getRuntime().halt(1); Runtime.getRuntime().addShutdownHook(null); }
                Object r(Rec rec) { return rec.getContent(); }
                String s = "see https://example.com in docs";
                java.util.concurrent.ExecutorService e;
                void run() { e.execute(null); }
            }
            """);
        EgressGuardCensus.Report r = EgressGuardCensus.scan(dir, Map.of(), List.of());
        assertThat(r.sitesByConstruct()).as("ordinary uses are not sites").isEmpty();
        assertThat(r.problems()).isEmpty();
    }

    @Test
    void fixture_aConstructNamedOnlyInACommentOrDocIsNotASite() throws IOException {
        plant("A.java", """
            /** Never call HttpClient.newBuilder() here; see {@code new Socket(} and ProcessBuilder. */
            class A {
                // HttpClient .newHttpClient()
                /* DriverManager.getConnection(x) */
                // "https://example.com/in-a-comment"
            }
            """);
        EgressGuardCensus.Report r = EgressGuardCensus.scan(dir, Map.of(), List.of());
        assertThat(r.sitesFound()).isZero();
        assertThat(r.problems()).isEmpty();
    }

    @Test
    void fixture_aSlashSlashInsideAStringLiteralIsNotAComment() {
        String src = "String u = \"https://api.voyageai.com/v1/x\"; HttpClient.newBuilder(); // tail";
        String stripped = EgressGuardCensus.stripComments(src);
        assertThat(stripped).contains("\"https://api.voyageai.com/v1/x\"").contains("HttpClient.newBuilder()")
            .doesNotContain("tail");
    }

    @Test
    void fixture_anAllowlistedConstructOtherThanTheNamedOneFails() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS.replace("return HttpClient.newBuilder().build();",
            "new Socket(\"x\", 1); return HttpClient.newBuilder().build();"));
        assertThat(EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE)).problems())
            .anyMatch(p -> p.contains("uses new Socket(") && p.contains("does not name"));
    }

    @Test
    void fixture_aStaleAllowlistEntryFails_andSoDoesAnAllowlistLargerThanTheSitesFound() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS.replace("HttpClient.newBuilder().build()", "null"));
        EgressGuardCensus.Report r = EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE));
        assertThat(r.problems()).anyMatch(p -> p.contains("stale"));
        assertThat(r.problems()).anyMatch(p -> p.startsWith("non-vacuity"));
    }

    @Test
    void fixture_aSecondSiteInAnAllowlistedFileFails() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS.replace("HttpClient c()",
            "HttpClient d() { return HttpClient.newBuilder().build(); }\n    HttpClient c()"));
        EgressGuardCensus.Report r = EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE));
        assertThat(r.sitesFound()).isEqualTo(3);
        assertThat(r.problems()).anyMatch(p -> p.contains("exactly 2") && p.contains("shows 3"));

        String reaper = "dev/nexus/service/db/BackendReaper.java";
        Map<String, EgressGuardCensus.Allow> one = Map.of(
            reaper, new EgressGuardCensus.Allow(Set.of("DriverManager.getConnection"), 1, "fixture"));
        Path sub = dir.resolve("reaperTree");
        Path f = sub.resolve(reaper);
        Files.createDirectories(f.getParent());
        Files.writeString(f, """
            package dev.nexus.service.db;
            final class BackendReaper {
                Object a() throws Exception { return java.sql.DriverManager.getConnection("jdbc:x"); }
                Object b() throws Exception { return java.sql.DriverManager.getConnection("jdbc:y"); }
            }
            """);
        EgressGuardCensus.Report rr = EgressGuardCensus.scan(sub, one, List.of());
        assertThat(rr.sitesFound()).isEqualTo(2);
        assertThat(rr.problems())
            .anyMatch(p -> p.contains("BackendReaper.java") && p.contains("exactly 1") && p.contains("shows 2"));
    }

    @Test
    void fixture_anAllowlistEntryWithoutAReasonFails() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS);
        Map<String, EgressGuardCensus.Allow> noReason = Map.of(
            VOYAGE, new EgressGuardCensus.Allow(Set.of("HttpClient.newBuilder", "http(s) URL literal"), 2, " "));
        assertThat(EgressGuardCensus.scan(dir, noReason, List.of(VOYAGE)).problems())
            .anyMatch(p -> p.contains("without a reason"));
    }

    @Test
    void fixture_aVoyageUrlConstantNamingAnotherHostFails() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS.replace("api.voyageai.com", "api.evil.example"));
        assertThat(EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE)).problems())
            .anyMatch(p -> p.contains("VOYAGE_URL") && p.contains("api.evil.example"));
    }

    @Test
    void fixture_aVoyageUrlWithTheRightNameInTheWrongPlaceFails() throws IOException {
        // userinfo trick and a look-alike suffix: the host is not api.voyageai.com in either.
        for (String url : new String[] {"https://api.voyageai.com@evil.example/v1", "https://api.voyageai.com.evil.example/v1",
                                        "http://api.voyageai.com/v1/embeddings"}) {
            plant(VOYAGE, GOOD_VOYAGE_CLASS.replace("https://api.voyageai.com/v1/embeddings", url));
            assertThat(EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE)).problems())
                .as(url).anyMatch(p -> p.contains("VOYAGE_URL"));
        }
    }

    @Test
    void fixture_aSecondHttpLiteralOnAnotherHostInAVoyageClassFails_andAMissingConstantFails() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS.replace("HttpClient c()",
            "String other = \"https://telemetry.example/ping\"; HttpClient c()"));
        assertThat(EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE)).problems())
            .anyMatch(p -> p.contains("telemetry.example"));

        plant(VOYAGE, GOOD_VOYAGE_CLASS.replace("VOYAGE_URL", "VOYAGE_ENDPOINT"));
        assertThat(EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE)).problems())
            .anyMatch(p -> p.contains("no static final String URL constant"));
    }

    @Test
    void fixture_liquibase430WithoutAnalyticsOffFails() {
        String pom = "<properties><liquibase.version>4.30.0</liquibase.version></properties>";
        assertThat(EgressGuardCensus.liquibaseProblems(pom, pom)).hasSize(1).first().asString()
            .contains("4.30").contains("analytics");
        String later = "<liquibase.version>5.0.1</liquibase.version>";
        assertThat(EgressGuardCensus.liquibaseProblems(later, later)).hasSize(1);
        String v431 = "<liquibase.version>4.31.1</liquibase.version>";
        assertThat(EgressGuardCensus.liquibaseProblems(v431, v431)).hasSize(1);
    }

    @Test
    void fixture_liquibaseOlderThan430PassesAndAnalyticsOffPasses() {
        assertThat(EgressGuardCensus.liquibaseProblems(
            "<liquibase.version>4.29.0</liquibase.version>", "")).isEmpty();
        String pom = "<liquibase.version>4.30.0</liquibase.version>";
        assertThat(EgressGuardCensus.liquibaseProblems(pom,
            pom + " System.setProperty(\"liquibase.analytics.enabled\", \"false\");")).isEmpty();
        assertThat(EgressGuardCensus.liquibaseProblems(pom,
            pom + "\nliquibase.analytics.enabled=false")).isEmpty();
        assertThat(EgressGuardCensus.liquibaseProblems(pom,
            pom + "\nliquibase.analytics.enabled=true")).as("true is not off").hasSize(1);
    }

    @Test
    void fixture_aCommentNamingTheAnalyticsSwitchDoesNotSatisfyTheCheck() throws IOException {
        String pomText = "<project><properties><liquibase.version>4.30.0</liquibase.version></properties></project>";
        Path pom = dir.resolve("pom.xml");
        Files.writeString(pom, pomText);
        Path main = dir.resolve("main");
        Files.createDirectories(main);
        Files.writeString(main.resolve("A.java"), """
            class A {
                // liquibase.analytics.enabled=false
                /* System.setProperty("liquibase.analytics.enabled", "false"); */
            }
            """);
        Files.writeString(main.resolve("app.properties"), "# liquibase.analytics.enabled=false\n");
        assertThat(EgressGuardCensus.liquibaseProblems(pomText, EgressGuardCensus.configText(pom, main)))
            .as("comments naming the switch").hasSize(1);

        Files.writeString(pom, pomText.replace("</project>", "<!-- liquibase.analytics.enabled=false --></project>"));
        assertThat(EgressGuardCensus.liquibaseProblems(pomText, EgressGuardCensus.configText(pom, main)))
            .as("a pom comment naming the switch").hasSize(1);

        Files.writeString(main.resolve("A.java"), """
            class A {
                static { System.setProperty("liquibase.analytics.enabled", "false"); }
            }
            """);
        assertThat(EgressGuardCensus.liquibaseProblems(pomText, EgressGuardCensus.configText(pom, main)))
            .as("a real setting still satisfies it").isEmpty();
    }

    @Test
    void fixture_aPomWithNoLiquibaseVersionFails() {
        assertThat(EgressGuardCensus.liquibaseProblems("<project/>", "")).hasSize(1);
    }

    @Test
    void fixture_aResolvedLiquibaseAt430OrLaterFailsWithoutAnalyticsOff_whateverThePomProperty() {
        String pomOld = "<liquibase.version>4.29.0</liquibase.version>";
        assertThat(EgressGuardCensus.liquibaseResolvedProblems("4.29.2", pomOld, pomOld)).isEmpty();
        // The pom says 4.29 but a BOM or transitive upgrade put 4.32 on the classpath: two problems.
        assertThat(EgressGuardCensus.liquibaseResolvedProblems("4.32.0", pomOld, pomOld))
            .hasSize(2).anyMatch(p -> p.contains("overrides the property")).anyMatch(p -> p.contains("analytics"));
        // A matching version with the switch off passes; a SNAPSHOT or a patch suffix still parses.
        String pom = "<liquibase.version>4.30.0</liquibase.version>";
        assertThat(EgressGuardCensus.liquibaseResolvedProblems("4.30.0", pom, pom + "\nliquibase.analytics.enabled=false"))
            .isEmpty();
        assertThat(EgressGuardCensus.liquibaseResolvedProblems("5.0.1-SNAPSHOT", "<project/>", "")).hasSize(1);
        assertThat(EgressGuardCensus.liquibaseResolvedProblems("unknown", pom, pom)).hasSize(1);
        assertThat(EgressGuardCensus.liquibaseResolvedProblems(null, pom, pom)).hasSize(1);
    }

    @Test
    void fixture_theAnalyticsSwitchSetAfterTheFirstLiquibaseUseFails() throws IOException {
        String late = EgressGuardCensus.stripComments("""
            class Main { void main() {
                SchemaMigrator.migrate(ds);
                System.setProperty("liquibase.analytics.enabled", "false");
            } }
            """);
        assertThat(EgressGuardCensus.analyticsOrderProblems("Main.java", late)).hasSize(1).first().asString()
            .contains("too late");
        String early = late.replace("SchemaMigrator.migrate(ds);", "").replace("} }",
            "SchemaMigrator.migrate(ds);\n} }");
        assertThat(EgressGuardCensus.analyticsOrderProblems("Main.java", early)).isEmpty();
        assertThat(EgressGuardCensus.analyticsOrderProblems("A.java", "class A { void m() { new Liquibase(a); } }"))
            .as("no switch in the file, nothing to order").isEmpty();

        plant("dev/Late.java", "class Late { void m() { new Liquibase(a, b); System.setProperty(\"liquibase.analytics.enabled\", \"false\"); } }");
        assertThat(EgressGuardCensus.analyticsOrderProblems(dir)).as("the directory walk finds it").hasSize(1);
    }
}
