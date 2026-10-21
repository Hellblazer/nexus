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
        VOYAGE, new EgressGuardCensus.Allow(Set.of("HttpClient.newBuilder"), 1, "fixture"));

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

    // ---- negative fixtures: the census itself is not vacuous --------------------------------------

    @Test
    void fixture_aCleanPlantedTreePasses() throws IOException {
        plant(VOYAGE, GOOD_VOYAGE_CLASS);
        EgressGuardCensus.Report r = EgressGuardCensus.scan(dir, ONE_VOYAGE, List.of(VOYAGE));
        assertThat(r.problems()).isEmpty();
        assertThat(r.sitesFound()).isEqualTo(1);
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

    @Test
    void fixture_everyListedConstructIsSeen() throws IOException {
        String[] spellings = {
            "HttpClient.newBuilder()", "HttpClient.newHttpClient" + "()", "u.openConnection()", "u.openStream()",
            "new Socket(h, 1)", "new ServerSocket(0)", "SocketChannel.open()", "ServerSocketChannel.open()",
            "DriverManager.getConnection(u)", "new ProcessBuilder(a)", "Runtime.getRuntime().exec(a)"};
        int i = 0;
        for (String spelling : spellings) {
            Path sub = dir.resolve("case" + i++);
            Files.createDirectories(sub);
            Files.writeString(sub.resolve("Rogue.java"), "class Rogue { void m() { Object o = " + spelling + "; } }");
            EgressGuardCensus.Report r = EgressGuardCensus.scan(sub, Map.of(), List.of());
            assertThat(r.sitesFound()).as("the census sees %s", spelling).isGreaterThanOrEqualTo(1);
            assertThat(r.problems()).as("and rejects %s", spelling).isNotEmpty();
        }
    }

    @Test
    void fixture_aConstructNamedOnlyInACommentOrDocIsNotASite() throws IOException {
        plant("A.java", """
            /** Never call HttpClient.newBuilder() here; see {@code new Socket(} and ProcessBuilder. */
            class A {
                // HttpClient .newHttpClient()
                /* DriverManager.getConnection(x) */
                String s = "https://example.com/not-a-site";
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
        assertThat(r.sitesFound()).isEqualTo(2);
        assertThat(r.problems()).anyMatch(p -> p.contains("exactly 1") && p.contains("shows 2"));

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
            VOYAGE, new EgressGuardCensus.Allow(Set.of("HttpClient.newBuilder"), 1, " "));
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
}
