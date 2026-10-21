// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ai.djl.huggingface.tokenizers.HuggingFaceTokenizer;
import dev.nexus.service.vectors.LocalTokenizers;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.Proxy;
import java.net.URL;
import java.net.URLConnection;
import java.net.URLStreamHandler;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-17jvq -- the local engine never dials the AWS instance-metadata address.
 *
 * <p>The probe is DJL's, not an AWS SDK's (the engine has none): {@code HuggingFaceTokenizer} calls
 * {@code ai.djl.util.Ec2Utils.callHome}, which asks {@code http://169.254.169.254/latest/api/token} for an IMDSv2
 * token and then reads the instance id, asks for a second token and reads the region, unless
 * {@code OPT_OUT_TRACKING} is set. On a real EC2 host that sends the instance id and region in a GET to a DJL
 * telemetry bucket.
 *
 * <p>The witness is a child JVM that installs a {@link URLStreamHandlerFactory} for http and https before it builds
 * a tokenizer: it records each URL the library opens and refuses it, so nothing leaves the machine. A child, not
 * this JVM, because the factory can be set once per JVM and {@code Ec2Utils} remembers its last check-in in a static.
 * (A {@code ProxySelector} would see nothing: {@code Ec2Utils} opens its connection with {@code Proxy.NO_PROXY}.)
 * The control child builds the tokenizer with DJL's own builder and must be seen to make the four metadata requests,
 * or the clean engine child proves nothing. The children run with {@code OPT_OUT_TRACKING} and {@code DJL_OFFLINE}
 * removed from their environment, so the result does not depend on the developer's shell.
 */
class LocalTokenizersNoImdsTest {

    private static final String IMDS_HOST = "169.254.169.254";

    /** A WordLevel tokenizer the DJL native library can load; no model download, no network. */
    private static final String TINY_TOKENIZER =
        "{\"version\":\"1.0\",\"truncation\":null,\"padding\":null,\"added_tokens\":[],"
        + "\"normalizer\":null,\"pre_tokenizer\":{\"type\":\"Whitespace\"},\"post_processor\":null,"
        + "\"decoder\":null,\"model\":{\"type\":\"WordLevel\",\"vocab\":{\"[UNK]\":0,\"hello\":1},"
        + "\"unk_token\":\"[UNK]\"}}";

    @TempDir Path dir;

    /** The child JVM: records and refuses every http(s) URL, builds one tokenizer in the named way, prints DONE. */
    public static final class Child {
        public static void main(String[] args) throws Exception {
            URL.setURLStreamHandlerFactory(protocol ->
                ("http".equals(protocol) || "https".equals(protocol)) ? new URLStreamHandler() {
                    @Override protected URLConnection openConnection(URL u) throws IOException {
                        System.out.println("REQUEST " + u);
                        throw new IOException("witness refuses " + u);
                    }
                    @Override protected URLConnection openConnection(URL u, Proxy p) throws IOException {
                        return openConnection(u);
                    }
                } : null);
            Path tok = Path.of(args[1]);
            try (HuggingFaceTokenizer t = "engine".equals(args[0])
                    ? LocalTokenizers.build(tok.toString(), 16)
                    : HuggingFaceTokenizer.builder().optTokenizerPath(tok).build()) {
                System.out.println("TOKENS " + t.encode("hello").getTokens().length);
            }
            System.out.println("DONE");
        }
    }

    /** Runs {@link Child} in a fresh JVM and returns the URLs it recorded; fails unless it printed DONE. */
    private List<String> requestsFrom(String mode) throws Exception {
        Path tok = dir.resolve("tokenizer-" + mode + ".json");
        Files.writeString(tok, TINY_TOKENIZER);
        ProcessBuilder pb = new ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp", System.getProperty("java.class.path"),
                Child.class.getName(), mode, tok.toString())
            .redirectErrorStream(true);
        pb.environment().remove(LocalTokenizers.OPT_OUT_PROPERTY);
        pb.environment().remove(LocalTokenizers.OFFLINE_ENV);
        Process p = pb.start();
        List<String> lines = new ArrayList<>();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
            for (String line; (line = r.readLine()) != null; ) {
                lines.add(line);
            }
        }
        assertThat(p.waitFor(60, TimeUnit.SECONDS)).as("the %s child exits", mode).isTrue();
        assertThat(p.exitValue()).as("the %s child ran to the end: %s", mode, lines).isZero();
        assertThat(lines).as("the %s child built and used a tokenizer", mode).contains("DONE");
        return lines.stream().filter(l -> l.startsWith("REQUEST ")).toList();
    }

    @Test
    void theEngineTokenizerBuildMakesNoHttpRequestAtAll() throws Exception {
        assertThat(requestsFrom("engine")).as("the engine build asked nothing, least of all %s", IMDS_HOST).isEmpty();
    }

    @Test
    void control_anUnguardedDjlBuildMakesTheFourMetadataRequests() throws Exception {
        List<String> urls = requestsFrom("control");
        assertThat(urls)
            .as("without the opt-out DJL's own build asks %s four times: token, instance-id, token, region; "
                + "if this differs the witness cannot see the probe", IMDS_HOST)
            .hasSize(4)
            .allMatch(u -> u.contains(IMDS_HOST));
        assertThat(urls.get(0)).endsWith("/latest/api/token");
        assertThat(urls.get(1)).endsWith("/instance-id");
        assertThat(urls.get(2)).endsWith("/latest/api/token");
        assertThat(urls.get(3)).endsWith("/placement/region");
    }

    @Test
    void goOffline_setsBothSwitches_idempotently_whenNothingIsChosen() {
        Properties props = new Properties();
        assertThat(LocalTokenizers.goOffline(name -> null, props)).isEmpty();
        assertThat(LocalTokenizers.goOffline(name -> null, props)).isEmpty();
        assertThat(props.getProperty("OPT_OUT_TRACKING")).isEqualTo("true");
        assertThat(props.getProperty("ai.djl.offline")).isEqualTo("true");
    }

    @Test
    void goOffline_leavesAnOperatorChoiceAlone_andNamesAnExportedValueDjlReadsAsFalse() {
        Properties chosen = new Properties();
        chosen.setProperty("OPT_OUT_TRACKING", "false");
        chosen.setProperty("ai.djl.offline", "false");
        assertThat(LocalTokenizers.goOffline(name -> null, chosen)).isEmpty();
        assertThat(chosen.getProperty("OPT_OUT_TRACKING")).isEqualTo("false");
        assertThat(chosen.getProperty("ai.djl.offline")).isEqualTo("false");

        // An exported true wins over the property and needs no warning, and no property is written.
        Properties none = new Properties();
        assertThat(LocalTokenizers.goOffline(name -> "TRUE", none)).isEmpty();
        assertThat(none).isEmpty();

        // An exported value that is not true: DJL reads false, the property cannot override it, so it is named.
        for (String bad : new String[] {"", "1", "yes", "false"}) {
            Properties p = new Properties();
            assertThat(LocalTokenizers.goOffline(name -> bad, p))
                .as("exported value '%s'", bad)
                .containsExactly("OPT_OUT_TRACKING", "DJL_OFFLINE");
            assertThat(p).as("a property is not written under an exported value '%s'", bad).isEmpty();
        }
    }

    /** The real no-argument entry point, against this JVM's real environment; properties cleared and restored. */
    @Test
    void goOffline_realEntryPoint_agreesWithTheEnvironmentItRunsIn() {
        String[][] switches = {{"OPT_OUT_TRACKING", "OPT_OUT_TRACKING"}, {"DJL_OFFLINE", "ai.djl.offline"}};
        for (String[] sw : switches) {
            String before = System.getProperty(sw[1]);
            try {
                System.clearProperty(sw[1]);
                LocalTokenizers.goOffline();
                LocalTokenizers.goOffline();
                if (System.getenv(sw[0]) == null) {
                    assertThat(System.getProperty(sw[1])).as(sw[1]).isEqualTo("true");
                } else {
                    assertThat(System.getProperty(sw[1])).as("%s is exported, so the helper left %s alone", sw[0], sw[1]).isNull();
                }
            } finally {
                if (before == null) {
                    System.clearProperty(sw[1]);
                } else {
                    System.setProperty(sw[1], before);
                }
            }
        }
    }

    private static final Pattern DIRECT_BUILD =
        Pattern.compile("HuggingFaceTokenizer\\s*\\.\\s*(builder|newInstance)\\b");
    private static final Pattern HELPER_BUILD = Pattern.compile("LocalTokenizers\\s*\\.\\s*build\\s*\\(");
    private static final Pattern COMMENTS = Pattern.compile("/\\*.*?\\*/|//[^\\n]*", Pattern.DOTALL);

    /**
     * Census: the production classes that build a tokenizer do it through {@link LocalTokenizers}, so another direct
     * {@code HuggingFaceTokenizer.builder()} or {@code newInstance(...)} (in any spacing) cannot reopen the probe
     * unseen. Comments are stripped first, so prose that names a call is not a call. Non-vacuous: it must read the
     * engine sources, find the three known callers and exactly the helper's own builder call.
     */
    @Test
    void everyProductionTokenizerBuildGoesThroughTheHelper() throws Exception {
        Path root = Path.of("src/main/java");
        int examined = 0;
        int directBuilds = 0;
        int viaHelper = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path f : files.filter(p -> p.toString().endsWith(".java")).toList()) {
                String src = COMMENTS.matcher(Files.readString(f)).replaceAll(" ");
                examined++;
                Matcher direct = DIRECT_BUILD.matcher(src);
                while (direct.find()) {
                    directBuilds++;
                    assertThat(f.getFileName().toString())
                        .as("only the helper may build a DJL tokenizer directly: %s", direct.group())
                        .isEqualTo("LocalTokenizers.java");
                }
                if (!f.getFileName().toString().equals("LocalTokenizers.java")) {
                    Matcher helper = HELPER_BUILD.matcher(src);
                    while (helper.find()) {
                        viaHelper++;
                    }
                }
            }
        }
        assertThat(examined).as("the census read the engine sources").isGreaterThan(50);
        assertThat(directBuilds).as("exactly one direct DJL build: the helper's").isEqualTo(1);
        assertThat(viaHelper).as("Bge768Embedder, CrossEncoderReranker, OnnxEmbedder").isEqualTo(3);
    }
}
